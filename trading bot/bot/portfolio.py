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

    def close_position(self, ticker: str, shares: float, exit_price: float,
                       exit_reason: str, signal_id: int, entry_price: float,
                       entry_date: str) -> None:
        self.broker.place_order(ticker=ticker, side="sell", qty=shares)
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=shares,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason=exit_reason,
            signal_id=signal_id,
        )
        db.delete_position(ticker)

    def reduce_position(self, ticker: str, shares: float, exit_price: float,
                        signal_id: int, entry_price: float, entry_date: str) -> None:
        sell_qty = shares / 2
        self.broker.place_order(ticker=ticker, side="sell", qty=sell_qty)
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=sell_qty,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason="reduce",
            signal_id=signal_id,
        )
        db.update_position_shares(ticker, shares - sell_qty)

    def enforce_stop_losses(self, stop_loss_pct: float = 15.0) -> list[str]:
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            current = pos["current_price"]
            meta = open_positions.get(ticker, {})
            peak = meta.get("peak_price") or pos["avg_entry_price"]

            db.update_position_peak(ticker, current)

            drop_from_peak = (peak - current) / peak * 100
            if drop_from_peak >= stop_loss_pct:
                self.close_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    exit_reason="stop_loss",
                    signal_id=meta.get("signal_id") or 0,
                    entry_price=meta.get("entry_price") or pos["avg_entry_price"],
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                )
                closed.append(ticker)
        return closed

    def enforce_take_profits(self, take_profit_pct: float = 25.0) -> list[str]:
        reduced = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            entry = pos["avg_entry_price"]
            current = pos["current_price"]
            gain_pct = (current - entry) / entry * 100

            if gain_pct >= take_profit_pct:
                meta = open_positions.get(ticker, {})
                self.reduce_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    signal_id=meta.get("signal_id") or 0,
                    entry_price=meta.get("entry_price") or entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                )
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
