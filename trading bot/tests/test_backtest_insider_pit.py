# tests/test_backtest_insider_pit.py
"""Tests for backtesting/backtest_insider_pit.py — the Phase 0 follow-up's
PIT backtest of the insider (Form 4) signal. Mirrors
tests/test_backtest_sue_pit_events.py's pattern. All offline."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import backtesting.backtest_insider_pit as m


# ---------------------------------------------------------------------------
# load_qualified_events
# ---------------------------------------------------------------------------

def test_load_qualified_events_applies_live_dollar_and_lag_filters(tmp_path):
    """Mirrors bot/insider_signal.py::is_qualified_insider exactly: dollar
    floor $50k, lag (disclosure - transaction) in [0, 45] days."""
    raw = pd.DataFrame([
        {"ticker": "A", "transaction_date": "2023-01-01", "disclosure_date": "2023-01-10",
         "amount_usd": 60_000.0},  # qualifies: $60k >= $50k, lag=9<=45
        {"ticker": "B", "transaction_date": "2023-01-01", "disclosure_date": "2023-01-05",
         "amount_usd": 40_000.0},  # too small
        {"ticker": "C", "transaction_date": "2023-01-01", "disclosure_date": "2023-04-01",
         "amount_usd": 100_000.0},  # lag too long (>45d)
        {"ticker": "D", "transaction_date": "2023-01-10", "disclosure_date": "2023-01-01",
         "amount_usd": 100_000.0},  # negative lag (disclosed before transaction) — excluded
    ])
    path = tmp_path / "tx.parquet"
    raw.to_parquet(path)

    result = m.load_qualified_events(path)

    assert list(result["ticker"]) == ["A"]
    assert result.iloc[0]["lag_days"] == 9


def test_load_qualified_events_empty_input_returns_empty(tmp_path):
    path = tmp_path / "tx.parquet"
    pd.DataFrame(columns=["ticker", "transaction_date", "disclosure_date", "amount_usd"]).to_parquet(path)
    result = m.load_qualified_events(path)
    assert result.empty


# ---------------------------------------------------------------------------
# add_tradable_date
# ---------------------------------------------------------------------------

def test_add_tradable_date_skips_weekend():
    # A Friday within this module's own SAMPLE_START/SAMPLE_END NYSE-session
    # range (2021-09-01..2025-06-30+14d) — unlike backtest_sue_pit.py's own
    # much wider 2012-2026 range, a date outside this module's window has no
    # matching session and silently returns the range's first session
    # instead (a real trap, not a hypothetical — caught by this exact test).
    events = pd.DataFrame([
        {"ticker": "ZZZ", "disclosure_date": "2022-09-09"},  # a Friday
    ])
    result = m.add_tradable_date(events)
    assert len(result) == 1
    assert result.iloc[0]["tradable_date"] == date(2022, 9, 12)  # following Monday


def test_add_tradable_date_always_strictly_after_disclosure_date():
    events = pd.DataFrame([
        {"ticker": "ZZZ", "disclosure_date": "2022-01-04"},
        {"ticker": "ZZZ", "disclosure_date": "2022-06-15"},
    ])
    result = m.add_tradable_date(events)
    assert len(result) == 2
    disclosure_dates = pd.to_datetime(result["disclosure_date"]).dt.date
    assert (result["tradable_date"] > disclosure_dates).all()


# ---------------------------------------------------------------------------
# build_pit_events (universe restriction reused from backtest_sue_pit)
# ---------------------------------------------------------------------------

def test_build_pit_events_drops_pre_membership_events(tmp_path):
    """Same regression this repo already caught for SUE: a ticker's events
    predating its actual S&P 500 membership must be dropped."""
    raw = pd.DataFrame([
        {"ticker": "REAL", "transaction_date": "2021-01-01", "disclosure_date": "2021-01-05",
         "amount_usd": 100_000.0},
        {"ticker": "LATE", "transaction_date": "2021-01-01", "disclosure_date": "2021-01-05",
         "amount_usd": 100_000.0},  # before LATE joined
        {"ticker": "LATE", "transaction_date": "2023-06-01", "disclosure_date": "2023-06-05",
         "amount_usd": 100_000.0},  # after LATE joined
    ])
    tx_path = tmp_path / "tx.parquet"
    raw.to_parquet(tx_path)

    constituents = pd.DataFrame([
        {"date": date(2020, 1, 1), "ticker": "REAL"},
        {"date": date(2023, 1, 1), "ticker": "LATE"},
    ])

    with patch("backtesting.backtest_insider_pit._TRANSACTIONS_PATH", tx_path), \
         patch("backtesting.backtest_sue_pit.fetch_sp500_pit_constituents", return_value=constituents):
        result = m.build_pit_events()

    kept_tickers = set(result["ticker"])
    assert "REAL" in kept_tickers
    late_rows = result[result["ticker"] == "LATE"]
    assert len(late_rows) == 1
    assert late_rows.iloc[0]["disclosure_date"] == "2023-06-05"


# ---------------------------------------------------------------------------
# daily_holding_returns
# ---------------------------------------------------------------------------

def test_daily_holding_returns_averages_currently_held_positions():
    dates = pd.date_range("2022-01-03", periods=30, freq="B")
    prices = pd.DataFrame({
        "AAA": np.linspace(100, 130, 30),  # steadily rising
        "BBB": np.linspace(100, 100, 30),  # flat
    }, index=dates)

    events = pd.DataFrame([
        {"ticker": "AAA", "tradable_date": dates[0].date()},
        {"ticker": "BBB", "tradable_date": dates[0].date()},
    ])

    result = m.daily_holding_returns(events, prices, horizon=5)

    assert not result.empty
    # AAA rises, BBB flat -> the equal-weighted average must be positive but
    # less than AAA's own daily return (BBB drags it toward zero).
    aaa_daily = prices["AAA"].pct_change().dropna()
    assert (result > 0).all()
    assert (result < aaa_daily.reindex(result.index)).all()


def test_daily_holding_returns_ticker_missing_from_prices_is_skipped():
    dates = pd.date_range("2022-01-03", periods=10, freq="B")
    prices = pd.DataFrame({"AAA": np.linspace(100, 110, 10)}, index=dates)
    events = pd.DataFrame([
        {"ticker": "ZZZZ", "tradable_date": dates[0].date()},  # not in prices
    ])
    result = m.daily_holding_returns(events, prices, horizon=5)
    assert result.empty


def test_daily_holding_returns_empty_events_returns_empty_series():
    prices = pd.DataFrame({"AAA": [100.0, 101.0]},
                          index=pd.date_range("2022-01-03", periods=2, freq="B"))
    result = m.daily_holding_returns(pd.DataFrame(columns=["ticker", "tradable_date"]),
                                      prices, horizon=5)
    assert result.empty


# ---------------------------------------------------------------------------
# run_gate_for_horizon (integration of split_windowed_gate + daily_holding_returns)
# ---------------------------------------------------------------------------

def test_run_gate_for_horizon_returns_research_and_validation_keys():
    dates = pd.date_range("2022-01-03", periods=60, freq="B")
    rng = np.random.default_rng(5)
    prices = pd.DataFrame({
        "AAA": 100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 60))),
    }, index=dates)
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, 60))), index=dates)
    events = pd.DataFrame([{"ticker": "AAA", "tradable_date": dates[0].date()}])

    result = m.run_gate_for_horizon(events, prices, spy, horizon=10)

    assert "research" in result and "validation" in result
    assert "tstat" in result["research"]


def test_run_gate_for_horizon_empty_events_does_not_crash():
    prices = pd.DataFrame({"AAA": [100.0, 101.0]},
                          index=pd.date_range("2022-01-03", periods=2, freq="B"))
    spy = pd.Series([100.0, 101.0], index=prices.index)
    result = m.run_gate_for_horizon(pd.DataFrame(columns=["ticker", "tradable_date"]),
                                     prices, spy, horizon=10)
    assert result["research"]["n_days"] == 0
