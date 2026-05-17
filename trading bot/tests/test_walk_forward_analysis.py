"""Tests for backtesting.analysis — regime/confidence/exposure breakdown."""
from unittest.mock import MagicMock
import pytest
from backtesting.simulation import SimTrade


def _trade(regime="bull", conviction=7, pnl_pct=5.0,
           entry="2020-01-02", exit_="2020-01-12"):
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


def test_regime_performance_groups_by_label():
    from backtesting.analysis import regime_performance
    trades = [_trade("bull", pnl_pct=5.0), _trade("bear", pnl_pct=-3.0)]
    result = regime_performance(trades)
    assert "bull" in result and "bear" in result
    assert result["bull"]["n_trades"] == 1
    assert result["bear"]["n_trades"] == 1


def test_regime_performance_win_rate():
    from backtesting.analysis import regime_performance
    trades = [_trade("bull", pnl_pct=5.0), _trade("bull", pnl_pct=-2.0)]
    result = regime_performance(trades)
    assert result["bull"]["win_rate"] == pytest.approx(0.5)


def test_regime_performance_empty():
    from backtesting.analysis import regime_performance
    assert regime_performance([]) == {}


def test_confidence_bucket_splits_correctly():
    from backtesting.analysis import confidence_bucket_performance
    # low=[1-5], mid=[6-7], high=[8-10]
    trades = [_trade(conviction=5), _trade(conviction=7), _trade(conviction=9)]
    result = confidence_bucket_performance(trades)
    assert result["low"]["n_trades"] == 1
    assert result["mid"]["n_trades"] == 1
    assert result["high"]["n_trades"] == 1


def test_confidence_bucket_boundary_conviction4_is_low():
    from backtesting.analysis import confidence_bucket_performance
    trades = [_trade(conviction=4)]
    result = confidence_bucket_performance(trades)
    assert result["low"]["n_trades"] == 1
    assert result["mid"]["n_trades"] == 0


def test_confidence_bucket_boundary_conviction6_is_mid():
    from backtesting.analysis import confidence_bucket_performance
    trades = [_trade(conviction=6)]
    result = confidence_bucket_performance(trades)
    assert result["mid"]["n_trades"] == 1


def test_regime_performance_includes_profit_factor():
    from backtesting.analysis import regime_performance
    trades = [_trade("bull", pnl_pct=10.0), _trade("bull", pnl_pct=-5.0)]
    result = regime_performance(trades)
    assert "profit_factor" in result["bull"]
    assert result["bull"]["profit_factor"] == pytest.approx(2.0, rel=0.01)


def test_exposure_by_regime_sums_to_one():
    from backtesting.analysis import exposure_by_regime
    states = [MagicMock(regime_label="bull")] * 3 + [MagicMock(regime_label="bear")] * 1
    result = exposure_by_regime(states)
    assert abs(sum(result.values()) - 1.0) < 1e-6
    assert result["bull"] == pytest.approx(0.75)


def test_exposure_by_regime_empty_returns_empty():
    from backtesting.analysis import exposure_by_regime
    assert exposure_by_regime([]) == {}
