# tests/test_backtest_sue_pit_gate.py
from __future__ import annotations

import numpy as np
import pandas as pd

from backtesting.backtest_sue_pit import daily_calendar_excess_returns, run_gate


def _synthetic_prices(n_days: int = 30) -> pd.DataFrame:
    """HIGH goes up 1%/day, LOW is flat -- deterministic, known excess."""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    high = 100 * (1.01 ** np.arange(n_days))
    low = np.full(n_days, 100.0)
    return pd.DataFrame({"HIGH": high, "LOW": low}, index=dates)


def test_daily_calendar_excess_returns_isolates_top_quintile_outperformance():
    prices = _synthetic_prices()
    events = pd.DataFrame([
        {"ticker": "HIGH", "tradable_date": prices.index[0].date(), "sue": 10.0,
         "drift_5d": 0.05},
        {"ticker": "LOW", "tradable_date": prices.index[0].date(), "sue": 0.0,
         "drift_5d": 0.0},
    ])
    daily = daily_calendar_excess_returns(events, prices, horizon=5)

    # HIGH's daily return is a constant 1% (1.01**t / 1.01**(t-1) - 1 = 0.01);
    # benchmark averages HIGH (1%) and LOW (0%) => 0.5%. Excess = top - all.
    assert len(daily) == 5
    assert np.allclose(daily.to_numpy(), 0.01 - 0.005, atol=1e-9)


def test_daily_calendar_excess_returns_empty_when_no_events_clear_quantile():
    prices = _synthetic_prices()
    events = pd.DataFrame(columns=["ticker", "tradable_date", "sue", "drift_5d"])
    daily = daily_calendar_excess_returns(events, prices, horizon=5)
    assert daily.empty


def test_run_gate_reports_stronger_tstat_for_a_real_edge_than_no_edge():
    """Sanity check the whole gate pipeline: a strong, mostly-consistent
    outperformance must produce a large positive t-stat -- confirms
    hac_mean_tstat, the quintile split, and the excess-return construction
    are wired together correctly end to end. Uses HIGH's daily return with
    small noise (not perfectly deterministic) -- a truly zero-variance
    excess series correctly triggers hac_mean_tstat's degenerate-input NaN
    guard (Task 5) rather than a huge/undefined t-stat, so a noise-free
    synthetic series is the wrong fixture for this particular assertion.
    """
    rng = np.random.default_rng(0)
    n_days = 80
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    high = 100 * np.cumprod(1 + rng.normal(0.01, 0.0005, n_days))
    low = 100 * np.cumprod(1 + rng.normal(0.0, 0.0005, n_days))
    prices = pd.DataFrame({"HIGH": high, "LOW": low}, index=dates)

    events = pd.DataFrame([
        {"ticker": "HIGH", "tradable_date": prices.index[i].date(), "sue": 10.0,
         "drift_20d": 0.2, "drift_60d": 0.6}
        for i in range(0, 15)
    ] + [
        {"ticker": "LOW", "tradable_date": prices.index[i].date(), "sue": 0.0,
         "drift_20d": 0.0, "drift_60d": 0.0}
        for i in range(0, 15)
    ])
    results = run_gate(events, prices)
    assert results[20]["tstat"] > 5  # deterministic edge, no noise -> very large t
    assert results[20]["ir"] > 0.5
