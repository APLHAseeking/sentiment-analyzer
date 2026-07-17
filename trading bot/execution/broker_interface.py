"""Abstract broker interface shared by all execution backends.

New backends (SimulatedBroker, AlpacaBroker) implement this protocol.
The rest of the system depends only on this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class Order:
    ticker: str
    side: OrderSide
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    order_id: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    submitted_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    filled_at: str | None = None
    reject_reason: str | None = None


@dataclass
class Position:
    ticker: str
    qty: float
    avg_entry_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_entry_price) * self.qty

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        return (self.current_price - self.avg_entry_price) / self.avg_entry_price * 100


class BrokerInterface(ABC):
    """All broker backends implement this interface."""

    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_equity(self) -> float: ...

    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]:
        """Return list of dicts with keys: ticker, qty, current_price, avg_entry_price."""
        ...

    @abstractmethod
    def place_order(self, ticker: str, side: str, qty: float) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_order_history(self) -> list[Order]: ...

    def get_commission_per_share(self) -> float:
        """Return per-share commission. Override in implementations."""
        return 0.0

    def place_stop_order(self, ticker: str, qty: float, stop_price: float,
                         side: str = "sell") -> str | None:
        """Place a resting stop order. Returns an order ID or None (no-op default).

        Concrete brokers that support native stop orders should override this.
        The polled enforce_stop_losses() in Portfolio acts as the backstop.

        `side="sell"` (default) is a long position's stop-loss; `side="buy"` is
        a short position's stop (buy-to-cover).
        """
        return None

    def cancel_stop_order(self, ticker: str, order_id: str | None = None) -> None:
        """Cancel a resting stop order.

        When ``order_id`` is given, cancel only that specific stop order — used
        by the trail-up path, which places the new (higher) stop first and must
        then cancel exactly the OLD stop by id, not every stop for the ticker
        (otherwise it would cancel the just-placed new one too). When
        ``order_id`` is None, cancel whatever stops are resting for the ticker —
        used on full close / reduce, where no new stop was just placed.

        No-op default. Concrete brokers with native stops override this.
        """
        return None

    def get_stop_orders(self) -> dict[str, tuple[float, float, str | None]]:
        """Return ``{ticker: (stop_price, qty, order_id)}`` for resting stops.

        ``order_id`` identifies the specific resting stop so the trail-up path
        can capture the OLD stop's id before placing the new one and then cancel
        exactly that order. If a ticker has more than one resting stop in flight
        (transiently, mid-trail-up), the most recently placed one is reported.
        No-op default returns an empty mapping.
        """
        return {}

    @property
    @abstractmethod
    def is_paper(self) -> bool: ...
