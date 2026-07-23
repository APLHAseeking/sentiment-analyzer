"""Tests for backtesting.pit_data and backtesting.run_strategy_backtest.

All tests are fully offline — no yfinance, no network.
Uses in-memory synthetic fixtures: 4 tickers (AAPL, MSFT, GOOG + ENRN which
'delists' mid-sample), 2 rebalance dates.
"""
from __future__ import annotations

import io
import textwrap
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backtesting.pit_data import CSVPITProvider, PITDataProvider
from backtesting.run_strategy_backtest import run_pit_backtest, _compute_pit_price_factors


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

    def test_result_exposes_raw_equity_series(self, tmp_path):
        """equity_series lets callers compute their own statistics (e.g. a
        HAC t-stat gate on daily excess returns) without duplicating the
        simulation — added for the Phase 0 PIT backtest gate."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01", "2020-08-01"],
            top_n=3,
            position_pct=5.0,
            initial_cash=100_000.0,
        )

        assert "equity_series" in result
        assert isinstance(result["equity_series"], pd.Series)
        assert len(result["equity_series"]) > 0

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

    def test_overlapping_ticker_not_force_closed_and_reopened(self, tmp_path, monkeypatch):
        """A ticker selected in two consecutive rebalance windows must not be
        force-closed and immediately reopened at the second rebalance — that
        pays a phantom round-trip commission and corrupts the backtest P&L
        used to justify the live _REGIME_WEIGHTS."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        from backtesting import run_strategy_backtest as rsb
        original_simulate = rsb.simulate_portfolio
        captured: dict = {}

        def _capture_and_delegate(*args, **kwargs):
            captured["forced_closes"] = kwargs.get("forced_closes")
            return original_simulate(*args, **kwargs)

        monkeypatch.setattr(rsb, "simulate_portfolio", _capture_and_delegate)

        # top_n=4 with at most 4 candidates per window means every candidate
        # is selected regardless of score — AAPL/MSFT/GOOG are in both the
        # 2020-02-01 and 2020-08-01 universes (only ENRN delists in between),
        # so they must persist, not round-trip.
        rsb.run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01", "2020-08-01"],
            top_n=4,
            position_pct=5.0,
            initial_cash=100_000.0,
        )

        forced_closes = captured["forced_closes"]
        second_window_forced = forced_closes.get("2020-08-01", [])
        assert "AAPL" not in second_window_forced
        assert "MSFT" not in second_window_forced
        assert "GOOG" not in second_window_forced

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

    def test_score_column_selects_alternate_top_n(self, tmp_path, monkeypatch):
        """score_column lets a caller rank/select by a single sleeve instead
        of composite_score, without touching screener/factor_scorer.py — the
        seam the Phase 0 follow-up sleeve-decomposition backtest relies on."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        from backtesting import run_strategy_backtest as rsb

        fake_scored = pd.DataFrame(
            {
                "composite_score": [90, 10, 50],
                "value_score": [10, 90, 50],
                "sector": ["Technology", "Technology", "Energy"],
            },
            index=["AAPL", "MSFT", "GOOG"],
        )
        monkeypatch.setattr(rsb, "_build_factor_df", lambda *a, **k: fake_scored)
        monkeypatch.setattr(rsb, "_compute_composite", lambda df, regime_label=None: fake_scored)

        composite_result = rsb.run_pit_backtest(
            provider=provider, rebalance_dates=["2020-02-01"], top_n=1,
        )
        value_result = rsb.run_pit_backtest(
            provider=provider, rebalance_dates=["2020-02-01"], top_n=1,
            score_column="value_score",
        )

        assert [s["ticker"] for s in composite_result["signals"]] == ["AAPL"]
        assert [s["ticker"] for s in value_result["signals"]] == ["MSFT"]
        assert value_result["signals"][0]["conviction"] == 90

    def test_score_transform_can_derive_a_new_column(self, tmp_path, monkeypatch):
        """score_transform lets a caller add a derived score column (e.g. a
        composite variant excluding one sleeve) purely inside the backtest
        driver, before the top_n selection — relied on by the Phase 0
        follow-up's ex-low-vol composite variant."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        from backtesting import run_strategy_backtest as rsb

        fake_scored = pd.DataFrame(
            {
                "composite_score": [90, 10, 50],
                "value_score": [10, 90, 50],
                "quality_score": [10, 90, 50],
                "sector": ["Technology", "Technology", "Energy"],
            },
            index=["AAPL", "MSFT", "GOOG"],
        )
        monkeypatch.setattr(rsb, "_build_factor_df", lambda *a, **k: fake_scored)
        monkeypatch.setattr(rsb, "_compute_composite", lambda df, regime_label=None: fake_scored)

        def _derive_ex_composite(df: pd.DataFrame) -> pd.DataFrame:
            df = df.copy()
            df["ex_composite_score"] = (df["value_score"] + df["quality_score"]) / 2
            return df

        result = rsb.run_pit_backtest(
            provider=provider, rebalance_dates=["2020-02-01"], top_n=1,
            score_column="ex_composite_score", score_transform=_derive_ex_composite,
        )

        assert [s["ticker"] for s in result["signals"]] == ["MSFT"]
        assert result["signals"][0]["conviction"] == 90

    def test_signals_include_sector(self, tmp_path):
        """Sector flows from the PIT fundamentals snapshot through to each
        signal dict — needed for the Phase 0 follow-up's financials-sector
        cut, which reads it off the returned signals rather than re-deriving
        it."""
        c, f, p = _make_fixtures(tmp_path)
        provider = CSVPITProvider(c, f, p)

        result = run_pit_backtest(
            provider=provider,
            rebalance_dates=["2020-02-01"],
            top_n=4,
        )
        sectors = {s["ticker"]: s["sector"] for s in result["signals"]}
        assert sectors
        assert all(sector in ("Technology", "Energy") for sector in sectors.values())


