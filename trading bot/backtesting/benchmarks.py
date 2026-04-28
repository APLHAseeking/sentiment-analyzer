"""Benchmark strategies for backtest comparison.

Three benchmarks:
1. Buy-and-hold SPY
2. Simple trend-following (long when price > 200-day MA, else cash)
3. Randomised allocation baseline (same entry frequency, random timing)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def buy_and_hold(price_series: pd.Series, initial_cash: float = 100_000.0) -> pd.Series:
    """Buy at first available close; hold until the end.

    Returns an equity curve Series aligned to price_series.index.
    """
    if price_series.empty:
        return pd.Series(dtype=float)
    entry_price = float(price_series.iloc[0])
    shares = initial_cash / entry_price
    equity = price_series * shares
    equity.name = "buy_and_hold"
    return equity


def trend_following(
    price_series: pd.Series,
    ma_window: int = 200,
    initial_cash: float = 100_000.0,
) -> pd.Series:
    """Long when close > MA, else stay in cash (no short).

    Returns an equity curve Series.
    """
    if len(price_series) < ma_window:
        return pd.Series(dtype=float)

    ma = price_series.rolling(ma_window, min_periods=ma_window // 2).mean()
    in_market = price_series > ma

    equity = pd.Series(index=price_series.index, dtype=float)
    cash = initial_cash
    position_shares = 0.0
    prev_in_market = False

    for dt, price in price_series.items():
        signal = bool(in_market.get(dt, False))
        if signal and not prev_in_market and cash > 0:
            position_shares = cash / price
            cash = 0.0
        elif not signal and prev_in_market and position_shares > 0:
            cash = position_shares * price
            position_shares = 0.0
        prev_in_market = signal
        equity[dt] = cash + position_shares * price

    equity.name = "trend_following"
    return equity


def random_allocation(
    price_series: pd.Series,
    n_signals: int,
    avg_hold_days: int = 20,
    position_pct: float = 5.0,
    initial_cash: float = 100_000.0,
    seed: int = 42,
) -> pd.Series:
    """Randomly time the same number of entries as the strategy, same hold period.

    Provides a baseline: if the strategy can't beat random entry timing with
    the same risk parameters, the alpha is not from signal selection.
    """
    rng = np.random.default_rng(seed)
    if price_series.empty or n_signals == 0:
        return pd.Series(dtype=float)

    dates = list(price_series.index)
    entry_indices = sorted(rng.choice(len(dates), size=min(n_signals, len(dates)), replace=False))

    cash = initial_cash
    positions: dict[int, dict] = {}  # exit_index → position_value_at_exit

    equity = pd.Series(0.0, index=price_series.index)
    pos_value = 0.0

    for i, dt in enumerate(dates):
        price = float(price_series.iloc[i])

        # Close positions that expire today
        to_close = [eidx for eidx in positions if eidx <= i]
        for eidx in to_close:
            p = positions.pop(eidx)
            exit_price = price
            proceeds = p["shares"] * exit_price
            cash += proceeds
            pos_value -= p["shares"] * float(price_series.iloc[p["entry_idx"]])

        # Open new position if this is a signal date and we have cash
        if i in entry_indices and cash > 0:
            alloc = cash * position_pct / 100
            shares = alloc / price
            cash -= alloc
            exit_idx = min(i + avg_hold_days, len(dates) - 1)
            positions[exit_idx] = {"shares": shares, "entry_idx": i}

        # Mark-to-market all open positions
        open_val = sum(p["shares"] * price for p in positions.values())
        equity.iloc[i] = cash + open_val

    equity.name = "random_allocation"
    return equity
