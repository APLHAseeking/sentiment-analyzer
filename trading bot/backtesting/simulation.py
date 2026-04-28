"""Portfolio simulation engine for walk-forward backtesting.

Simulates the full regime-aware trading system on historical data with
realistic slippage, transaction costs, and position tracking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class SimTrade:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    pnl_pct: float
    regime_at_entry: str
    conviction: int
    exit_reason: str


@dataclass
class SimState:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)


def _apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    """Apply directional slippage to a fill price."""
    slip = price * slippage_bps / 10_000
    return price + slip if side == "buy" else price - slip


def _apply_commission(value: float, commission_pct: float) -> float:
    return value * commission_pct / 100


def simulate_portfolio(
    signals: list[dict],        # list of {date, ticker, conviction, position_pct, regime_label}
    price_data: dict[str, pd.Series],  # ticker → daily close price series
    initial_cash: float = 100_000.0,
    slippage_bps: float = 10.0,
    commission_pct: float = 0.05,
    max_positions: int = 20,
    max_position_pct: float = 8.0,
    trailing_stop_pct: float = 15.0,
    take_profit_pct: float = 25.0,
) -> SimState:
    """Simulate the portfolio day-by-day through the provided signal list.

    Parameters
    ----------
    signals : sorted by date ascending. Each dict must have:
              date (YYYY-MM-DD), ticker, conviction, position_pct, regime_label.
    price_data : maps ticker → pd.Series of daily close prices (DatetimeIndex).
    """
    state = SimState(cash=initial_cash)
    signal_by_date: dict[str, list[dict]] = {}
    for sig in signals:
        signal_by_date.setdefault(sig["date"], []).append(sig)

    # Determine date range from price_data
    all_dates = sorted({str(d.date()) for series in price_data.values()
                        for d in series.index})

    peak_prices: dict[str, float] = {}

    for day_str in all_dates:
        day_signals = signal_by_date.get(day_str, [])

        # --- Update prices and check stops/profits -----------------------
        closed_today = []
        for ticker, pos in list(state.positions.items()):
            series = price_data.get(ticker)
            if series is None:
                continue
            try:
                current_price = float(series.loc[day_str])
            except (KeyError, TypeError):
                continue

            # Read old peak BEFORE updating — so today's new price doesn't hide a drop
            peak = peak_prices.get(ticker, pos["entry_price"])
            peak_prices[ticker] = max(peak, current_price)

            # Trailing stop — uses peak from PREVIOUS high, not today's price
            drop_from_peak = (peak - current_price) / peak * 100
            if drop_from_peak >= trailing_stop_pct:
                _close_position(state, ticker, current_price, day_str,
                                "stop_loss", slippage_bps, commission_pct, peak_prices)
                closed_today.append(ticker)
                continue

            # Take profit
            gain = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            if gain >= take_profit_pct:
                _close_position(state, ticker, current_price, day_str,
                                "take_profit", slippage_bps, commission_pct, peak_prices)
                closed_today.append(ticker)

        # --- Open new positions ------------------------------------------
        for sig in day_signals:
            ticker = sig["ticker"]
            if ticker in state.positions:
                continue
            if len(state.positions) >= max_positions:
                continue
            series = price_data.get(ticker)
            if series is None:
                continue
            try:
                price = float(series.loc[day_str])
            except (KeyError, TypeError):
                continue

            position_pct = min(float(sig.get("position_pct", 5.0)), max_position_pct)
            fill_price = _apply_slippage(price, "buy", slippage_bps)
            cost_basis = state.cash * position_pct / 100
            commission = _apply_commission(cost_basis, commission_pct)
            total_cost = cost_basis + commission
            if total_cost > state.cash:
                continue

            shares = cost_basis / fill_price
            state.cash -= total_cost
            state.positions[ticker] = {
                "ticker": ticker,
                "entry_date": day_str,
                "entry_price": fill_price,
                "shares": shares,
                "position_pct": position_pct,
                "regime_at_entry": sig.get("regime_label", "unknown"),
                "conviction": sig.get("conviction", 5),
            }
            peak_prices[ticker] = fill_price

        # --- Daily equity snapshot ---------------------------------------
        pos_value = 0.0
        for ticker, pos in state.positions.items():
            series = price_data.get(ticker)
            if series is None:
                continue
            try:
                pos_value += pos["shares"] * float(series.loc[day_str])
            except (KeyError, TypeError):
                pos_value += pos["shares"] * pos["entry_price"]

        state.equity_curve.append((day_str, state.cash + pos_value))

    # Close all open positions at end of simulation
    last_date = all_dates[-1] if all_dates else date.today().isoformat()
    for ticker in list(state.positions.keys()):
        series = price_data.get(ticker)
        price = (float(series.iloc[-1]) if series is not None and not series.empty
                 else state.positions[ticker]["entry_price"])
        _close_position(state, ticker, price, last_date, "end_of_backtest",
                        slippage_bps, commission_pct, peak_prices)

    return state


def _close_position(
    state: SimState,
    ticker: str,
    current_price: float,
    exit_date: str,
    exit_reason: str,
    slippage_bps: float,
    commission_pct: float,
    peak_prices: dict,
) -> None:
    if ticker not in state.positions:
        return
    pos = state.positions.pop(ticker)
    fill_price = _apply_slippage(current_price, "sell", slippage_bps)
    gross = pos["shares"] * fill_price
    commission = _apply_commission(gross, commission_pct)
    proceeds = gross - commission
    state.cash += proceeds

    entry_value = pos["shares"] * pos["entry_price"]
    pnl = proceeds - entry_value
    pnl_pct = pnl / entry_value * 100 if entry_value > 0 else 0.0

    state.trades.append(SimTrade(
        ticker=ticker,
        entry_date=pos["entry_date"],
        exit_date=exit_date,
        entry_price=pos["entry_price"],
        exit_price=fill_price,
        shares=pos["shares"],
        pnl=pnl,
        pnl_pct=pnl_pct,
        regime_at_entry=pos.get("regime_at_entry", "unknown"),
        conviction=pos.get("conviction", 0),
        exit_reason=exit_reason,
    ))
    peak_prices.pop(ticker, None)


def equity_series(state: SimState) -> pd.Series:
    """Convert the equity curve list to an indexed Series."""
    if not state.equity_curve:
        return pd.Series(dtype=float)
    dates, values = zip(*state.equity_curve)
    return pd.Series(values, index=pd.to_datetime(dates), name="equity")


def trade_returns(state: SimState) -> pd.Series:
    """Return per-trade P&L% as a Series."""
    if not state.trades:
        return pd.Series(dtype=float)
    return pd.Series([t.pnl_pct / 100 for t in state.trades], name="trade_return")
