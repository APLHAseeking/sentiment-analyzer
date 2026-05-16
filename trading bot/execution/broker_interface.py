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

    @property
    @abstractmethod
    def is_paper(self) -> bool: ...
