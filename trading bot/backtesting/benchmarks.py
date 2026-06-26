"""Benchmark strategies for backtest comparison.

Three benchmarks:
1. Buy-and-hold SPY
2. Simple trend-following (long when price > 200-day MA, else cash)
3. Randomised allocation baseline (same entry frequency, random timing)

All benchmarks accept slippage_bps and commission_pct so the cost
structure matches the strategy under test. Comparisons are only meaningful
when costs are applied consistently.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _apply_entry_cost(cash: float, price: float, slippage_bps: float,
                      commission_pct: float) -> tuple[float, float]:
    """Return (fill_price, shares) after applying entry slippage and commission."""
    fill = price * (1 + slippage_bps / 10_000)
    alloc = cash / fill
    cost = alloc * fill * commission_pct / 100
    shares = (cash - cost) / fill
    return fill, shares


def _apply_exit_cost(shares: float, price: float, slippage_bps: float,
                     commission_pct: float) -> float:
    """Return cash proceeds after applying exit slippage and commission."""
    fill = price * (1 - slippage_bps / 10_000)
    gross = shares * fill
    commission = gross * commission_pct / 100
    return gross - commission


def buy_and_hold(
    price_series: pd.Series,
    initial_cash: float = 100_000.0,
    slippage_bps: float = 0.0,
    commission_pct: float = 0.0,
) -> pd.Series:
    """Buy at first available close; hold until the end."""
    if price_series.empty:
        return pd.Series(dtype=float)
    entry_price = float(price_series.iloc[0])
    _, shares = _apply_entry_cost(initial_cash, entry_price, slippage_bps, commission_pct)
    equity = price_series * shares
    equity.name = "buy_and_hold"
    return equity


def trend_following(
    price_series: pd.Series,
    ma_window: int = 200,
    initial_cash: float = 100_000.0,
    slippage_bps: float = 0.0,
    commission_pct: float = 0.0,
) -> pd.Series:
    """Long when close > MA, else stay in cash (no short).

    Slippage and commission are applied on each in/out switch.
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
            _, position_shares = _apply_entry_cost(cash, price, slippage_bps, commission_pct)
            cash = 0.0
        elif not signal and prev_in_market and position_shares > 0:
            cash = _apply_exit_cost(position_shares, price, slippage_bps, commission_pct)
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
    slippage_bps: float = 0.0,
    commission_pct: float = 0.0,
) -> pd.Series:
    """Randomly time the same number of entries as the strategy, same hold period.

    Provides a baseline: if the strategy can't beat random entry timing with
    the same risk parameters, the alpha is not from signal selection.
    Slippage and commission are applied on open and close.
    """
    rng = np.random.default_rng(seed)
    if price_series.empty or n_signals == 0:
        return pd.Series(dtype=float)

    dates = list(price_series.index)
    entry_indices = sorted(rng.choice(len(dates), size=min(n_signals, len(dates)), replace=False))

    cash = initial_cash
    positions: dict[int, dict] = {}  # exit_index → {shares, entry_price}

    equity = pd.Series(0.0, index=price_series.index)

    for i, dt in enumerate(dates):
        price = float(price_series.iloc[i])

        # Close positions that expire today
        to_close = [eidx for eidx in positions if eidx <= i]
        for eidx in to_close:
            p = positions.pop(eidx)
            cash += _apply_exit_cost(p["shares"], price, slippage_bps, commission_pct)

        # Open new position if this is a signal date and we have cash
        if i in entry_indices and cash > 0:
            alloc = cash * position_pct / 100
            # Commission is an explicit separate cost on top of the stock purchase.
            # shares = alloc / fill so the full allocation buys stock; commission is
            # NOT embedded in the share count (which would reduce shares and charge
            # a smaller exit commission later, double-penalising the benchmark).
            fill_price = price * (1 + slippage_bps / 10_000)
            commission = alloc * commission_pct / 100
            shares = alloc / fill_price
            total_cost = alloc + commission
            if total_cost > cash:
                continue
            cash -= total_cost
            exit_idx = min(i + avg_hold_days, len(dates) - 1)
            positions[exit_idx] = {"shares": shares, "entry_price": price}

        # Mark-to-market all open positions
        open_val = sum(p["shares"] * price for p in positions.values())
        equity.iloc[i] = cash + open_val

    equity.name = "random_allocation"
    return equity
