"""Alpaca paper trading broker — implements BrokerInterface.

Pass `api_client` in tests to inject a mock and avoid network calls.
"""
from __future__ import annotations

import logging
import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce

from execution.broker_interface import (
    BrokerInterface, Order, OrderSide, OrderStatus, OrderType,
)
from monitoring.logger import EventType, emit_event

log = logging.getLogger(__name__)

_ALPACA_TIMEOUT_SECONDS = 15  # > _poll_order_fill's ~14s budget; must still be finite


def _apply_request_timeout(client: TradingClient) -> None:
    """alpaca-py's RESTClient exposes no timeout parameter at all (confirmed
    via inspect.signature(TradingClient.__init__)/inspect.signature(
    RESTClient.__init__), v0.43.2) and every call — get_account,
    get_all_positions, submit_order, get_order_by_id, cancel_order_by_id,
    get_orders — funnels through one choke point, `self._session.request()`
    (a bare `requests.Session()` with no timeout set; confirmed via
    inspect.getsource(RESTClient._one_request)). A stalled request there
    blocks the caller — and, on the live bot's single-thread scheduler
    executor, every subsequent scheduled job — forever. Patch the session's
    `request` to default one in, same fix class as the yfinance hang
    (market_data/yf_session.py).
    """
    session = client._session
    original_request = session.request

    def _request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", _ALPACA_TIMEOUT_SECONDS)
        return original_request(method, url, **kwargs)

    session.request = _request_with_timeout


