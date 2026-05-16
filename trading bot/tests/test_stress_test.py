"""Tests for backtesting.stress_test scenario generation and transforms."""
import numpy as np
import pandas as pd
import pytest


def _price_series(n=50, start=100.0, daily_ret=0.001):
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    prices = [start * (1 + daily_ret) ** i for i in range(n)]
    return pd.Series(prices, index=dates, name="SPY")


def test_sudden_crash_factory_fields():
    from backtesting.stress_test import sudden_crash
    s = sudden_crash(drop_pct=0.20, duration_days=3)
    assert s.name == "sudden_crash"
    assert s.crash_pct == pytest.approx(0.20)
    assert s.crash_duration_days == 3


def test_crash_reduces_tail_prices():
    from backtesting.stress_test import _apply_crash
    series = _price_series(30)
    result = _apply_crash({"SPY": series}, drop_pct=0.30, start_idx=5, duration=5)
    # Tail prices should be ~70% of where they would have been
    assert result["SPY"].iloc[-1] < series.iloc[-1] * 0.90


def test_vol_cluster_increases_return_variance():
    from backtesting.stress_test import _apply_vol_cluster
    rng = np.random.default_rng(0)
    n = 80
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    prices = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=dates)
    result = _apply_vol_cluster({"SPY": prices}, multiplier=3.0, start_idx=20, duration=30)
    original_std = prices.pct_change().iloc[20:50].std()
    stressed_std = result["SPY"].pct_change().iloc[20:50].std()
    assert stressed_std > original_std * 1.5


def test_drop_random_bars_reduces_length():
    from backtesting.stress_test import _drop_random_bars
    series = _price_series(100)
    result = _drop_random_bars({"SPY": series}, fraction=0.10, seed=42)
    assert len(result["SPY"]) < 100
    assert len(result["SPY"]) >= 85


def test_apply_stress_scenario_slippage_multiplier():
    from backtesting.stress_test import apply_stress_scenario, slippage_spike
    _, stressed_slip, _ = apply_stress_scenario({}, 10.0, slippage_spike(multiplier=3.0))
    assert stressed_slip == pytest.approx(30.0)


def test_apply_stress_scenario_fill_delay():
    from backtesting.stress_test import apply_stress_scenario, delayed_fills
    _, _, delay = apply_stress_scenario({}, 10.0, delayed_fills(delay_bars=2))
    assert delay == 2


def test_apply_stress_scenario_crash_modifies_prices():
    from backtesting.stress_test import apply_stress_scenario, sudden_crash
    series = _price_series(40)
    stressed_prices, _, _ = apply_stress_scenario(
        {"SPY": series}, 10.0, sudden_crash(drop_pct=0.30, duration_days=5)
    )
    assert stressed_prices["SPY"].iloc[-1] < series.iloc[-1] * 0.90


def test_default_stress_scenarios_has_five_distinct_names():
    from backtesting.stress_test import DEFAULT_STRESS_SCENARIOS
    assert len(DEFAULT_STRESS_SCENARIOS) == 5
    names = {s.name for s in DEFAULT_STRESS_SCENARIOS}
    assert names == {"sudden_crash", "high_vol_cluster", "slippage_spike",
                     "delayed_fills", "missing_data"}
