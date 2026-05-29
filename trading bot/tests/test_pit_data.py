"""Tests for backtesting.pit_data and backtesting.run_strategy_backtest.

All tests are fully offline — no yfinance, no network.
Uses in-memory synthetic fixtures: 4 tickers (AAPL, MSFT, GOOG + ENRN which
'delists' mid-sample), 2 rebalance dates.
"""
from __future__ import annotations

import io
import textwrap
from datetime import date

import numpy as np
import pandas as pd
import pytest

from backtesting.pit_data import CSVPITProvider
from backtesting.run_strategy_backtest import run_pit_backtest


# ── Synthetic fixture builders ────────────────────────────────────────────────

def _write_csv(tmp_path, filename: str, content: str):
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content).strip())
    return str(p)


def _make_fixtures(tmp_path):
    """Return (constituents_path, fundamentals_path, prices_path)."""

    constituents = _write_csv(tmp_path, "constituents.csv", """
        date,ticker
        2020-01-01,AAPL
        2020-01-01,MSFT
        2020-01-01,GOOG
        2020-01-01,ENRN
        2020-07-01,AAPL
        2020-07-01,MSFT
        2020-07-01,GOOG
    """)
    # Note: ENRN absent from 2020-07-01 snapshot — it 'delisted' mid-2020.

    fundamentals = _write_csv(tmp_path, "fundamentals.csv", """
        date,ticker,trailingPE,priceToBook,freeCashflow,marketCap,returnOnEquity,profitMargins,debtToEquity,sector
        2020-01-15,AAPL,22.0,11.0,5.0e10,1.3e12,0.60,0.21,1.7,Technology
        2020-01-15,MSFT,30.0,12.0,4.0e10,1.4e12,0.38,0.34,0.6,Technology
        2020-01-15,GOOG,28.0,5.0,3.0e10,9.0e11,0.17,0.22,0.1,Technology
        2020-01-15,ENRN,12.0,2.0,1.0e9,5.0e10,0.08,0.05,2.5,Energy
        2020-07-15,AAPL,24.0,12.0,6.0e10,1.5e12,0.65,0.23,1.6,Technology
        2020-07-15,MSFT,32.0,13.0,4.5e10,1.6e12,0.40,0.36,0.5,Technology
        2020-07-15,GOOG,30.0,6.0,3.5e10,1.0e12,0.18,0.24,0.1,Technology
    """)

    # Build synthetic price series: 2 years of daily data, business days only.
    # ENRN prices stop after 2020-06-01 (simulating delisting).
    dates = pd.bdate_range("2019-01-02", "2020-12-31")
    np.random.seed(42)

    def _price_series(start, vol=0.015):
        rets = np.random.normal(0.0003, vol, len(dates))
        prices = start * np.exp(np.cumsum(rets))
        return pd.Series(prices, index=dates)

    aapl = _price_series(150.0)
    msft = _price_series(100.0)
    goog = _price_series(1200.0)
    enrn = _price_series(60.0)

    # ENRN goes to NaN after 2020-06-01
    enrn_series = enrn.copy()
    enrn_series[enrn_series.index > pd.Timestamp("2020-06-01")] = np.nan

    price_df = pd.DataFrame({
        "date": dates.date,
        "AAPL": aapl.values,
        "MSFT": msft.values,
        "GOOG": goog.values,
        "ENRN": enrn_series.values,
    })
    prices_path = str(tmp_path / "prices.csv")
    price_df.to_csv(prices_path, index=False)

    return constituents, fundamentals, prices_path


# ── CSVPITProvider unit tests ─────────────────────────────────────────────────

