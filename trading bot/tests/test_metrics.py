"""Tests for backtesting metrics calculations."""
import numpy as np
import pandas as pd
import pytest

from backtesting.metrics import (
    total_return, annualized_return, sharpe_ratio, sortino_ratio,
    max_drawdown, win_rate, profit_factor, compute_all,
    turnover, avg_holding_period,
)
from backtesting.simulation import SimTrade


def _equity(values):
    dates = pd.date_range("2020-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=dates)


def _make_trade(entry="2020-01-02", exit_="2020-01-22",
                pnl_pct=5.0, regime="bull", conviction=7):
    return SimTrade(
        ticker="AAPL",
        entry_date=entry,
        exit_date=exit_,
        entry_price=100.0,
        exit_price=100.0 * (1 + pnl_pct / 100),
        shares=10.0,
        pnl=pnl_pct * 10,
        pnl_pct=pnl_pct,
        regime_at_entry=regime,
        conviction=conviction,
        exit_reason="take_profit",
    )


def test_total_return_zero_on_flat():
    eq = _equity([100.0] * 50)
    assert total_return(eq) == pytest.approx(0.0)


def test_total_return_positive():
    eq = _equity([100.0, 110.0])
    assert total_return(eq) == pytest.approx(0.10, abs=0.001)


def test_total_return_negative():
    eq = _equity([100.0, 80.0])
    assert total_return(eq) == pytest.approx(-0.20, abs=0.001)


def test_max_drawdown_zero_on_monotone():
    eq = _equity(list(range(100, 200)))
    assert max_drawdown(eq) == pytest.approx(0.0, abs=1e-6)


def test_max_drawdown_known_value():
    # Rise to 120, fall to 90 → drawdown = 90/120 - 1 = -25%
    eq = _equity([100.0, 110.0, 120.0, 100.0, 90.0])
    dd = max_drawdown(eq)
    assert dd == pytest.approx(0.25, abs=0.01)


def test_sharpe_ratio_returns_float():
    eq = _equity([100 * (1 + 0.0004) ** i for i in range(252)])
    sr = sharpe_ratio(eq)
    assert isinstance(sr, float)


def test_sharpe_ratio_positive_for_trending_equity():
    eq = _equity([100 * (1 + 0.001) ** i for i in range(252)])
    sr = sharpe_ratio(eq, risk_free=0.0)
    assert sr > 0


def test_win_rate_empty():
    assert win_rate(pd.Series(dtype=float)) == 0.0


def test_win_rate_all_wins():
    tr = pd.Series([0.01, 0.02, 0.03])
    assert win_rate(tr) == pytest.approx(1.0)


def test_win_rate_half_wins():
    tr = pd.Series([0.01, -0.01])
    assert win_rate(tr) == pytest.approx(0.5)


def test_profit_factor_no_losses():
    tr = pd.Series([0.05, 0.03])
    pf = profit_factor(tr)
    assert pf == float("inf")


def test_profit_factor_calculation():
    # Wins: 0.10, Losses: 0.05 → PF = 0.10 / 0.05 = 2.0
    tr = pd.Series([0.10, -0.05])
    assert profit_factor(tr) == pytest.approx(2.0)


def test_compute_all_returns_all_keys():
    eq = _equity([100 * (1 + 0.0005) ** i for i in range(252)])
    tr = pd.Series([0.01, -0.005, 0.02, -0.01])
    result = compute_all(eq, tr)
    required = ["total_return_pct", "annualized_return_pct", "sharpe",
                "max_drawdown_pct", "win_rate", "n_trades"]
    for key in required:
        assert key in result, f"Missing metric: {key}"


def test_compute_all_edge_case_empty_equity():
    eq = pd.Series(dtype=float)
    result = compute_all(eq)
    assert result["total_return_pct"] == 0.0
    assert result["sharpe"] == 0.0


def test_turnover_zero_when_no_volume():
    eq = _equity([100_000.0] * 10)
    assert turnover(0.0, eq) == pytest.approx(0.0)


def test_turnover_ratio():
    # avg NAV = 100_000; traded 50_000 → turnover = 0.5
    eq = _equity([100_000.0] * 10)
    assert turnover(50_000.0, eq) == pytest.approx(0.5, rel=1e-3)


def test_avg_holding_period_empty():
    assert avg_holding_period([]) == pytest.approx(0.0)


def test_avg_holding_period_single_trade():
    # entry 2020-01-02, exit 2020-01-12 → 10 calendar days
    t = _make_trade(entry="2020-01-02", exit_="2020-01-12")
    assert avg_holding_period([t]) == pytest.approx(10.0)


def test_compute_all_includes_turnover_when_volume_provided():
    eq = _equity([100_000.0, 101_000.0, 102_000.0])
    result = compute_all(eq, total_volume_traded=50_000.0)
    assert "turnover" in result
    assert result["turnover"] is not None


def test_compute_all_turnover_none_when_not_provided():
    eq = _equity([100_000.0, 101_000.0])
    result = compute_all(eq)
    assert result.get("turnover") is None
