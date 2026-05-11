"""Unit tests for CorrelationFilter."""
import numpy as np
import pandas as pd
import pytest


def _make_filter(threshold=0.7, min_overlap=20, window_days=60):
    from system.config import Settings, CorrelationConfig
    cfg = Settings(correlation=CorrelationConfig(
        threshold=threshold,
        window_days=window_days,
        min_overlap_days=min_overlap,
    ))
    from risk.correlation import CorrelationFilter
    return CorrelationFilter(cfg)


def _idx(n=60):
    return pd.date_range("2026-01-01", periods=n, freq="B")


# ── _multiplier_from_rho: pure math, no mocking ───────────────────────

def test_multiplier_below_threshold_returns_one():
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(0.5) == pytest.approx(1.0)


def test_multiplier_at_threshold_returns_one():
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(0.7) == pytest.approx(1.0)


def test_multiplier_midpoint_returns_half():
    # ρ=0.85, threshold=0.7: (0.85-0.7)/(1.0-0.7) = 0.5 → mult = 0.5
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(0.85) == pytest.approx(0.5)


def test_multiplier_at_perfect_correlation_returns_zero():
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(1.0) == pytest.approx(0.0)


# ── size_multiplier: injected data (no yfinance) ──────────────────────

def test_size_multiplier_returns_one_when_no_holdings():
    f = _make_filter()
    assert f.size_multiplier("AAPL") == pytest.approx(1.0)


def test_size_multiplier_returns_one_for_uncorrelated_candidate():
    f = _make_filter(threshold=0.7)
    idx = _idx(60)
    np.random.seed(0)
    hold = pd.Series(np.random.normal(0, 1, 60), index=idx)
    np.random.seed(1)
    cand = pd.Series(np.random.normal(0, 1, 60), index=idx)
    f._holdings_returns = {"SPY": hold}
    f._candidate_cache = {"AAPL": cand}
    mult = f.size_multiplier("AAPL")
    # Independent series → ρ ≈ 0 → mult = 1.0
    assert mult == pytest.approx(1.0)


def test_size_multiplier_scales_down_for_correlated_candidate():
    f = _make_filter(threshold=0.7)
    idx = _idx(60)
    np.random.seed(42)
    base = pd.Series(np.random.normal(0, 1, 60), index=idx)
    np.random.seed(7)
    noise = pd.Series(np.random.normal(0, 1, 60), index=idx)
    # Theoretical ρ ≈ 0.85 between base and candidate
    candidate = 0.85 * base + np.sqrt(1 - 0.85 ** 2) * noise
    f._holdings_returns = {"SPY": base}
    f._candidate_cache = {"AAPL": candidate}
    mult = f.size_multiplier("AAPL")
    # Allow variance from finite samples: somewhere in (0.2, 0.8)
    assert 0.2 <= mult <= 0.8


def test_size_multiplier_uses_max_correlation_across_holdings():
    f = _make_filter(threshold=0.7)
    idx = _idx(60)
    np.random.seed(42)
    base = pd.Series(np.random.normal(0, 1, 60), index=idx)
    np.random.seed(7)
    noise = pd.Series(np.random.normal(0, 1, 60), index=idx)
    # hold1: ρ ≈ 0.9 with base; hold2: uncorrelated
    hold1 = 0.9 * base + np.sqrt(1 - 0.9 ** 2) * noise
    np.random.seed(99)
    hold2 = pd.Series(np.random.normal(0, 1, 60), index=idx)
    f._holdings_returns = {"SPY": hold1, "QQQ": hold2}
    f._candidate_cache = {"AAPL": base}
    mult = f.size_multiplier("AAPL")
    # hold1 drives the max ρ → significant reduction
    assert mult < 0.6


def test_size_multiplier_skips_holdings_with_insufficient_overlap():
    f = _make_filter(threshold=0.7, min_overlap=30)
    # Holding and candidate date ranges don't overlap
    idx_hold = pd.date_range("2025-01-01", periods=60, freq="B")
    idx_cand = pd.date_range("2026-05-01", periods=60, freq="B")
    np.random.seed(0)
    f._holdings_returns = {
        "SPY": pd.Series(np.random.normal(0, 1, 60), index=idx_hold)
    }
    f._candidate_cache = {
        "AAPL": pd.Series(np.random.normal(0, 1, 60), index=idx_cand)
    }
    assert f.size_multiplier("AAPL") == pytest.approx(1.0)


def test_size_multiplier_returns_one_on_yfinance_failure(mocker):
    f = _make_filter()
    idx = _idx(60)
    np.random.seed(0)
    f._holdings_returns = {
        "SPY": pd.Series(np.random.normal(0, 1, 60), index=idx)
    }
    mocker.patch("risk.correlation.yf.download", side_effect=Exception("network error"))
    assert f.size_multiplier("AAPL") == pytest.approx(1.0)


# ── load_holdings_returns ─────────────────────────────────────────────

def test_load_holdings_returns_empty_list_is_noop():
    f = _make_filter()
    f.load_holdings_returns([])
    assert f._holdings_returns == {}


def test_load_holdings_returns_yfinance_failure_leaves_empty_cache(mocker):
    f = _make_filter()
    mocker.patch("risk.correlation.yf.download", side_effect=Exception("network"))
    f.load_holdings_returns(["AAPL"])
    assert f._holdings_returns == {}


# ── clear ─────────────────────────────────────────────────────────────

def test_clear_resets_both_caches():
    f = _make_filter()
    idx = _idx(30)
    np.random.seed(0)
    f._holdings_returns = {"SPY": pd.Series(np.random.normal(0, 1, 30), index=idx)}
    f._candidate_cache = {"AAPL": pd.Series(np.random.normal(0, 1, 30), index=idx)}
    f.clear()
    assert f._holdings_returns == {}
    assert f._candidate_cache == {}
