from __future__ import annotations

import logging
from datetime import date

import bot.db as db
from execution.broker_interface import OrderStatus

log = logging.getLogger(__name__)

# Kept for any external code that imports these constants directly.
MAX_POSITIONS = 20
MAX_POSITIONS_PER_DAY = 3
MAX_POSITION_PCT = 8.0


class Portfolio:
    def __init__(self, broker, risk_cfg=None):
        self.broker = broker
        if risk_cfg is None:
            from system.config import settings
            risk_cfg = settings.risk
        self._risk = risk_cfg
        self._opened_today = 0

    def get_cash(self) -> float:
        return self.broker.get_cash()

    def can_open_new_position(self) -> bool:
        if len(self.broker.get_positions()) >= self._risk.max_positions:
            return False
        if self._opened_today >= self._risk.max_positions_per_day:
            return False
        return True

    def reset_daily_counter(self) -> None:
        self._opened_today = 0

    def open_position(self, ticker: str, position_pct: float, signal_id: int | None,
                      rationale: str, entry_price: float,
                      signal_source: str = "congressional") -> bool:
        """Returns True if position was successfully opened."""
        position_pct = min(position_pct, self._risk.max_position_pct)

        # Pre-flight duplicate check before committing real capital
        if db.position_exists(ticker):
            log.warning("open_position: %s already in DB — skipping duplicate open", ticker)
            return False

        # Size against NAV (cash + mark-to-market positions), not cash alone
        positions_now = self.broker.get_positions()
        nav = self.get_cash() + sum(p["qty"] * p["current_price"] for p in positions_now)
        shares = (nav * position_pct / 100) / entry_price

        order = self.broker.place_order(ticker=ticker, side="buy", qty=shares)
        if order.status == OrderStatus.REJECTED:
            log.warning("Order rejected for %s: %s", ticker, order.reject_reason)
            return False

        try:
            db.insert_position(
                ticker=ticker,
                entry_price=entry_price,
                shares=shares,
                position_pct=position_pct,
                entry_date=date.today().isoformat(),
                signal_id=signal_id,
                rationale=rationale,
                signal_source=signal_source,
            )
        except Exception:
            log.critical(
                "CRITICAL: broker order for %s was filled but DB insert failed — "
                "manual close required. Shares=%.4f @ $%.2f",
                ticker, shares, entry_price,
            )
            raise

        self._opened_today += 1
        return True

    def close_position(self, ticker: str, shares: float, exit_price: float,
                       exit_reason: str, signal_id: int | None, entry_price: float,
                       entry_date: str, signal_source: str = "congressional") -> None:
        order = self._place_sell_with_retry(ticker, shares)
        if order.status == OrderStatus.REJECTED:
            log.error(
                "Sell order failed for %s after retries: %s — manual close may be required",
                ticker, order.reject_reason,
            )
        _commission = vars(self.broker).get("_commission_per_share", 0.0)
        costs = shares * _commission if isinstance(_commission, (int, float)) else 0.0
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
            costs=costs,
        )
        db.delete_position(ticker)

    def _place_sell_with_retry(self, ticker: str, qty: float, max_retries: int = 3):
        import time
        order = None
        for attempt in range(max_retries):
            order = self.broker.place_order(ticker=ticker, side="sell", qty=qty)
            if order.status != OrderStatus.REJECTED:
                return order
            if attempt < max_retries - 1:
                delay = 1.0 * (attempt + 1)
                log.warning("Sell rejected for %s (attempt %d/%d): %s — retrying in %.0fs",
                            ticker, attempt + 1, max_retries, order.reject_reason, delay)
                time.sleep(delay)
        return order

    def reduce_position(self, ticker: str, shares: float, exit_price: float,
                        signal_id: int | None, entry_price: float, entry_date: str,
                        signal_source: str = "congressional") -> None:
        sell_qty = shares / 2
        order = self._place_sell_with_retry(ticker, sell_qty)
        if order.status == OrderStatus.REJECTED:
            log.error(
                "Reduce sell failed for %s after retries: %s — manual close may be required",
                ticker, order.reject_reason,
            )
        _commission = vars(self.broker).get("_commission_per_share", 0.0)
        costs = sell_qty * _commission if isinstance(_commission, (int, float)) else 0.0
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=sell_qty,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason="reduce",
            signal_id=signal_id,
            signal_source=signal_source,
            costs=costs,
        )
        db.update_position_shares(ticker, shares - sell_qty)

    def reconcile_with_broker(self) -> dict:
        """Compare broker positions vs SQLite. Log and resolve discrepancies.

        Returns dict with keys: ghost_positions (in SQLite not broker),
        untracked_positions (in broker not SQLite).
        """
        broker_positions = {p["ticker"] for p in self.broker.get_positions()}
        db_positions = {p["ticker"] for p in db.get_open_positions()}

        ghost = db_positions - broker_positions
        untracked = broker_positions - db_positions

        for ticker in ghost:
            log.warning(
                "RECONCILIATION: %s in SQLite but not at broker — removing ghost position",
                ticker,
            )
            db.delete_position(ticker)

        for ticker in untracked:
            log.warning(
                "RECONCILIATION: %s at broker but not in SQLite — manual trade or bug",
                ticker,
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
        pct = stop_loss_pct if stop_loss_pct is not None else self._risk.trailing_stop_pct
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            current = pos["current_price"]
            meta = open_positions.get(ticker, {})
            peak = meta.get("peak_price") or pos["avg_entry_price"]
            db.update_position_peak(ticker, current)

            source = meta.get("signal_source", "congressional")

            if source_include is not None and source != source_include:
                continue
            if source_exclude is not None and source == source_exclude:
                continue
            drop_from_peak = (peak - current) / peak * 100
            if drop_from_peak >= pct:
                self.close_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    exit_reason="stop_loss",
                    signal_id=meta.get("signal_id"),
                    entry_price=meta.get("entry_price") or pos["avg_entry_price"],
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=meta.get("signal_source", "congressional"),
                )
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
                self.close_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    exit_reason="hard_exit",
                    signal_id=meta.get("signal_id"),
                    entry_price=entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=source,
                )
                reduced.append(ticker)
            elif gain_pct >= tp_pct and not meta.get("take_profit_taken", 0):
                # Partial reduce: sell 50%, mark flag so we don't reduce again
                self.reduce_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    signal_id=meta.get("signal_id"),
                    entry_price=entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=source,
                )
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