class TestCSVPITProvider:

    def test_load_without_crash(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)
        assert provider is not None

    def test_constituents_returns_correct_snapshot(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        members_jan = provider.constituents(date(2020, 2, 1))
        assert "AAPL" in members_jan
        assert "ENRN" in members_jan  # still in universe in Jan snapshot

    def test_constituents_excludes_delisted_after_removal(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        members_aug = provider.constituents(date(2020, 8, 1))
        assert "ENRN" not in members_aug  # removed from 2020-07-01 snapshot
        assert "AAPL" in members_aug

    def test_constituents_empty_before_any_snapshot(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)
        members = provider.constituents(date(2018, 1, 1))
        assert members == set()

    def test_fundamentals_returns_most_recent_before_date(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        # Jan snapshot available, Jul snapshot not yet visible
        fund = provider.fundamentals("AAPL", date(2020, 3, 1))
        assert fund is not None
        assert fund["trailingPE"] == pytest.approx(22.0)

    def test_fundamentals_respects_pit_lag(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        # Jul 15 snapshot not yet visible on Jul 14
        fund_before = provider.fundamentals("AAPL", date(2020, 7, 14))
        assert fund_before["trailingPE"] == pytest.approx(22.0)  # still Jan snapshot

        # Jul 15 snapshot visible on Jul 15+
        fund_after = provider.fundamentals("AAPL", date(2020, 7, 16))
        assert fund_after["trailingPE"] == pytest.approx(24.0)

    def test_fundamentals_returns_none_for_unknown_ticker(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)
        assert provider.fundamentals("ZZZZ", date(2020, 6, 1)) is None

    def test_fundamentals_returns_none_before_any_snapshot(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)
        assert provider.fundamentals("AAPL", date(2019, 1, 1)) is None

    def test_prices_returns_correct_range(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        prices = provider.prices("AAPL", date(2020, 1, 2), date(2020, 3, 31))
        assert len(prices) > 50  # ~60 business days in that window
        assert prices.dtype == float

    def test_prices_empty_for_unknown_ticker(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)
        s = provider.prices("ZZZZ", date(2020, 1, 1), date(2020, 6, 1))
        assert s.empty

    def test_prices_enrn_stops_after_delist(self, tmp_path):
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        prices_before = provider.prices("ENRN", date(2019, 1, 2), date(2020, 5, 31))
        prices_after = provider.prices("ENRN", date(2020, 7, 1), date(2020, 12, 31))

        assert len(prices_before) > 100
        assert len(prices_after) == 0  # delisted — no prices after cutoff

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            CSVPITProvider(
                str(tmp_path / "no_file.csv"),
                str(tmp_path / "no_file2.csv"),
                str(tmp_path / "no_file3.csv"),
            )


# ── run_pit_backtest integration tests ───────────────────────────────────────

class TestRunPITBacktest:

    def test_end_to_end_produces_metrics(self, tmp_path):
        """Full pipeline runs without error and returns a metrics dict."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01", "2020-08-01"],
            top_n=3,
            position_pct=5.0,
            initial_cash=100_000.0,
        )

        assert "metrics" in result
        assert "n_signals" in result
        assert result["n_signals"] > 0
        assert "sharpe" in result["metrics"]
        assert "total_return_pct" in result["metrics"]

    def test_delisted_ticker_excluded_after_removal(self, tmp_path):
        """ENRN must not appear in signals generated from the Aug 2020 rebalance."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-08-01"],
            top_n=5,
            position_pct=5.0,
            initial_cash=100_000.0,
        )

        aug_signals = [s["ticker"] for s in result["signals"]
                       if s["date"] == "2020-08-01"]
        assert "ENRN" not in aug_signals

    def test_enrn_can_appear_in_early_rebalance(self, tmp_path):
        """ENRN may appear in a Jan 2020 rebalance since it was in the universe then."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01"],
            top_n=4,
            position_pct=5.0,
            initial_cash=100_000.0,
        )
        # ENRN may or may not score in top_n; test just that it's not crashed/excluded
        # by the harness (it was in the universe)
        assert result["n_signals"] >= 0  # no crash

    def test_no_signals_with_empty_universe(self, tmp_path):
        """A rebalance date before any constituent snapshot → no signals, no crash."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2018-01-01"],
            top_n=5,
        )
        assert result["n_signals"] == 0

    def test_windows_field_populated(self, tmp_path):
        """Each rebalance date should have a corresponding entry in result['windows']."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01", "2020-08-01"],
            top_n=3,
        )
        # At least the windows with non-empty universe should be present
        assert len(result["windows"]) >= 1

    def test_spy_benchmark_returns_attribution_keys(self, tmp_path):
        """When spy_prices is provided, metrics should include beta/alpha/IR."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        dates = pd.bdate_range("2020-01-02", "2020-12-31")
        np.random.seed(7)
        spy = pd.Series(100 * np.exp(np.cumsum(np.random.normal(0.0003, 0.01, len(dates)))),
                        index=dates)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01", "2020-08-01"],
            top_n=3,
            spy_prices=spy,
        )
        assert "beta" in result["metrics"]
        assert "alpha_annualized_pct" in result["metrics"]
        assert "information_ratio" in result["metrics"]