class AlpacaBroker(BrokerInterface):
    """Alpaca paper trading client.

    Parameters
    ----------
    api_client : inject a TradingClient mock in tests. If None, credentials
                 are read from ALPACA_API_KEY / ALPACA_SECRET_KEY env vars.
    """

    def __init__(self, api_client: TradingClient | None = None) -> None:
        if api_client is not None:
            self._api = api_client
            self._is_paper = True  # injected client = always paper/test mode
        else:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
            base_url = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
            if not api_key or not secret_key:
                raise RuntimeError(
                    "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for Alpaca paper trading"
                )
            self._is_paper = "paper" in base_url
            self._api = TradingClient(api_key, secret_key, paper=self._is_paper)
            _apply_request_timeout(self._api)

    @property
    def is_paper(self) -> bool:
        return self._is_paper

    def get_cash(self) -> float:
        try:
            return float(self._api.get_account().cash)
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_account failed: {exc}") from exc

    def get_equity(self) -> float:
        try:
            return float(self._api.get_account().equity)
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_account failed: {exc}") from exc

    def shorting_enabled(self) -> bool:
        """Whether this Alpaca account is margin-enabled and can hold short positions.

        Must be verified once via a real (paper) account call before the
        short-selling flag is ever turned on — see
        docs/superpowers/specs/2026-07-17-short-selling-design.md Component 4.
        """
        try:
            return bool(getattr(self._api.get_account(), "shorting_enabled", False))
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_account failed: {exc}") from exc

    def is_shortable(self, ticker: str) -> bool:
        """Whether `ticker` can currently be shorted (shortable AND not hard-to-borrow)."""
        try:
            asset = self._api.get_asset(ticker.upper())
            return bool(getattr(asset, "shortable", False)) and bool(
                getattr(asset, "easy_to_borrow", False)
            )
        except Exception as exc:
            log.warning("is_shortable check failed for %s: %s", ticker, exc)
            return False

    def get_positions(self) -> list[dict]:
        try:
            return [
                {
                    "ticker": p.symbol,
                    "qty": float(p.qty),
                    "current_price": float(p.current_price),
                    "avg_entry_price": float(p.avg_entry_price),
                }
                for p in self._api.get_all_positions()
            ]
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_positions failed: {exc}") from exc

    def _poll_order_fill(self, order_id: str, attempts: int = 15, delay: float = 1.0):
        """Poll Alpaca for a terminal order status after submission.

        Returns the API order object once it reaches filled/cancelled/rejected,
        or None if still open after `attempts` polls or the API call fails.

        attempts=3/delay=0.2 (~0.4s total) was too short for Alpaca's paper API
        to confirm a market-order fill and caused CF/VTRS (2026-07-07) to be
        booked as phantom positions from a stale quote price. 15 attempts at
        1s apart (~14s) gives a real fill room to land before falling back.
        """
        import time
        for i in range(attempts):
            try:
                resp = self._api.get_order_by_id(order_id)
            except Exception as exc:
                log.warning("get_order_by_id(%s) failed: %s", order_id, exc)
                return None
            status_str = str(getattr(resp, "status", "")).lower()
            if "filled" in status_str or "cancel" in status_str or "reject" in status_str:
                return resp
            if i < attempts - 1:
                time.sleep(delay)
        return None

    def place_order(self, ticker: str, side: str, qty: float) -> Order:
        if side not in ("buy", "sell"):
            order = Order(ticker=ticker.upper(), side=side, qty=qty, order_type=OrderType.MARKET)
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"Invalid side: {side!r}"
            return order
        req = MarketOrderRequest(
            symbol=ticker.upper(),
            qty=qty,
            side=AlpacaSide.BUY if side == "buy" else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = Order(
            ticker=ticker.upper(),
            side=OrderSide(side),
            qty=qty,
            order_type=OrderType.MARKET,
        )
        try:
            submitted = self._api.submit_order(req)
            order.status = OrderStatus.SUBMITTED
            order.order_id = str(submitted.id)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            log.warning("Order rejected for %s %s %.4f: %s", side, ticker, qty, exc)
            return order

        filled = self._poll_order_fill(order.order_id)
        if filled is not None:
            status_str = str(getattr(filled, "status", "")).lower()
            if "filled" in status_str:
                order.status = OrderStatus.FILLED
                try:
                    order.filled_qty = float(filled.filled_qty or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    order.filled_avg_price = float(filled.filled_avg_price or 0)
                except (TypeError, ValueError):
                    pass
                filled_at = getattr(filled, "filled_at", None)
                if filled_at:
                    order.filled_at = str(filled_at)
            elif "cancel" in status_str:
                order.status = OrderStatus.CANCELLED
            elif "reject" in status_str:
                order.status = OrderStatus.REJECTED
                order.reject_reason = str(getattr(filled, "reject_reason", "") or "rejected by broker")
        else:
            # Poll exhausted its retries with no terminal status — the order is
            # still SUBMITTED (filled_qty/filled_avg_price stay at 0.0), so the
            # caller (Portfolio.open_position/close_position) falls back to the
            # pre-trade quote price. That fallback price may not match the real
            # fill, so flag it instead of letting it pass silently.
            emit_event(
                log, EventType.SLIPPAGE_HIGH,
                f"Fill poll timed out for {order.ticker} {order.side.value} order "
                f"{order.order_id} — falling back to pre-trade quote price, real "
                "fill price unconfirmed",
                data={"ticker": order.ticker, "order_id": order.order_id, "side": order.side.value},
                level=logging.WARNING,
                alert=True,
            )
        return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._api.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    def get_order_history(self) -> list[Order]:
        try:
            api_orders = self._api.get_orders()
        except Exception as exc:
            log.warning("get_order_history failed: %s", exc)
            return []

        history: list[Order] = []
        for o in api_orders:
            try:
                side = OrderSide.BUY if "buy" in str(o.side).lower() else OrderSide.SELL
                order_type = (
                    OrderType.LIMIT if "limit" in str(o.order_type).lower() else OrderType.MARKET
                )
                order = Order(
                    ticker=o.symbol,
                    side=side,
                    qty=float(o.qty),
                    order_type=order_type,
                    order_id=str(o.id),
                )
                status_str = str(o.status).lower()
                if "filled" in status_str:
                    order.status = OrderStatus.FILLED
                elif "cancel" in status_str:
                    order.status = OrderStatus.CANCELLED
                elif "reject" in status_str:
                    order.status = OrderStatus.REJECTED
                else:
                    order.status = OrderStatus.SUBMITTED
                order.filled_qty = float(o.filled_qty or 0)
                order.filled_avg_price = float(o.filled_avg_price or 0)
                filled_at = getattr(o, "filled_at", None)
                order.filled_at = str(filled_at) if filled_at else None
                history.append(order)
            except Exception as exc:
                log.warning("Skipping unparseable order in get_order_history: %s", exc)
        return history

    def place_stop_order(self, ticker: str, qty: float, stop_price: float,
                         side: str = "sell") -> str | None:
        """Submit a stop order to Alpaca. Returns order ID or None on failure.

        `side="sell"` (default) is a long position's stop-loss. `side="buy"` is
        a short position's stop (buy-to-cover if price rises against it).

        Alpaca rejects fractional-share orders with GTC ("fractional orders
        must be DAY orders") — NAV-based sizing routinely produces fractional
        qty, so this must switch to DAY whenever qty isn't a whole share.
        A DAY stop expires at end of session; enforce_stop_losses' trail-up
        poll re-places it each intraday check regardless (existing_stop reads
        back as 0.0 once expired), so overnight re-arming is already handled.
        """
        if side not in ("buy", "sell"):
            log.warning("Invalid side for place_stop_order: %r — expected 'buy' or 'sell'", side)
            return None

        from alpaca.trading.requests import StopOrderRequest
        tif = TimeInForce.DAY if qty != int(qty) else TimeInForce.GTC
        req = StopOrderRequest(
            symbol=ticker.upper(),
            qty=qty,
            side=AlpacaSide.BUY if side == "buy" else AlpacaSide.SELL,
            time_in_force=tif,
            stop_price=round(stop_price, 2),
        )
        try:
            submitted = self._api.submit_order(req)
            log.info("Stop order placed %s %s qty=%.4f @ $%.2f id=%s",
                     side, ticker, qty, stop_price, submitted.id)
            return str(submitted.id)
        except Exception as exc:
            log.warning("Failed to place stop order for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _is_open_stop(o) -> bool:
        """True if order ``o`` is an open (cancellable) resting STOP.

        alpaca-py returns enum instances, so ``str(o.order_type)`` is
        ``"OrderType.STOP"`` and ``str(o.status)`` is ``"OrderStatus.NEW"`` —
        NOT ``"stop"`` / ``"new"``. Compare on the substring case-insensitively
        so both real enums and any plain-string stand-ins match.
        """
        type_str = str(getattr(o, "order_type", "")).lower()
        status_str = str(getattr(o, "status", "")).lower()
        return "stop" in type_str and ("new" in status_str or "accepted" in status_str)

    def cancel_stop_order(self, ticker: str, order_id: str | None = None) -> None:
        """Cancel open stop order(s) for ticker.

        With an ``order_id``, cancel ONLY that specific resting stop — the
        trail-up path passes the OLD stop's id so the just-placed new (higher)
        stop is left resting. With no ``order_id``, cancel every open stop for
        the ticker (full close / reduce path, where no new stop was just placed).
        """
        try:
            for o in self._api.get_orders():
                if o.symbol != ticker.upper() or not self._is_open_stop(o):
                    continue
                if order_id is not None and str(o.id) != str(order_id):
                    continue
                self._api.cancel_order_by_id(str(o.id))
        except Exception as exc:
            log.warning("cancel_stop_order failed for %s: %s", ticker, exc)

    def get_stop_orders(self) -> dict[str, tuple[float, float, str | None]]:
        """Return {ticker: (stop_price, qty, order_id)} for open stop orders.

        If a ticker has more than one open stop (e.g. transiently during a
        trail-up), the last one returned by the API wins.
        """
        result: dict[str, tuple[float, float, str | None]] = {}
        try:
            for o in self._api.get_orders():
                if self._is_open_stop(o):
                    result[o.symbol] = (float(o.stop_price or 0), float(o.qty), str(o.id))
        except Exception as exc:
            log.warning("get_stop_orders failed: %s", exc)
        return result
