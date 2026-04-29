import pytest
import pandas as pd
from performance.tracker import PerformanceTracker


def test_equity_series_empty(db):
    tracker = PerformanceTracker()
    eq = tracker.equity_series()
    assert isinstance(eq, pd.Series)
    assert eq.empty


def test_equity_series_with_data(db):
    db.log_portfolio("2026-01-02", cash=95_000.0,
                     positions_value=5_000.0, total_nav=100_000.0)
    db.log_portfolio("2026-01-03", cash=94_000.0,
                     positions_value=7_000.0, total_nav=101_000.0)
    tracker = PerformanceTracker()
    eq = tracker.equity_series()
    assert len(eq) == 2
    assert float(eq.iloc[0]) == pytest.approx(100_000.0)
    assert float(eq.iloc[1]) == pytest.approx(101_000.0)


def test_trade_returns_empty(db):
    tracker = PerformanceTracker()
    tr = tracker.trade_returns()
    assert isinstance(tr, pd.Series)
    assert tr.empty


def test_summary_returns_error_when_no_data(db):
    tracker = PerformanceTracker()
    result = tracker.summary()
    assert "error" in result


def test_summary_with_portfolio_data(db):
    for i in range(10):
        db.log_portfolio(
            f"2026-01-{i + 2:02d}",
            cash=99_000.0,
            positions_value=1_000.0 + i * 100,
            total_nav=100_000.0 + i * 100,
        )
    tracker = PerformanceTracker()
    result = tracker.summary()
    assert "sharpe" in result
    assert "total_return_pct" in result
    assert "max_drawdown_pct" in result
    assert result["n_trades"] == 0


def test_by_regime_empty(db):
    tracker = PerformanceTracker()
    result = tracker.by_regime()
    assert isinstance(result, dict)


def test_by_regime_groups_by_label(db):
    db.log_regime(
        date="2026-01-02",
        regime_label="bull",
        regime_index=2,
        confidence=0.8,
        is_stable=True,
        n_regimes=3,
    )
    db.log_closed_position(
        ticker="AAPL",
        entry_price=100.0,
        exit_price=110.0,
        shares=10.0,
        entry_date="2026-01-02",
        exit_date="2026-01-15",
        exit_reason="ai_exit",
        signal_id=1,
        signal_source="congressional",
    )
    tracker = PerformanceTracker()
    result = tracker.by_regime()
    total_trades = sum(v["n_trades"] for v in result.values())
    assert total_trades == 1
