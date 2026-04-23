from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from bot.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

_api: TradingClient | None = None


def _get_api() -> TradingClient:
    global _api
    if _api is None:
        paper = "paper" in ALPACA_BASE_URL
        _api = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
    return _api


class AlpacaBroker:
    def __init__(self):
        self._api = _get_api()

    def get_cash(self) -> float:
        try:
            return float(self._api.get_account().cash)
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

    def place_order(self, ticker: str, side: str, qty: float) -> None:
        if side == "buy":
            order_side = OrderSide.BUY
        elif side == "sell":
            order_side = OrderSide.SELL
        else:
            raise ValueError(f"Invalid side: {side!r}. Expected 'buy' or 'sell'.")
        order = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            self._api.submit_order(order)
        except Exception as exc:
            raise RuntimeError(f"Alpaca submit_order failed for {ticker} {side}: {exc}") from exc
