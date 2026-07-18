from __future__ import annotations

import logging
from datetime import date

import bot.db as db
from bot.direction_math import stop_trigger_price
from execution.broker_interface import OrderSide, OrderStatus
from monitoring.logger import EventType, emit_event

log = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, broker, risk_cfg=None):
        self.broker = broker
        if risk_cfg is None:
            from system.config import settings
            risk_cfg = settings.risk
        self._risk = risk_cfg
        self._opened_today = 0
        self._opened_short_today = 0

    def get_cash(self) -> float:
        return self.broker.get_cash()

    def can_open_new_position(self) -> bool:
        long_count = sum(1 for p in self.broker.get_positions() if p.get("qty", 0) >= 0)
        if long_count >= self._risk.max_positions:
            return False
        if self._opened_today >= self._risk.max_positions_per_day:
            return False
        return True

    def can_open_new_short_position(self) -> bool:
        short_count = sum(1 for p in self.broker.get_positions() if p.get("qty", 0) < 0)
        if short_count >= self._risk.max_short_positions:
            return False
        if self._opened_short_today >= self._risk.max_short_positions_per_day:
            return False
        return True

    def reset_daily_counter(self) -> None:
        self._opened_today = 0
        self._opened_short_today = 0

    def open_position(self, ticker: str, position_pct: float, signal_id: int | None,
                      rationale: str, entry_price: float,
                      signal_source: str = "congressional",
                      initial_stop_pct: float | None = None,
                      direction: str = "long") -> bool:
        """Returns True if position was successfully opened."""
        is_short = direction == "short"

        if is_short:
            position_pct = min(position_pct, self._risk.max_short_position_pct)
            stop_pct_used = (
                initial_stop_pct if initial_stop_pct is not None
                else self._risk.short_trailing_stop_pct
            )
            if not self.broker.shorting_enabled():
                log.warning("open_position: account does not support shorting — skipping %s", ticker)
                return False
            if not self.broker.is_shortable(ticker):
                log.warning("open_position: %s is not shortable (HTB or restricted) — skipping", ticker)
                return False
        else:
            position_pct = min(position_pct, self._risk.max_position_pct)
            stop_pct_used = (
                initial_stop_pct if initial_stop_pct is not None else self._risk.trailing_stop_pct
            )

        # Pre-flight duplicate check before committing real capital — ticker-unique
        # regardless of direction: a name can never be simultaneously long and short.
        if db.position_exists(ticker):
            log.warning("open_position: %s already in DB — skipping duplicate open", ticker)
            return False

        # Size against NAV (cash + mark-to-market positions), not cash alone
        positions_now = self.broker.get_positions()
        nav = self.get_cash() + sum(p["qty"] * p["current_price"] for p in positions_now)
        shares = (nav * position_pct / 100) / entry_price

        order_side = "sell" if is_short else "buy"
        order = self.broker.place_order(ticker=ticker, side=order_side, qty=shares)
        if order.status == OrderStatus.REJECTED:
            log.warning("Order rejected for %s: %s", ticker, order.reject_reason)
            return False
        if order.status != OrderStatus.FILLED:
            # Fill poll timed out (or any other non-terminal state) — the order
            # is still live at the broker with an unconfirmed outcome. Booking a
            # position here would create a phantom: the DB believes we're in,
            # but the broker doesn't. Cancel the dangling order (a no-op if it
            # actually filled in the interim — reconcile_with_broker's
            # untracked-position path is the backstop for that race) and skip
            # this candidate rather than book on a guess.
            cancelled = self.broker.cancel_order(order.order_id)
            if cancelled:
                emit_event(
                    log, EventType.ORDER_REJECTED,
                    f"{ticker} {order_side} order {order.order_id} did not confirm FILLED "
                    f"(status={order.status.value}) — cancelled, position not opened",
                    data={"ticker": ticker, "order_id": order.order_id, "status": order.status.value},
                    level=logging.ERROR,
                    alert=True,
                )
            else:
                # cancel_order returned a falsy result — the order may not actually
                # be cancelled (transient broker error, or a state that can't be
                # cancelled). Unlike the success path above, this is NOT a
                # "handled, no action needed" outcome: a stray order could still be
                # resting at the broker, invisible to both the DB (we never book
                # it) and reconcile_with_broker's untracked-position check (which
                # only looks at broker *positions*, not outstanding *orders*).
                emit_event(
                    log, EventType.ORDER_REJECTED,
                    f"{ticker} {order_side} order {order.order_id} did not confirm FILLED "
                    f"(status={order.status.value}) — cancel FAILED, order may still "
                    f"be resting at the broker — check manually",
                    data={
                        "ticker": ticker, "order_id": order.order_id,
                        "status": order.status.value, "cancel_failed": True,
                    },
                    level=logging.CRITICAL,
                    alert=True,
                )
            return False

        # Use actual fill data when available; fall back to pre-trade NAV estimate
        # for a FILLED order that still left filled_avg_price at 0.0.
        actual_shares = shares
        actual_entry_price = entry_price
        if order.filled_avg_price > 0 and order.filled_qty > 0:
            actual_shares = order.filled_qty
            actual_entry_price = order.filled_avg_price
            # Recompute position_pct from actual fill value / current NAV so that
            # downstream consumers get the real allocation, not the pre-slippage estimate.
            position_pct = actual_shares * actual_entry_price / nav * 100

        entry_commission = actual_shares * self.broker.get_commission_per_share()
        try:
            db.insert_position(
                ticker=ticker,
                entry_price=actual_entry_price,
                shares=actual_shares,
                position_pct=position_pct,
                entry_date=date.today().isoformat(),
                signal_id=signal_id,
                rationale=rationale,
                signal_source=signal_source,
                entry_commission=entry_commission,
                stop_pct=stop_pct_used,
                direction=direction,
            )
        except Exception:
            log.critical(
                "CRITICAL: broker order for %s was filled but DB insert failed — "
                "manual close required. Shares=%.4f @ $%.2f",
                ticker, actual_shares, actual_entry_price,
            )
            raise

        if is_short:
            self._opened_short_today += 1
        else:
            self._opened_today += 1

        # Register a resting stop order at the initial trailing-stop level so that
        # overnight / between-poll gaps are covered. The polled enforce_stop_losses()
        # acts as backstop and updates (trails) the stop upward as the peak rises.
        stop_price = stop_trigger_price(direction, actual_entry_price, stop_pct_used)
        stop_side = "buy" if is_short else "sell"
        initial_stop_id = self._place_stop_with_retry(ticker, actual_shares, stop_price, side=stop_side)
        if initial_stop_id is None:
            # Placement failed — the position is open with zero resting stop.
            # enforce_stop_losses() polling is the backstop, but it's not
            # instantaneous; alert so a human can check the broker directly.
            emit_event(
                log, EventType.ORDER_REJECTED,
                f"Failed to place initial stop for {ticker} at ${stop_price:.2f} — "
                "position is open with NO resting stop",
                data={"ticker": ticker, "attempted_stop_price": stop_price},
                level=logging.ERROR,
                alert=True,
            )

        return True

    def close_position(self, ticker: str, shares: float, exit_price: float,
                       exit_reason: str, signal_id: int | None, entry_price: float,
                       entry_date: str, signal_source: str = "congressional",
                       direction: str = "long") -> bool:
        """Returns True if the position was booked closed, False on no-fill (REJECTED/CANCELLED/SUBMITTED)."""
        order = self._place_closing_order_with_retry(ticker, shares, direction)
        _NON_FILL = (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.SUBMITTED)
        if order.status in _NON_FILL:
            reason = order.reject_reason or order.status.value
            emit_event(
                log, EventType.ORDER_REJECTED,
                f"Close for {ticker} {order.status.value} after retries ({reason}) — "
                "position left intact for next reconcile/poll",
                data={"ticker": ticker, "reason": reason, "status": order.status.value},
                level=logging.ERROR,
                alert=True,
            )
            return False
        actual_filled = order.filled_qty if order.filled_qty > 0 else shares
        if order.filled_qty <= 0:
            log.warning(
                "close_position: order.filled_qty=0 for %s — falling back to caller shares=%.4f",
                ticker, shares,
            )
        exit_commission = actual_filled * self.broker.get_commission_per_share()
        self._book_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=actual_filled,
            entry_date=entry_date,
            exit_reason=exit_reason,
            signal_id=signal_id,
            signal_source=signal_source,
            exit_commission=exit_commission,
            direction=direction,
        )
        return True

    def _book_closed_position(self, ticker: str, entry_price: float, exit_price: float,
                              shares: float, entry_date: str, exit_reason: str,
                              signal_id: int | None, signal_source: str,
                              exit_commission: float, direction: str = "long") -> None:
        """Shared booking logic: write the closed_positions row, delete the open
        position, and cancel any resting stop. Used both for a freshly-placed
        sell (close_position) and for a fill discovered after the fact via
        get_order_history() (reconcile_with_broker's ghost-position handling)."""
        entry_commission = 0.0
        for pos in db.get_open_positions():
            if pos["ticker"] == ticker:
                entry_commission = pos["entry_commission"] if pos["entry_commission"] is not None else 0.0
                break
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=shares,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason=exit_reason,
            signal_id=signal_id,
            signal_source=signal_source,
            costs=exit_commission,
            entry_commission=entry_commission,
            direction=direction,
        )
        db.delete_position(ticker)
        if hasattr(self.broker, "cancel_stop_order"):
            self.broker.cancel_stop_order(ticker)

    def _place_stop_with_retry(self, ticker: str, qty: float, stop_price: float,
                               max_retries: int = 3, side: str = "sell") -> str | None:
        """Alpaca's wash-trade check can reject a stop placed immediately after
        its opposite-side buy fills (it lags our own fill confirmation) —
        code 40310000, "opposite side market/stop order exists". Retry with
        backoff before surfacing the no-resting-stop alert; a real rejection
        (e.g. bad price) fails the same way each attempt and still alerts."""
        import time
        stop_id = None
        for attempt in range(max_retries):
            stop_id = self.broker.place_stop_order(
                ticker=ticker, qty=qty, stop_price=stop_price, side=side
            )
            if stop_id is not None:
                return stop_id
            if attempt < max_retries - 1:
                delay = 1.0 * (attempt + 1)
                log.warning("Stop placement failed for %s (attempt %d/%d) — retrying in %.0fs",
                            ticker, attempt + 1, max_retries, delay)
                time.sleep(delay)
        return stop_id

    def _place_closing_order_with_retry(self, ticker: str, qty: float, direction: str,
                                        max_retries: int = 3):
        """Places the order that CLOSES a position: sell (long) or buy-to-cover (short)."""
        import time
        close_side = "sell" if direction == "long" else "buy"
        order = None
        for attempt in range(max_retries):
            order = self.broker.place_order(ticker=ticker, side=close_side, qty=qty)
            if order.status != OrderStatus.REJECTED:
                return order
            if attempt < max_retries - 1:
                delay = 1.0 * (attempt + 1)
                log.warning("%s rejected for %s (attempt %d/%d): %s — retrying in %.0fs",
                            close_side, ticker, attempt + 1, max_retries, order.reject_reason, delay)
                time.sleep(delay)
        return order

    def reduce_position(self, ticker: str, shares: float, exit_price: float,
                        signal_id: int | None, entry_price: float, entry_date: str,
                        signal_source: str = "congressional",
                        direction: str = "long") -> bool:
        """Returns True if the partial close was booked, False on no-fill (REJECTED/CANCELLED/SUBMITTED)."""
        sell_qty = shares / 2
        order = self._place_closing_order_with_retry(ticker, sell_qty, direction)
        _NON_FILL = (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.SUBMITTED)
        if order.status in _NON_FILL:
            reason = order.reject_reason or order.status.value
            emit_event(
                log, EventType.ORDER_REJECTED,
                f"Reduce for {ticker} {order.status.value} after retries ({reason}) — "
                "shares left unchanged",
                data={"ticker": ticker, "reason": reason, "status": order.status.value},
                level=logging.ERROR,
                alert=True,
            )
            return False
        actual_filled = order.filled_qty if order.filled_qty > 0 else sell_qty
        if order.filled_qty <= 0:
            log.warning(
                "reduce_position: order.filled_qty=0 for %s — falling back to sell_qty=%.4f",
                ticker, sell_qty,
            )
        exit_commission = actual_filled * self.broker.get_commission_per_share()
        entry_commission = 0.0
        for pos in db.get_open_positions():
            if pos["ticker"] == ticker:
                full_entry_comm = pos["entry_commission"] if pos["entry_commission"] is not None else 0.0
                entry_commission = full_entry_comm * (actual_filled / shares) if shares > 0 else 0.0
                break
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=actual_filled,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason="reduce",
            signal_id=signal_id,
            signal_source=signal_source,
            costs=exit_commission,
            entry_commission=entry_commission,
            direction=direction,
        )
        db.update_position_shares(ticker, shares - actual_filled)
        # Cancel the stale full-qty stop; the next enforce_stop_losses poll re-places
        # a fresh trailing stop for the reduced share count.
        if hasattr(self.broker, "cancel_stop_order"):
            self.broker.cancel_stop_order(ticker)
        return True

    def _find_matching_fill(self, ticker: str, direction: str = "long"):
        """Look for a filled closing order for `ticker` in the broker's order history.

        A long's closing fill is a SELL; a short's is a BUY (buy-to-cover).

        Used by reconcile_with_broker to distinguish a ghost position whose
        resting stop (or other order) actually filled server-side from one
        that vanished with no record at all. Returns the matching Order, or
        None if get_order_history() is unavailable or has no matching fill for
        this ticker.
        """
        if not hasattr(self.broker, "get_order_history"):
            return None
        expected_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
        try:
            history = self.broker.get_order_history()
        except Exception as exc:
            log.warning("get_order_history failed during reconcile for %s: %s", ticker, exc)
            return None
        for order in history:
            if (
                order.ticker == ticker
                and order.side == expected_side
                and order.status == OrderStatus.FILLED
            ):
                return order
        return None

    def reconcile_with_broker(self) -> dict:
        """Compare broker positions vs SQLite. Log and resolve discrepancies.

        Returns dict with keys: ghost_positions (in SQLite not broker),
        untracked_positions (in broker not SQLite).

        **Untracked position handling** (controlled by ``RiskConfig.auto_flatten_untracked``):

        * ``auto_flatten_untracked=False`` (default, safe): emit a CRITICAL alert for every
          untracked broker position so a human can review before the next pipeline run. The
          position is **not** added to the DB and therefore is **not** covered by
          ``enforce_stop_losses`` until reconciled manually. Trade-off: no automatic exposure
          change, but no automatic stop coverage either.

        * ``auto_flatten_untracked=True`` (aggressive): immediately issue a market sell order
          for each untracked position and emit an alert.  This eliminates exposure quickly but
          may crystallise a loss on a position that was legitimately opened by a separate
          system or manual trade.  Use only in fully-automated deployments where no manual
          broker intervention occurs.
        """
        broker_pos_list = self.broker.get_positions()
        broker_positions = {p["ticker"] for p in broker_pos_list}
        db_meta_by_ticker = {p["ticker"]: dict(p) for p in db.get_open_positions()}
        db_positions = set(db_meta_by_ticker)

        ghost = db_positions - broker_positions
        untracked = broker_positions - db_positions

        for ticker in ghost:
            meta = db_meta_by_ticker.get(ticker, {})
            direction = meta.get("direction", "long")
            fill = self._find_matching_fill(ticker, direction)
            if fill is not None:
                # The position didn't vanish — a resting order (e.g. a GTC stop)
                # filled server-side. Book the real outcome instead of discarding it.
                log.warning(
                    "RECONCILIATION: %s in SQLite but not at broker — found a matching "
                    "fill in order history, booking it instead of a bare delete",
                    ticker,
                )
                exit_commission = fill.filled_qty * self.broker.get_commission_per_share()
                self._book_closed_position(
                    ticker=ticker,
                    entry_price=meta.get("entry_price", 0.0),
                    exit_price=fill.filled_avg_price,
                    shares=fill.filled_qty or meta.get("shares", 0.0),
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    exit_reason="reconcile_fill",
                    signal_id=meta.get("signal_id"),
                    signal_source=meta.get("signal_source", "congressional"),
                    exit_commission=exit_commission,
                    direction=direction,
                )
            else:
                log.warning(
                    "RECONCILIATION: %s in SQLite but not at broker — removing ghost position",
                    ticker,
                )
                db.delete_position(ticker)
                emit_event(
                    log, EventType.DEAD_FEED,
                    f"RECONCILIATION: ghost position {ticker} removed with no matching fill "
                    "in order history — its outcome (and any P&L) is unknown. Manual review "
                    "recommended.",
                    data={"ticker": ticker},
                    level=logging.CRITICAL,
                    alert=True,
                )

        for ticker in untracked:
            if getattr(self._risk, "auto_flatten_untracked", False):
                # Aggressive mode: close the position immediately via market sell
                broker_qty = next(
                    (p["qty"] for p in broker_pos_list if p["ticker"] == ticker), 0.0
                )
                log.warning(
                    "RECONCILIATION: %s at broker but not in SQLite — auto-flattening "
                    "(auto_flatten_untracked=True), qty=%.4f",
                    ticker, broker_qty,
                )
                if broker_qty > 0:
                    self.broker.place_order(ticker=ticker, side="sell", qty=broker_qty)
                emit_event(
                    log, EventType.DEAD_FEED,
                    f"RECONCILIATION: untracked position {ticker} auto-flattened "
                    f"(qty={broker_qty:.4f})",
                    alert=True,
                )
            else:
                # Safe mode (default): alert loudly so a human can investigate
                log.critical(
                    "RECONCILIATION: %s at broker but not in SQLite — manual trade or bug. "
                    "Review before next pipeline run.",
                    ticker,
                )
                emit_event(
                    log, EventType.DEAD_FEED,
                    f"RECONCILIATION: untracked position {ticker} at broker — "
                    "CRITICAL: not in SQLite. Manual review required.",
                    data={"ticker": ticker},
                    level=logging.CRITICAL,
                    alert=True,
                )

        return {"ghost_positions": list(ghost), "untracked_positions": list(untracked)}

    def enforce_stop_losses(
        self,
        stop_loss_pct: float | None = None,
        source_include: str | None = None,
        source_exclude: str | None = None,
    ) -> list[str]:
        if source_include is not None and source_exclude is not None:
            raise ValueError("source_include and source_exclude are mutually exclusive")
        default_pct = self._risk.trailing_stop_pct
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            meta = open_positions.get(ticker, {})
            source = meta.get("signal_source", "congressional")

            # Scope filter FIRST: a hedge-only call must not touch long stops (and
            # vice versa), so each position's resting stop uses its own call's pct.
            if source_include is not None and source != source_include:
                continue
            if source_exclude is not None and source == source_exclude:
                continue

            # An explicit override (e.g. hedge-scoped polling) applies uniformly and
            # ignores the stored per-position value. With no override, each position
            # trails/closes at its OWN stop_pct (set once at open time), not whatever
            # RiskConfig.trailing_stop_pct happens to be right now.
            stored_pct = meta.get("stop_pct")
            pct = (
                stop_loss_pct if stop_loss_pct is not None
                else stored_pct if stored_pct is not None
                else default_pct
            )

            current = pos["current_price"]
            stored_peak = meta.get("peak_price")
            peak = stored_peak if stored_peak is not None else pos["avg_entry_price"]
            db.update_position_peak(ticker, current)

            # Trail the resting stop upward (only-up). Cancel the old stop before
            # placing the new one so brokers (Alpaca) don't accumulate duplicates.
            new_stop = current * (1 - pct / 100)
            existing_stop = 0.0
            existing_stop_id: str | None = None
            try:
                if hasattr(self.broker, "get_stop_orders"):
                    _stops = self.broker.get_stop_orders()
                    if isinstance(_stops, dict):
                        _resting = _stops.get(ticker)
                        if _resting is not None:
                            existing_stop = float(_resting[0])
                            # 3-tuple (price, qty, order_id); tolerate the old
                            # 2-tuple shape from any stale mock by guarding length.
                            if len(_resting) > 2:
                                existing_stop_id = _resting[2]
            except Exception:
                pass
            if new_stop > existing_stop:
                # Place the new stop BEFORE cancelling the old one so the
                # position always has a resting stop (no gap between cancel and place).
                new_stop_id = self.broker.place_stop_order(
                    ticker=ticker, qty=pos["qty"], stop_price=new_stop
                )
                if new_stop_id is not None:
                    # Cancel the OLD stop by its specific id, never by ticker —
                    # the new stop shares the same ticker/type/status, so a
                    # ticker-only cancel (order_id=None) would catch the
                    # just-placed new stop too, leaving zero resting stops.
                    # When there's no KNOWN prior stop id (e.g. a fresh broker
                    # after a restart — get_stop_orders() found nothing for
                    # this ticker), there's nothing to cancel; skip the call
                    # entirely rather than sweeping the ticker.
                    if existing_stop_id is not None and hasattr(self.broker, "cancel_stop_order"):
                        self.broker.cancel_stop_order(ticker, order_id=existing_stop_id)
                else:
                    # Placement failed — keep the old stop resting rather than
                    # cancelling it and leaving the position with zero stops.
                    emit_event(
                        log, EventType.ORDER_REJECTED,
                        f"Failed to place trailing stop for {ticker} at ${new_stop:.2f} — "
                        "keeping the existing resting stop in place",
                        data={"ticker": ticker, "attempted_stop_price": new_stop},
                        level=logging.ERROR,
                        alert=True,
                    )

            # A non-positive peak (e.g. an explicitly-stored 0.0) can't be used
            # as a meaningful denominator — skip the stop-loss distance check
            # for this poll rather than dividing by zero (same guard style as
            # enforce_take_profits' `if entry <= 0: continue`).
            if peak <= 0:
                continue
            drop_from_peak = (peak - current) / peak * 100
            if drop_from_peak >= pct:
                if self.close_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    exit_reason="stop_loss",
                    signal_id=meta.get("signal_id"),
                    entry_price=meta.get("entry_price") or pos["avg_entry_price"],
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=meta.get("signal_source", "congressional"),
                ):
                    closed.append(ticker)
        return closed

    def enforce_take_profits(
        self,
        take_profit_pct: float | None = None,
        hard_exit_pct: float | None = None,
        source_exclude: str | None = None,
    ) -> list[str]:
        tp_pct = take_profit_pct if take_profit_pct is not None else self._risk.take_profit_pct
        he_pct = hard_exit_pct if hard_exit_pct is not None else self._risk.hard_exit_pct
        reduced = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            if ticker in reduced:
                continue
            meta = open_positions.get(ticker, {})
            source = meta.get("signal_source", "congressional")

            if source_exclude is not None and source == source_exclude:
                continue

            # Use DB entry_price as canonical (not broker avg which can drift)
            entry = meta.get("entry_price") or pos["avg_entry_price"]
            current = pos["current_price"]
            if entry <= 0:
                continue
            gain_pct = (current - entry) / entry * 100

            if gain_pct >= he_pct:
                # Hard exit: full close
                if self.close_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    exit_reason="hard_exit",
                    signal_id=meta.get("signal_id"),
                    entry_price=entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=source,
                ):
                    reduced.append(ticker)
            elif gain_pct >= tp_pct and not meta.get("take_profit_taken", 0):
                # Partial reduce: sell 50%, mark flag so we don't reduce again
                if self.reduce_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    signal_id=meta.get("signal_id"),
                    entry_price=entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=source,
                ):
                    db.mark_take_profit_taken(ticker)
                    reduced.append(ticker)
        return reduced

    @staticmethod
    def is_sector_capped(sector: str, sector_allocation: dict[str, float],
                         cap_pct: float = 30.0) -> bool:
        return sector_allocation.get(sector, 0.0) >= cap_pct

    @staticmethod
    def is_liquid_enough(position_size_usd: float, avg_daily_volume_usd: float,
                         max_adv_pct: float = 10.0) -> bool:
        if avg_daily_volume_usd <= 0:
            return False
        return (position_size_usd / avg_daily_volume_usd * 100) <= max_adv_pct

    @staticmethod
    def is_in_drawdown(peak_nav: float, current_nav: float,
                       max_drawdown_pct: float = 10.0) -> bool:
        if peak_nav <= 0:
            return False
        return (peak_nav - current_nav) / peak_nav * 100 >= max_drawdown_pct

    def log_snapshot(self) -> None:
        positions = self.broker.get_positions()
        positions_value = sum(p["qty"] * p["current_price"] for p in positions)
        cash = self.get_cash()
        db.log_portfolio(
            date=date.today().isoformat(),
            cash=cash,
            positions_value=positions_value,
            total_nav=cash + positions_value,
        )
