"""SimulatedBroker — fully offline paper execution.

No API keys required. Fills market orders instantly at the provided price
plus configurable slippage. Suitable for backtesting and local testing.

AlpacaBroker is preserved in bot/broker.py but is clearly separated;
this module is the recommended execution backend for research and testing.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

import yfinance as yf

from execution.broker_interface import (
    BrokerInterface, Order, OrderSide, OrderStatus, OrderType, Position
)

log = logging.getLogger(__name__)


class SimulatedBroker(BrokerInterface):
    """In-memory paper broker with realistic slippage and fee simulation.

    State is not persisted across restarts — use for backtesting and unit
    tests. For day-to-day paper trading with state persistence, use the
    Alpaca paper API via bot/broker.py.
    """

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        slippage_bps: float = 5.0,
        commission_per_share: float = 0.0,
    ) -> None:
        self._cash = initial_cash
        self._slippage_bps = slippage_bps
        self._commission_per_share = commission_per_share
        # ticker → Position
        self._positions: dict[str, Position] = {}
        # full order history
        self._orders: list[Order] = []
        # resting stop orders: ticker → {order_id: (stop_price, qty)}.
        # A ticker may hold more than one resting stop transiently during a
        # trail-up (new placed before old is cancelled), so each stop has its
        # own id and they don't overwrite each other.
        self._stop_orders: dict[str, dict[str, tuple[float, float]]] = {}
        self._stop_seq = 0  # monotonic counter for unique simulated stop ids

    # ------------------------------------------------------------------
    # BrokerInterface implementation
    # ------------------------------------------------------------------

    @property
    def is_paper(self) -> bool:
        return True

    def get_commission_per_share(self) -> float:
        return self._commission_per_share

    def get_cash(self) -> float:
        return self._cash

    def get_equity(self) -> float:
        pos_value = sum(p.market_value for p in self._positions.values())
        return self._cash + pos_value

    def get_positions(self) -> list[dict[str, Any]]:
        """Return current positions using in-memory prices (no live API calls).

        Call refresh_prices() first if you need up-to-date market values.
        """
        return [
            {
                "ticker": pos.ticker,
                "qty": pos.qty,
                "current_price": pos.current_price,
                "avg_entry_price": pos.avg_entry_price,
            }
            for pos in self._positions.values()
        ]

    def refresh_prices(self) -> None:
        """Fetch current market prices for all held positions via yfinance.

        Call once per pipeline run rather than implicitly on every get_positions().
        """
        for pos in self._positions.values():
            try:
                price = yf.Ticker(pos.ticker).fast_info.last_price
                if price:
                    pos.current_price = float(price)
            except Exception:
                pass

    def place_order(self, ticker: str, side: str, qty: float) -> Order:
        order = Order(
            ticker=ticker.upper(),
            side=OrderSide(side),
            qty=qty,
            order_type=OrderType.MARKET,
        )
        try:
            fill_price = self._get_fill_price(ticker, side)
            self._apply_fill(order, fill_price)
            log.info(
                "SimulatedBroker fill: %s %s %.4f @ $%.2f",
                side.upper(), ticker, qty, fill_price,
            )
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            log.warning("SimulatedBroker order rejected: %s", exc)
        self._orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        for order in self._orders:
            if order.order_id == order_id and order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED
                return True
        return False

    def get_order_history(self) -> list[Order]:
        return list(self._orders)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_fill_price(self, ticker: str, side: str) -> float:
        """Fetch market price and apply directional slippage."""
        try:
            price = yf.Ticker(ticker).fast_info.last_price
        except Exception:
            price = None
        if not price:
            raise RuntimeError(f"No market price available for {ticker}")
        price = float(price)
        slip = price * self._slippage_bps / 10_000
        return price + slip if side == "buy" else price - slip

    def _apply_fill(self, order: Order, fill_price: float) -> None:
        ticker = order.ticker
        qty = order.qty
        commission = qty * self._commission_per_share
        filled_qty = qty  # default; overwritten for partial sells

        if order.side == OrderSide.BUY:
            cost = fill_price * qty + commission
            if cost > self._cash:
                raise RuntimeError(
                    f"Insufficient buying power: need ${cost:.2f}, have ${self._cash:.2f}"
                )
            self._cash -= cost
            if ticker in self._positions:
                pos = self._positions[ticker]
                total_qty = pos.qty + qty
                pos.avg_entry_price = (
                    (pos.avg_entry_price * pos.qty + fill_price * qty) / total_qty
                )
                pos.qty = total_qty
                pos.current_price = fill_price
            else:
                self._positions[ticker] = Position(
                    ticker=ticker,
                    qty=qty,
                    avg_entry_price=fill_price,
                    current_price=fill_price,
                )
        else:  # SELL
            if ticker not in self._positions:
                raise RuntimeError(f"Cannot sell {ticker}: no position held")
            pos = self._positions[ticker]
            filled_qty = min(qty, pos.qty)     # actual shares sold (may be < requested)
            proceeds = fill_price * filled_qty - commission
            self._cash += proceeds
            pos.qty -= filled_qty
            pos.current_price = fill_price
            if pos.qty <= 1e-6:
                del self._positions[ticker]

        order.status = OrderStatus.FILLED
        order.filled_qty = filled_qty
        order.filled_avg_price = fill_price
        order.filled_at = datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # Stop order simulation
    # ------------------------------------------------------------------

    def place_stop_order(self, ticker: str, qty: float, stop_price: float,
                         side: str = "sell") -> str | None:
        """Register a resting stop. The polled enforce_stop_losses() does the actual fill.

        Returns a unique order id. Multiple stops can rest for the same ticker
        at once (e.g. a new trail-up stop placed before the old one is cancelled);
        each is keyed by its own id and does not overwrite the others.

        `side` is accepted for interface parity with AlpacaBroker (a short's stop
        is a buy-to-cover) but not stored — this simulated broker's stop registry
        is a passive record consumed by Portfolio's own direction-aware polling
        (bot.direction_math), not a side-aware fill simulator.
        """
        self._stop_seq += 1
        order_id = f"sim-stop-{ticker}-{self._stop_seq}"
        self._stop_orders.setdefault(ticker, {})[order_id] = (stop_price, qty)
        log.info("SimulatedBroker: stop registered %s qty=%.4f @ $%.2f id=%s",
                 ticker, qty, stop_price, order_id)
        return order_id

    def cancel_stop_order(self, ticker: str, order_id: str | None = None) -> None:
        """Cancel resting stop(s) for ticker.

        With an ``order_id``, cancel only that specific stop (the trail-up path
        cancels exactly the OLD stop by id). With no ``order_id``, cancel every
        resting stop for the ticker (full close / reduce path). No-op if none
        match.
        """
        if order_id is None:
            self._stop_orders.pop(ticker, None)
            return
        stops = self._stop_orders.get(ticker)
        if stops is not None:
            stops.pop(order_id, None)
            if not stops:
                self._stop_orders.pop(ticker, None)

    def get_stop_orders(self) -> dict[str, tuple[float, float, str | None]]:
        """Return {ticker: (stop_price, qty, order_id)} for resting stops.

        If a ticker has more than one resting stop in flight, the most recently
        placed one is reported (its id is the highest in insertion order).
        """
        result: dict[str, tuple[float, float, str | None]] = {}
        for ticker, stops in self._stop_orders.items():
            if not stops:
                continue
            order_id, (stop_price, qty) = next(reversed(stops.items()))
            result[ticker] = (stop_price, qty, order_id)
        return result

    # ------------------------------------------------------------------
    # Extra simulation utilities
    # ------------------------------------------------------------------

    def set_position_price(self, ticker: str, price: float) -> None:
        """Update current price for backtesting bar-by-bar simulation."""
        if ticker in self._positions:
            self._positions[ticker].current_price = price

    def snapshot(self) -> dict[str, Any]:
        return {
            "cash": self._cash,
            "equity": self.get_equity(),
            "positions": self.get_positions(),
            "n_orders": len(self._orders),
        }
