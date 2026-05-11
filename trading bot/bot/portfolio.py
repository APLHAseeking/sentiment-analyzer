from datetime import date
import bot.db as db

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
                      signal_source: str = "congressional") -> None:
        position_pct = min(position_pct, self._risk.max_position_pct)
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
            signal_source=signal_source,
        )
        self._opened_today += 1

    def close_position(self, ticker: str, shares: float, exit_price: float,
                       exit_reason: str, signal_id: int | None, entry_price: float,
                       entry_date: str, signal_source: str = "congressional") -> None:
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
            signal_source=signal_source,
        )
        db.delete_position(ticker)

    def reduce_position(self, ticker: str, shares: float, exit_price: float,
                        signal_id: int | None, entry_price: float, entry_date: str,
                        signal_source: str = "congressional") -> None:
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
            signal_source=signal_source,
        )
        db.update_position_shares(ticker, shares - sell_qty)

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
            source = meta.get("signal_source", "congressional")

            if source_include is not None and source != source_include:
                continue
            if source_exclude is not None and source == source_exclude:
                continue

            peak = meta.get("peak_price") or pos["avg_entry_price"]
            db.update_position_peak(ticker, current)
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
        source_exclude: str | None = None,
    ) -> list[str]:
        pct = take_profit_pct if take_profit_pct is not None else self._risk.take_profit_pct
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

            entry = pos["avg_entry_price"]
            current = pos["current_price"]
            gain_pct = (current - entry) / entry * 100
            if gain_pct >= pct:
                self.reduce_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    signal_id=meta.get("signal_id"),
                    entry_price=meta.get("entry_price") or entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=meta.get("signal_source", "congressional"),
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
