# tests/test_backtest_factor_pit_followup.py
"""Tests for backtesting/backtest_factor_pit_followup.py — the Phase 0
follow-up's sleeve decomposition, ex-low-vol composite variant, and
financials-sector diagnostic cut. All offline (synthetic fixtures), no
network — mirrors tests/test_backtest_factor_pit.py's pattern."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

import backtesting.backtest_factor_pit_followup as m


# ---------------------------------------------------------------------------
# split_windowed_gate
# ---------------------------------------------------------------------------

def test_split_windowed_gate_boundary_shared_between_windows():
    """The cutoff day itself must appear in BOTH windows (mirrors
    stability_split's iloc[:mid+1]/iloc[mid:] overlap) so neither window's
    first daily return is silently dropped by pct_change()."""
    dates = pd.date_range("2022-01-03", periods=40, freq="B")
    rng = np.random.default_rng(11)
    benchmark_rets = pd.Series(rng.normal(0.0001, 0.005, size=40), index=dates)
    equity = (1 + benchmark_rets + 0.0003).cumprod() * 100_000.0

    cutoff = dates[19].date()  # a date that exists exactly in the index
    result = m.split_windowed_gate(equity, benchmark_rets, cutoff)

    assert result["research"]["n_days"] > 0
    assert result["validation"]["n_days"] > 0
    # research covers dates[0..19] (20 equity points -> 19 returns),
    # validation covers dates[19..39] (21 equity points -> 20 returns)
    assert result["research"]["n_days"] == 19
    assert result["validation"]["n_days"] == 20


def test_split_windowed_gate_cutoff_between_index_dates_uses_prior_date():
    """A cutoff that falls on a non-trading day should split at the last
    trading day on or before it, same convention CSVPITProvider's own
    as-of lookups use elsewhere in this harness."""
    dates = pd.date_range("2022-01-03", periods=10, freq="B")  # business days
    rng = np.random.default_rng(12)
    benchmark_rets = pd.Series(rng.normal(0.0, 0.005, size=10), index=dates)
    equity = (1 + benchmark_rets).cumprod() * 100_000.0

    # dates[4] is a Friday; pick the following Saturday (not in the index)
    cutoff = (dates[4] + pd.Timedelta(days=1)).date()
    result = m.split_windowed_gate(equity, benchmark_rets, cutoff)

    # Should split as if cutoff were dates[4], not dates[5]
    expected = m.split_windowed_gate(equity, benchmark_rets, dates[4].date())
    assert result["research"]["n_days"] == expected["research"]["n_days"]
    assert result["validation"]["n_days"] == expected["validation"]["n_days"]


def test_split_windowed_gate_empty_equity_does_not_crash():
    empty = pd.Series(dtype=float)
    result = m.split_windowed_gate(empty, empty, pd.Timestamp("2024-01-01").date())
    assert result["research"]["n_days"] == 0
    assert result["validation"]["n_days"] == 0


# ---------------------------------------------------------------------------
# ex_low_vol_transform
# ---------------------------------------------------------------------------

def test_ex_low_vol_transform_matches_hand_computed_weights():
    """Weights renormalize the live _DEFAULT_WEIGHTS (0.25/0.25/0.25 value/
    momentum/quality, 0.15 reversal) over 0.90 after dropping low_vol's 0.10
    — hand-verified here, not just "did it run"."""
    df = pd.DataFrame({
        "value_score": [33, 0],
        "momentum_score": [33, 0],
        "quality_score": [33, 0],
        "low_vol_score": [33, 99],  # deliberately extreme — must be ignored
        "reversal_score": [33, 0],
    }, index=["ALL_EQUAL", "ZERO_EXCEPT_LOWVOL"])

    out = m.ex_low_vol_transform(df)

    # ALL_EQUAL: every non-low_vol sleeve is 33, so the weighted blend is 33
    # regardless of weights (weights sum to 1) — a degenerate but useful
    # sanity check that low_vol_score (33 here too) doesn't silently leak in
    # despite being the same value, since ZERO_EXCEPT_LOWVOL disambiguates.
    assert out.loc["ALL_EQUAL", "ex_low_vol_score"] == pytest.approx(33.0)
    # ZERO_EXCEPT_LOWVOL: every non-low_vol sleeve is 0, low_vol_score=99
    # must be fully excluded -> result must be exactly 0, not pulled toward 99.
    assert out.loc["ZERO_EXCEPT_LOWVOL", "ex_low_vol_score"] == pytest.approx(0.0)

    weights_sum = sum(m._EX_LOW_VOL_WEIGHTS.values())
    assert weights_sum == pytest.approx(1.0)
    assert m._EX_LOW_VOL_WEIGHTS["value_score"] == pytest.approx(0.25 / 0.90)
    assert m._EX_LOW_VOL_WEIGHTS["reversal_score"] == pytest.approx(0.15 / 0.90)
    assert "low_vol_score" not in m._EX_LOW_VOL_WEIGHTS


def test_ex_low_vol_transform_does_not_mutate_input():
    df = pd.DataFrame({
        "value_score": [10.0], "momentum_score": [10.0], "quality_score": [10.0],
        "low_vol_score": [10.0], "reversal_score": [10.0],
    }, index=["X"])
    m.ex_low_vol_transform(df)
    assert "ex_low_vol_score" not in df.columns


# ---------------------------------------------------------------------------
# financials_sector_cut
# ---------------------------------------------------------------------------

@dataclass
class _FakeTrade:
    ticker: str
    pnl_pct: float


def test_financials_sector_cut_buckets_correctly():
    signals = [
        {"ticker": "JPM", "sector": "Financial Services"},
        {"ticker": "BAC", "sector": "Financial Services"},
        {"ticker": "AAPL", "sector": "Technology"},
        {"ticker": "XOM", "sector": "Energy"},
    ]
    trades = [
        _FakeTrade("JPM", 5.0),
        _FakeTrade("JPM", -3.0),
        _FakeTrade("BAC", 2.0),
        _FakeTrade("AAPL", 10.0),
        _FakeTrade("XOM", -1.0),
    ]

    summary = m.financials_sector_cut(trades, signals)

    assert summary["Financial Services"]["n_trades"] == 3
    assert summary["Financial Services"]["mean_pnl_pct"] == pytest.approx((5.0 - 3.0 + 2.0) / 3)
    assert summary["Other"]["n_trades"] == 2
    assert summary["Financial Services"]["win_rate"] == pytest.approx(2 / 3)


def test_financials_sector_cut_unknown_sector_goes_to_other():
    """A ticker with no matching signal (sector unresolvable) must not
    crash and must not be silently counted as Financial Services."""
    signals: list[dict] = []
    trades = [_FakeTrade("ZZZZ", 1.0)]

    summary = m.financials_sector_cut(trades, signals)

    assert summary["Other"]["n_trades"] == 1
    assert "Financial Services" not in summary
