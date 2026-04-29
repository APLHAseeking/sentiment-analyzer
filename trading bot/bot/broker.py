"""Alpaca paper trading broker — implements BrokerInterface.

Pass `api_client` in tests to inject a mock and avoid network calls.
"""
from __future__ import annotations

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce

from execution.broker_interface import (
    BrokerInterface, Order, OrderSide, OrderStatus, OrderType,
)


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

    def place_order(self, ticker: str, side: str, qty: float) -> Order:
        if side not in ("buy", "sell"):
            order = Order(ticker=ticker.upper(), side=OrderSide("buy"), qty=qty, order_type=OrderType.MARKET)
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
            self._api.submit_order(req)
            order.status = OrderStatus.PENDING
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
        return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._api.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    def get_order_history(self) -> list[Order]:
        return []
