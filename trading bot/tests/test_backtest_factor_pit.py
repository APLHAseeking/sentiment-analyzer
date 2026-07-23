# tests/test_backtest_factor_pit.py
"""Tests for backtesting/backtest_factor_pit.py — the Phase 0 PIT backtest
driver for the bot's primary (fundamental factor) signal."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import backtesting.backtest_factor_pit as m


# ---------------------------------------------------------------------------
# run_gate / stability_split
# ---------------------------------------------------------------------------

def test_run_gate_positive_excess_return_gives_positive_tstat_and_ir():
    dates = pd.date_range("2022-01-03", periods=200, freq="B")
    rng = np.random.default_rng(1)
    benchmark_rets = pd.Series(rng.normal(0.0002, 0.005, size=200), index=dates)
    # Strategy has its OWN independent noise plus a positive offset — using
    # the exact same noise as the benchmark (just shifted by a constant)
    # would make the active/excess return literally zero-variance, which
    # hac_mean_tstat correctly flags as degenerate (nan) rather than a
    # real edge; independent noise is what a genuinely different strategy
    # return series would look like.
    strategy_rets = pd.Series(rng.normal(0.0002, 0.005, size=200), index=dates) + 0.001
    equity = (1 + strategy_rets).cumprod() * 100_000.0

    gate = m.run_gate(equity, benchmark_rets)

    assert gate["tstat"] > 0
    assert gate["ir"] > 0
    assert gate["n_days"] > 0


def test_run_gate_zero_excess_return_gives_near_zero_tstat():
    dates = pd.date_range("2022-01-03", periods=100, freq="B")
    rng = np.random.default_rng(2)
    benchmark_rets = pd.Series(rng.normal(0.0, 0.005, size=100), index=dates)
    equity = (1 + benchmark_rets).cumprod() * 100_000.0  # strategy == benchmark exactly

    gate = m.run_gate(equity, benchmark_rets)

    assert gate["mean_daily_excess"] == pytest.approx(0.0, abs=1e-9)


def test_stability_split_runs_gate_on_each_half():
    dates = pd.date_range("2022-01-03", periods=100, freq="B")
    rng = np.random.default_rng(3)
    benchmark_rets = pd.Series(rng.normal(0.0001, 0.005, size=100), index=dates)
    equity = (1 + benchmark_rets + 0.0005).cumprod() * 100_000.0

    result = m.stability_split(equity, benchmark_rets)

    assert "first_half" in result and "second_half" in result
    assert result["first_half"]["n_days"] > 0
    assert result["second_half"]["n_days"] > 0
    # Roughly even split (within a day or two given the +1 overlap point)
    assert abs(result["first_half"]["n_days"] - result["second_half"]["n_days"]) <= 2


# ---------------------------------------------------------------------------
# build_constituents_csv
# ---------------------------------------------------------------------------

def test_build_constituents_csv_restricts_to_sample_window(mocker, tmp_path):
    mocker.patch.object(m, "_CONSTITUENTS_CACHE", tmp_path / "constituents_cache.parquet")
    mocker.patch.object(m, "_BUILT_INPUTS_DIR", tmp_path)
    mocker.patch.object(m, "_CONSTITUENTS_CSV", tmp_path / "constituents.csv")

    fake_df = pd.DataFrame({
        "date": [
            pd.Timestamp("2019-01-01").date(),   # before SAMPLE_START — must be excluded
            pd.Timestamp("2022-01-01").date(),   # inside window
            pd.Timestamp("2026-01-01").date(),   # after SAMPLE_END — must be excluded
        ],
        "ticker": ["OLD", "AAPL", "FUTURE"],
    })
    mocker.patch.object(m, "fetch_sp500_pit_constituents", return_value=fake_df)

    m.build_constituents_csv()

    written = pd.read_csv(m._CONSTITUENTS_CSV)
    assert list(written["ticker"]) == ["AAPL"]


# ---------------------------------------------------------------------------
# build_prices_csv
# ---------------------------------------------------------------------------

def test_build_prices_csv_writes_wide_csv_and_returns_missing(mocker, tmp_path):
    mocker.patch.object(m, "_BUILT_INPUTS_DIR", tmp_path)
    mocker.parameter = None
    mocker.patch.object(m, "_PRICES_CSV", tmp_path / "prices.csv")
    fake_wide = pd.DataFrame({"AAPL": [100.0, 101.0]},
                             index=pd.to_datetime(["2022-01-03", "2022-01-04"]))
    fake_wide.index.name = "date"
    mocker.patch.object(m, "fetch_pit_prices", return_value=(fake_wide, ["MISSING"]))

    missing = m.build_prices_csv(["AAPL", "MISSING"])

    assert missing == ["MISSING"]
    assert (tmp_path / "prices.csv").exists()


# ---------------------------------------------------------------------------
# build_fundamentals_csv — the ticker-scope bug caught live this session
# ---------------------------------------------------------------------------

def _fake_simfin_dataset(dataset, cache_path, market="us", variant=None):
    """Stand-in for fetch_simfin_dataset that returns a distinguishable
    2-row frame (AAPL + SOMEOTHERTICKER) for every dataset/variant name —
    income/income-banks/income-insurance all get an 'x' column, etc., so
    concatenating the 3 variants per statement type produces 6 rows
    (2 tickers x 3 variants) before any ticker filtering is applied."""
    col = {"income": "x", "balance": "y", "cashflow": "z"}
    for stem, colname in col.items():
        if dataset.startswith(stem):
            return pd.DataFrame({"Ticker": ["AAPL", "SOMEOTHERTICKER"], colname: [1, 2]})
    if dataset == "companies":
        return pd.DataFrame({"Ticker": ["AAPL", "SOMEOTHERTICKER"], "IndustryId": [1, 1]})
    if dataset == "industries":
        return pd.DataFrame({"IndustryId": [1], "Sector": ["Technology"]})
    raise AssertionError(f"unexpected dataset {dataset!r}")


def test_build_fundamentals_csv_restricts_datasets_to_given_tickers(mocker, tmp_path):
    """Regression test for a real bug: an earlier version of this function
    ignored its ticker restriction entirely, silently walking SimFin's full
    ~3,781-ticker universe and (in production) burning real Tiingo
    rate-limit budget on tickers nobody asked to fetch. This asserts
    compute_fundamentals_snapshots is only ever called with rows for the
    tickers actually requested — across all 3 variants (generic/banks/
    insurance) per statement type, not just the generic one."""
    mocker.patch.object(m, "_SIMFIN_CACHE_DIR", tmp_path)
    mocker.patch.object(m, "_BUILT_INPUTS_DIR", tmp_path)
    mocker.patch.object(m, "_FUNDAMENTALS_CSV", tmp_path / "fundamentals.csv")
    mocker.patch.object(m, "fetch_simfin_dataset", side_effect=_fake_simfin_dataset)
    mocker.patch.object(m, "sector_map", return_value={"AAPL": "Technology"})
    snapshot_spy = mocker.patch.object(
        m, "compute_fundamentals_snapshots", return_value=pd.DataFrame({"date": [], "ticker": []}),
    )

    m.build_fundamentals_csv(["AAPL"])

    call_args = snapshot_spy.call_args[0]
    income_arg, balance_arg, cashflow_arg = call_args[0], call_args[1], call_args[2]
    assert set(income_arg["Ticker"]) == {"AAPL"}
    assert set(balance_arg["Ticker"]) == {"AAPL"}
    assert set(cashflow_arg["Ticker"]) == {"AAPL"}


def test_build_fundamentals_csv_merges_generic_banks_and_insurance_variants(mocker, tmp_path):
    """The real bug this session found: banks/insurers report on separate
    SimFin datasets with different statement structures, and an earlier
    version only fetched the generic dataset — silently zeroing out
    fundamentals for an entire sector (146 of 576 real universe tickers,
    concentrated in financials). All 3 variants per statement type must be
    concatenated, not just the generic one."""
    mocker.patch.object(m, "_SIMFIN_CACHE_DIR", tmp_path)
    mocker.patch.object(m, "_BUILT_INPUTS_DIR", tmp_path)
    mocker.patch.object(m, "_FUNDAMENTALS_CSV", tmp_path / "fundamentals.csv")
    fetch_spy = mocker.patch.object(m, "fetch_simfin_dataset", side_effect=_fake_simfin_dataset)
    mocker.patch.object(m, "sector_map", return_value={})
    mocker.patch.object(
        m, "compute_fundamentals_snapshots", return_value=pd.DataFrame({"date": [], "ticker": []}),
    )

    m.build_fundamentals_csv(None)

    requested_datasets = {call.args[0] for call in fetch_spy.call_args_list}
    assert requested_datasets == {
        "income", "income-banks", "income-insurance",
        "balance", "balance-banks", "balance-insurance",
        "cashflow", "cashflow-banks", "cashflow-insurance",
        "companies", "industries",
    }


def test_build_fundamentals_csv_with_none_tickers_uses_full_universe(mocker, tmp_path):
    """tickers=None (the real full-production-run case) must NOT filter —
    this is the one case where processing every ticker is correct, since
    build_prices_csv was also called with the full universe."""
    mocker.patch.object(m, "_SIMFIN_CACHE_DIR", tmp_path)
    mocker.patch.object(m, "_BUILT_INPUTS_DIR", tmp_path)
    mocker.patch.object(m, "_FUNDAMENTALS_CSV", tmp_path / "fundamentals.csv")
    mocker.patch.object(m, "fetch_simfin_dataset", side_effect=_fake_simfin_dataset)
    mocker.patch.object(m, "sector_map", return_value={})
    snapshot_spy = mocker.patch.object(
        m, "compute_fundamentals_snapshots", return_value=pd.DataFrame({"date": [], "ticker": []}),
    )

    m.build_fundamentals_csv(None)

    income_arg = snapshot_spy.call_args[0][0]
    assert set(income_arg["Ticker"]) == {"AAPL", "SOMEOTHERTICKER"}


# ---------------------------------------------------------------------------
# fetch_spy_returns
# ---------------------------------------------------------------------------

def test_fetch_spy_returns_closes_session(mocker):
    mock_session = MagicMock()
    mocker.patch.object(m, "make_shared_yf_session", return_value=mock_session)
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame(
        {"Close": [400.0, 401.0]},
        index=pd.to_datetime(["2022-01-03", "2022-01-04"]),
    )
    mocker.patch("yfinance.Ticker", return_value=mock_ticker)

    prices = m.fetch_spy_returns()

    assert len(prices) == 2
    mock_session.close.assert_called_once()