# ── _compute_pit_price_factors window-anchoring tests ──────────────────────────

class _FixedSeriesPITProvider(PITDataProvider):
    """Minimal PITDataProvider serving one pre-built price series, filtered to
    the requested [start, end] window — used to check that
    _compute_pit_price_factors anchors vol/beta/resid_mom to a trailing bar
    count, not to whatever wider window the caller's start/end happens to
    include."""

    def __init__(self, ticker: str, prices: pd.Series):
        self._ticker = ticker
        self._prices = prices

    def constituents(self, as_of):
        return {self._ticker}

    def fundamentals(self, ticker, as_of):
        return None

    def prices(self, ticker, start, end):
        if ticker != self._ticker:
            return pd.Series(dtype=float)
        mask = (self._prices.index.date >= start) & (self._prices.index.date <= end)
        return self._prices[mask]


class TestComputePITPriceFactorsWindow:
    """_compute_pit_price_factors must use the SAME trailing 252-bar window
    that _compute_pit_momentum anchors mom_12m to (line 68: `prices.iloc[-252]`),
    not the full ~278+ bar history the caller's lookback_days happens to fetch.
    """

    N_BARS = 300
    SHIFT = N_BARS - 252  # 48 bars older than the trailing-252 window

    @staticmethod
    def _vol_beta_resid(prices: pd.Series, spy_ret: pd.Series):
        """Independent reimplementation of the vol/beta/resid_mom formula,
        applied directly to whichever `prices` window is passed in — lets the
        test compute the correct trailing-252 answer and the old buggy
        full-window answer without calling the function under test."""
        ret = prices.pct_change().dropna()
        vol = float(ret.std() * (252 ** 0.5) * 100) if ret.std() > 0 else None
        aligned = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
        aligned.columns = ["stock", "spy"]
        spy_var = float(aligned["spy"].var())
        beta = float(aligned["stock"].cov(aligned["spy"]) / spy_var)
        resid = aligned["stock"] - beta * aligned["spy"]
        resid_window = resid.iloc[:max(0, len(resid) - 21)]
        resid_mom = (
            float(((1 + resid_window).prod() - 1) * 100) if len(resid_window) > 0 else None
        )
        return vol, beta, resid_mom

    def _build_series(self):
        """~300 trading bars where the first 48 (dropped once sliced to the
        trailing 252) are high-vol and strongly negatively correlated with
        SPY, while the last 252 are calmer and positively correlated —
        constructed so the full-window and trailing-252-window computations
        diverge materially."""
        idx = pd.bdate_range("2019-06-03", periods=self.N_BARS)
        rng = np.random.RandomState(123)
        spy_rets = rng.normal(0.0004, 0.01, self.N_BARS - 1)
        spy_prices = 300.0 * np.exp(np.cumsum(np.concatenate([[0.0], spy_rets])))
        spy = pd.Series(spy_prices, index=idx)

        stock_rets = np.empty(self.N_BARS - 1)
        old_noise = rng.normal(0.0, 0.03, self.SHIFT)
        stock_rets[: self.SHIFT] = -5.0 * spy_rets[: self.SHIFT] + old_noise
        recent_len = self.N_BARS - 1 - self.SHIFT
        recent_noise = rng.normal(0.0005, 0.004, recent_len)
        stock_rets[self.SHIFT :] = 1.0 * spy_rets[self.SHIFT :] + recent_noise

        stock_prices = 100.0 * np.exp(np.cumsum(np.concatenate([[0.0], stock_rets])))
        stock = pd.Series(stock_prices, index=idx)
        return stock, spy

    def test_price_factors_anchor_to_trailing_252_bars_like_momentum(self):
        stock, spy = self._build_series()
        as_of = stock.index[-1].date()
        provider = _FixedSeriesPITProvider("TEST", stock)

        # lookback_days=500 so the provider's window comfortably covers all
        # ~300 bars (mirrors the real caller fetching more history than the
        # 252-bar window mom_12m/vol/beta should be anchored to).
        result = _compute_pit_price_factors(
            provider, ["TEST"], as_of, spy_prices=spy, lookback_days=500,
        )
        vol, beta, resid_mom = result["TEST"]
        assert vol is not None and beta is not None and resid_mom is not None

        full_prices = provider.prices("TEST", as_of - timedelta(days=540), as_of)
        assert len(full_prices) > 278  # sanity: caller's window is wider than 252 bars

        spy_ret_full = spy.pct_change().dropna()
        correct_vol, correct_beta, correct_resid = self._vol_beta_resid(
            full_prices.iloc[-252:], spy_ret_full
        )
        buggy_vol, buggy_beta, buggy_resid = self._vol_beta_resid(
            full_prices, spy_ret_full
        )

        # Function must match the trailing-252-bar computation ...
        assert vol == pytest.approx(correct_vol, rel=1e-9)
        assert beta == pytest.approx(correct_beta, rel=1e-9)
        assert resid_mom == pytest.approx(correct_resid, rel=1e-9)

        # ... and must NOT match the old buggy full-window computation — the
        # constructed regime shift makes these materially different.
        assert abs(vol - buggy_vol) > 5.0
        assert abs(beta - buggy_beta) > 0.5
        assert abs(resid_mom - buggy_resid) > 5.0
