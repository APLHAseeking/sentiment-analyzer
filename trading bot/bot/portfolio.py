from datetime import date
import bot.db as db

MAX_POSITIONS = 20
MAX_POSITIONS_PER_DAY = 3
MAX_POSITION_PCT = 8.0

class Portfolio:
    def __init__(self, broker):
        self.broker = broker
        self._opened_today = 0

    def get_cash(self) -> float:
        return self.broker.get_cash()

    def can_open_new_position(self) -> bool:
        if len(self.broker.get_positions()) >= MAX_POSITIONS:
            return False
        if self._opened_today >= MAX_POSITIONS_PER_DAY:
            return False
        return True

    def reset_daily_counter(self) -> None:
        self._opened_today = 0

    def open_position(self, ticker: str, position_pct: float, signal_id: int,
                      rationale: str, entry_price: float) -> None:
        position_pct = min(position_pct, MAX_POSITION_PCT)
        shares = (self.get_cash() * position_pct / 100) / entry_price
        self.broker.place_order(ticker=ticker, side="buy", qty=shares)
        db.insert_position(
            ticker=ticker,
            entry_price=entry_price,
            shares=shares,
            position_pct=position_pct,
            entry_date=date.today().isoformat(),
            signal_id=signal_id,
            rationale=rationale,
        )
        self._opened_today += 1

    def close_position(self, ticker: str, shares: float) -> None:
        self.broker.place_order(ticker=ticker, side="sell", qty=shares)
        db.delete_position(ticker)

    def reduce_position(self, ticker: str, shares: float) -> None:
        self.broker.place_order(ticker=ticker, side="sell", qty=shares / 2)
        db.update_position_shares(ticker, shares / 2)

    def enforce_stop_losses(self, stop_loss_pct: float = 15.0) -> list[str]:
        closed = []
        for pos in self.broker.get_positions():
            loss_pct = (pos["avg_entry_price"] - pos["current_price"]) / pos["avg_entry_price"] * 100
            if loss_pct >= stop_loss_pct:
                self.broker.place_order(ticker=pos["ticker"], side="sell", qty=pos["qty"])
                db.delete_position(pos["ticker"])
                closed.append(pos["ticker"])
        return closed

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
