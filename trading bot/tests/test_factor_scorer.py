import pytest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock
from screener.factor_scorer import (
    _build_factor_df,
    _compute_composite,
    run_factor_screen,
    FactorCandidate,
)


def _make_info(
    pe=15.0, pb=2.0, fcf=1e9, mcap=10e9, roe=0.15, margin=0.20, de=0.5,
    evebitda=10.0, gross_margin=0.40, current_ratio=1.5, earnings_growth=0.10,
):
    return {
        "trailingPE": pe,
        "priceToBook": pb,
        "freeCashflow": fcf,
        "marketCap": mcap,
        "returnOnEquity": roe,
        "profitMargins": margin,
        "debtToEquity": de,
        "enterpriseToEbitda": evebitda,
        "grossMargins": gross_margin,
        "currentRatio": current_ratio,
        "earningsGrowth": earnings_growth,
    }


# 4-tuple: (mom_1m, mom_12m, mom_6m, high52_ratio)
def _mom(m1=5.0, m12=10.0, m6=6.0, h52=0.95):
    return (m1, m12, m6, h52)


# 3-tuple: (realized_vol_pct, beta, resid_mom_pct)
def _pf(vol=20.0, beta=1.0, resid=5.0):
    return (vol, beta, resid)


def test_build_factor_df_basic():
    infos = {"AAPL": _make_info(), "MSFT": _make_info(pe=30.0, pb=4.0)}
    momentum = {"AAPL": _mom(), "MSFT": _mom(m1=1.0, m12=2.0)}
    df = _build_factor_df(infos, momentum)
    assert set(df.index) == {"AAPL", "MSFT"}
    assert "pe_inv" in df.columns
    assert df.loc["AAPL", "mom_1m"] == pytest.approx(5.0)


def test_build_factor_df_new_fields_populated():
    """New yfinance.info fields are extracted and negated correctly."""
    infos = {"AAPL": _make_info(evebitda=12.0, gross_margin=0.45, current_ratio=2.0, earnings_growth=0.15)}
    momentum = {"AAPL": _mom(m6=7.0, h52=0.92)}
    df = _build_factor_df(infos, momentum)
    assert df.loc["AAPL", "evebitda_inv"] == pytest.approx(-12.0)
    assert df.loc["AAPL", "gross_margin"] == pytest.approx(0.45)
    assert df.loc["AAPL", "current_ratio"] == pytest.approx(2.0)
    assert df.loc["AAPL", "earnings_growth"] == pytest.approx(0.15)
    assert df.loc["AAPL", "mom_6m"] == pytest.approx(7.0)
    assert df.loc["AAPL", "high52_ratio"] == pytest.approx(0.92)


def test_build_factor_df_evebitda_negative_becomes_none():
    """Negative/zero EV/EBITDA (distressed firm) must not invert to positive."""
    infos = {"BAD": _make_info(evebitda=-5.0)}
    momentum = {"BAD": _mom()}
    df = _build_factor_df(infos, momentum)
    assert df.loc["BAD", "evebitda_inv"] is None or pd.isna(df.loc["BAD", "evebitda_inv"])


def test_build_factor_df_skips_none_info():
    infos = {"AAPL": _make_info(), "BAD": None}
    momentum = {"AAPL": _mom(), "BAD": (None, None, None, None)}
    df = _build_factor_df(infos, momentum)
    assert "BAD" not in df.index
    assert "AAPL" in df.index


def test_compute_composite_excludes_sparse_data():
    infos = {
        "GOOD": _make_info(),
        "SPARSE": _make_info(pe=None, pb=None, fcf=None, mcap=None),
    }
    momentum = {"GOOD": _mom(), "SPARSE": (None, None, None, None)}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert "GOOD" in scored.index
    assert "SPARSE" not in scored.index


def test_compute_composite_prefers_low_pe():
    infos = {
        "CHEAP": _make_info(pe=8.0, pb=1.0),
        "EXPENSIVE": _make_info(pe=60.0, pb=6.0),
    }
    momentum = {"CHEAP": _mom(), "EXPENSIVE": _mom()}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["CHEAP", "value_score"] > scored.loc["EXPENSIVE", "value_score"]


def test_compute_composite_prefers_low_evebitda():
    """Lower EV/EBITDA (cheaper) should produce a higher value score."""
    infos = {
        "CHEAP": _make_info(evebitda=6.0),
        "EXPENSIVE": _make_info(evebitda=30.0),
    }
    momentum = {"CHEAP": _mom(), "EXPENSIVE": _mom()}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["CHEAP", "value_score"] > scored.loc["EXPENSIVE", "value_score"]


def test_compute_composite_prefers_high_gross_margin():
    """Higher gross margin (Novy-Marx quality) should score better in quality."""
    infos = {
        "HIGH_GM": _make_info(gross_margin=0.70),
        "LOW_GM": _make_info(gross_margin=0.10),
    }
    momentum = {"HIGH_GM": _mom(), "LOW_GM": _mom()}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["HIGH_GM", "quality_score"] > scored.loc["LOW_GM", "quality_score"]


def test_compute_composite_momentum_uses_three_signals():
    """Ticker near 52-week high with strong 6m momentum should outscore one at 52-week low."""
    infos = {t: _make_info() for t in ["STRONG", "WEAK"]}
    momentum = {
        "STRONG": _mom(m12=30.0, m6=20.0, h52=0.98),
        "WEAK":   _mom(m12=-20.0, m6=-15.0, h52=0.60),
    }
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["STRONG", "momentum_score"] > scored.loc["WEAK", "momentum_score"]


def test_compute_composite_missing_momentum_scored_neutrally():
    """Missing momentum signals (thin-history names) must not be scored as the worst
    possible momentum (0) — they should land neutrally."""
    infos = {t: _make_info() for t in ["MISSING", "WORST", "MID", "BEST"]}
    momentum = {
        "MISSING": (None, None, None, None),
        "WORST":   _mom(m12=-50.0, m6=-30.0, h52=0.50),
        "MID":     _mom(m12=0.0,   m6=0.0,   h52=0.75),
        "BEST":    _mom(m12=100.0, m6=60.0,  h52=0.99),
    }
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["MISSING", "momentum_score"] > scored.loc["WORST", "momentum_score"]
    # With centered ranks a true median name sits exactly at the 0.5 neutral
    # imputation point, so MISSING and MID legitimately tie at the midpoint.
    assert scored.loc["MISSING", "momentum_score"] <= scored.loc["MID", "momentum_score"]
    assert scored.loc["MISSING", "momentum_score"] < scored.loc["BEST", "momentum_score"]


def test_centered_rank_no_small_group_bias():
    """Centered percentile ranks must average 0.5 for any group size — plain
    rank(pct=True) averages (n+1)/2n, inflating small sectors (A4 fix)."""
    from screener.factor_scorer import _centered_rank
    small = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0]})
    large = pd.DataFrame({"x": list(map(float, range(100)))})
    assert _centered_rank(small)["x"].mean() == pytest.approx(0.5)
    assert _centered_rank(large)["x"].mean() == pytest.approx(0.5)
    # NaNs stay NaN and don't distort the centering of the rest
    with_nan = pd.DataFrame({"x": [1.0, np.nan, 3.0]})
    ranked = _centered_rank(with_nan)["x"]
    assert pd.isna(ranked.iloc[1])
    assert ranked.dropna().mean() == pytest.approx(0.5)


def test_compute_composite_missing_new_fields_degrade_gracefully():
    """When new yfinance.info fields are missing, scores stay valid (skipna=True)."""
    infos = {
        "A": _make_info(evebitda=None, gross_margin=None, current_ratio=None, earnings_growth=None),
        "B": _make_info(),
    }
    momentum = {"A": _mom(), "B": _mom()}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert "A" in scored.index
    assert (scored["composite_score"] >= 0).all()
    assert (scored["composite_score"] <= 99).all()


def test_short_interest_screen_excludes_crowded_names():
    """Names shorted above UniverseConfig.max_short_pct_float (default 20%) are
    excluded outright; missing short data passes (unknown != crowded)."""
    infos = {
        "CROWDED": {**_make_info(), "shortPercentOfFloat": 0.35},  # 35% of float
        "NORMAL": {**_make_info(), "shortPercentOfFloat": 0.02},
        "UNKNOWN": _make_info(),
    }
    momentum = {t: _mom() for t in infos}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert "CROWDED" not in scored.index
    assert "NORMAL" in scored.index
    assert "UNKNOWN" in scored.index


def test_short_interest_screen_disabled_when_zero(mocker):
    from screener import factor_scorer
    from system.config import Settings, UniverseConfig
    mocker.patch.object(factor_scorer, "settings",
                        Settings(universe=UniverseConfig(max_short_pct_float=0.0)))
    infos = {"CROWDED": {**_make_info(), "shortPercentOfFloat": 0.35}, "B": _make_info()}
    momentum = {t: _mom() for t in infos}
    scored = _compute_composite(_build_factor_df(infos, momentum))
    assert "CROWDED" in scored.index


def test_compute_composite_returns_scores_in_range():
    infos = {t: _make_info() for t in ["A", "B", "C"]}
    momentum = {t: _mom() for t in ["A", "B", "C"]}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert (scored["composite_score"] >= 0).all()
    assert (scored["composite_score"] <= 99).all()


def test_build_factor_df_price_factors_populated():
    """Price factors are inverted correctly: lower vol/beta -> higher ranked signal."""
    infos = {"AAPL": _make_info()}
    df = _build_factor_df(infos, {"AAPL": _mom()}, {"AAPL": _pf(vol=18.0, beta=0.8, resid=12.0)})
    assert df.loc["AAPL", "vol_inv"] == pytest.approx(-18.0)
    assert df.loc["AAPL", "beta_inv"] == pytest.approx(-0.8)
    assert df.loc["AAPL", "resid_mom"] == pytest.approx(12.0)


def test_compute_composite_prefers_low_vol():
    """A low-volatility, low-beta name should outscore a high-vol, high-beta peer."""
    infos = {"CALM": _make_info(), "WILD": _make_info()}
    momentum = {"CALM": _mom(), "WILD": _mom()}
    price_factors = {"CALM": _pf(vol=12.0, beta=0.6), "WILD": _pf(vol=45.0, beta=1.8)}
    df = _build_factor_df(infos, momentum, price_factors)
    scored = _compute_composite(df)
    assert scored.loc["CALM", "low_vol_score"] > scored.loc["WILD", "low_vol_score"]


def test_compute_composite_residual_momentum_feeds_momentum_sleeve():
    """Higher residual momentum lifts the momentum sleeve when other signals tie."""
    infos = {"HIGH": _make_info(), "LOW": _make_info()}
    momentum = {"HIGH": _mom(), "LOW": _mom()}
    price_factors = {"HIGH": _pf(resid=30.0), "LOW": _pf(resid=-10.0)}
    df = _build_factor_df(infos, momentum, price_factors)
    scored = _compute_composite(df)
    assert scored.loc["HIGH", "momentum_score"] > scored.loc["LOW", "momentum_score"]


def test_residual_momentum_is_largest_momentum_subweight():
    """Residual momentum carries the largest weight inside the momentum sleeve
    (emphasis encoded from the PIT backtest)."""
    from screener.factor_scorer import _MOMENTUM_WEIGHTS
    assert sum(_MOMENTUM_WEIGHTS.values()) == pytest.approx(1.0)
    assert max(_MOMENTUM_WEIGHTS, key=_MOMENTUM_WEIGHTS.get) == "resid_mom"
    # and it is strictly larger than every other sub-signal
    others = {k: v for k, v in _MOMENTUM_WEIGHTS.items() if k != "resid_mom"}
    assert all(_MOMENTUM_WEIGHTS["resid_mom"] > v for v in others.values())


def test_compute_composite_missing_price_factors_neutral():
    """Names without price-factor data must not crash and land near neutral (no worst-case)."""
    infos = {"A": _make_info(), "B": _make_info()}
    momentum = {"A": _mom(), "B": _mom()}
    df = _build_factor_df(infos, momentum)  # no price_factors at all
    scored = _compute_composite(df)
    assert (scored["composite_score"] >= 0).all()
    assert (scored["composite_score"] <= 99).all()
    # neutral 0.5 rank * 33 ~= 16
    assert (scored["low_vol_score"] == 16).all()


def test_compute_composite_sue_feeds_momentum_sleeve():
    """Higher SUE (positive earnings surprise) lifts the momentum sleeve when
    price signals tie (PEAD input)."""
    infos = {"BEAT": _make_info(), "MISS": _make_info()}
    momentum = {"BEAT": _mom(), "MISS": _mom()}
    xbrl = {
        "BEAT": {"sue": 3.0, "accruals": None, "net_payout_usd": None},
        "MISS": {"sue": -3.0, "accruals": None, "net_payout_usd": None},
    }
    df = _build_factor_df(infos, momentum, xbrl=xbrl)
    scored = _compute_composite(df)
    assert scored.loc["BEAT", "momentum_score"] > scored.loc["MISS", "momentum_score"]


def test_compute_composite_net_payout_feeds_value_sleeve():
    """Higher net payout (buybacks + dividends / mcap) lifts the value sleeve."""
    infos = {"PAYER": _make_info(), "HOARDER": _make_info()}
    momentum = {"PAYER": _mom(), "HOARDER": _mom()}
    xbrl = {
        "PAYER": {"sue": None, "accruals": None, "net_payout_usd": 8e8},   # 8% of 10e9 mcap
        "HOARDER": {"sue": None, "accruals": None, "net_payout_usd": 1e7},
    }
    df = _build_factor_df(infos, momentum, xbrl=xbrl)
    assert df.loc["PAYER", "net_payout_yield"] == pytest.approx(0.08)
    scored = _compute_composite(df)
    assert scored.loc["PAYER", "value_score"] > scored.loc["HOARDER", "value_score"]


def test_compute_composite_low_accruals_feed_quality_sleeve():
    """Lower accruals (cash-backed earnings) lift the quality sleeve (Sloan)."""
    infos = {"CASH": _make_info(), "ACCRUED": _make_info()}
    momentum = {"CASH": _mom(), "ACCRUED": _mom()}
    xbrl = {
        "CASH": {"sue": None, "accruals": -0.05, "net_payout_usd": None},
        "ACCRUED": {"sue": None, "accruals": 0.15, "net_payout_usd": None},
    }
    df = _build_factor_df(infos, momentum, xbrl=xbrl)
    scored = _compute_composite(df)
    assert scored.loc["CASH", "quality_score"] > scored.loc["ACCRUED", "quality_score"]


def test_compute_composite_without_xbrl_unchanged():
    """No xbrl dict at all -> columns are absent/NaN and scores stay valid
    (weights renormalise over the present sub-signals)."""
    infos = {"A": _make_info(), "B": _make_info()}
    momentum = {"A": _mom(), "B": _mom(m12=20.0)}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert (scored["composite_score"] >= 0).all()
    assert (scored["composite_score"] <= 99).all()
    assert scored.loc["B", "momentum_score"] > scored.loc["A", "momentum_score"]


def test_regime_weights_sum_to_one():
    """Every regime weight tuple (value, momentum, quality, low_vol, reversal) sums to 1.0."""
    from screener.factor_scorer import _REGIME_WEIGHTS, _DEFAULT_WEIGHTS
    for label, weights in _REGIME_WEIGHTS.items():
        assert len(weights) == 5, label
        assert sum(weights) == pytest.approx(1.0), label
    assert len(_DEFAULT_WEIGHTS) == 5
    assert sum(_DEFAULT_WEIGHTS) == pytest.approx(1.0)


def test_compute_composite_reversal_prefers_oversold():
    """A recent loser (negative 1m return) should outscore a recent winner on reversal."""
    infos = {"LOSER": _make_info(), "WINNER": _make_info()}
    momentum = {"LOSER": _mom(m1=-20.0), "WINNER": _mom(m1=25.0)}
    df = _build_factor_df(infos, momentum, {"LOSER": _pf(), "WINNER": _pf()})
    scored = _compute_composite(df)
    assert scored.loc["LOSER", "reversal_score"] > scored.loc["WINNER", "reversal_score"]


def test_reversal_weighted_up_in_neutral_vs_bull():
    """Reversal carries more composite weight in neutral than in bull (regime gating)."""
    from screener.factor_scorer import _REGIME_WEIGHTS
    assert _REGIME_WEIGHTS["neutral"][4] > _REGIME_WEIGHTS["bull"][4]


def test_compute_composite_in_range_with_price_factors():
    infos = {t: _make_info() for t in ["A", "B", "C"]}
    momentum = {t: _mom() for t in ["A", "B", "C"]}
    price_factors = {"A": _pf(vol=15.0, beta=0.7), "B": _pf(vol=30.0, beta=1.3), "C": _pf()}
    df = _build_factor_df(infos, momentum, price_factors)
    for regime in ["crash", "bear", "neutral", "bull", "euphoria", "melt-up", "deep-bear"]:
        scored = _compute_composite(df, regime_label=regime)
        assert (scored["composite_score"] >= 0).all()
        assert (scored["composite_score"] <= 99).all()


def test_run_factor_screen_empty_tickers():
    result = run_factor_screen([], top_n=5)
    assert result == []


def test_run_factor_screen_returns_top_n(mocker):
    tickers = [f"T{i}" for i in range(10)]
    mock_info = _make_info()
    mocker.patch(
        "screener.factor_scorer._fetch_all_infos",
        return_value={t: mock_info for t in tickers},
    )
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={t: (float(i), float(i * 2), float(i), 0.9) for i, t in enumerate(tickers)},
    )
    mocker.patch(
        "screener.factor_scorer._fetch_price_factors_batch",
        return_value={t: (20.0, 1.0, float(i)) for i, t in enumerate(tickers)},
    )
    mocker.patch("screener.factor_scorer._fetch_xbrl_safe", return_value={})
    mocker.patch(
        "screener.factor_scorer._gather_research_with_momentum",
        side_effect=lambda t, m1, m3: (t, None),
    )
    result = run_factor_screen(tickers, top_n=3)
    assert len(result) <= 3
    assert all(isinstance(c, FactorCandidate) for c in result)


def test_run_factor_screen_all_none_returns_empty(mocker):
    mocker.patch(
        "screener.factor_scorer._fetch_all_infos",
        return_value={"AAPL": None, "MSFT": None},
    )
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={"AAPL": (None, None, None, None), "MSFT": (None, None, None, None)},
    )
    mocker.patch(
        "screener.factor_scorer._fetch_price_factors_batch",
        return_value={"AAPL": (None, None, None), "MSFT": (None, None, None)},
    )
    mocker.patch("screener.factor_scorer._fetch_xbrl_safe", return_value={})
    result = run_factor_screen(["AAPL", "MSFT"], top_n=5)
    assert result == []


def test_run_factor_screen_calls_research_for_top_tickers(mocker):
    """run_factor_screen calls _gather_research_with_momentum for each top ticker."""
    tickers = ["AAPL", "MSFT"]
    mocker.patch(
        "screener.factor_scorer._fetch_all_infos",
        return_value={t: _make_info() for t in tickers},
    )
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={"AAPL": (5.0, 10.0, 6.0, 0.95), "MSFT": (3.0, 8.0, 5.0, 0.88)},
    )
    mocker.patch(
        "screener.factor_scorer._fetch_price_factors_batch",
        return_value={"AAPL": (18.0, 0.9, 7.0), "MSFT": (25.0, 1.2, 4.0)},
    )
    mocker.patch("screener.factor_scorer._fetch_xbrl_safe", return_value={})
    research_spy = mocker.patch(
        "screener.factor_scorer._gather_research_with_momentum",
        side_effect=lambda t, m1, m3: (t, None),
    )
    run_factor_screen(tickers, top_n=2)
    assert research_spy.call_count == len(tickers)
    called_tickers = {call.args[0] for call in research_spy.call_args_list}
    assert called_tickers == set(tickers)


def test_fetch_price_factors_batch_real_computation():
    """Exercises the REAL cov/var/window-slicing math (not the pre-baked _pf tuple).

    Builds a synthetic stock return series with a known analytic relationship to
    SPY: stock_ret = beta_true * spy_ret + idio_drift, where idio_drift is a
    *constant* (no random noise). A constant additive term doesn't change
    covariance or std relative to SPY, so beta and vol are recoverable exactly
    (up to floating-point rounding) rather than merely approximately — a wrong
    cov/var formula (e.g. swapped numerator/denominator) would produce a beta
    far from beta_true (e.g. ~1/beta_true), not a slightly-off one.
    """
    from screener.factor_scorer import _fetch_price_factors_batch, _MIN_MOMENTUM_BARS

    n_bars = 320  # comfortably above _MIN_MOMENTUM_BARS (200) plus the 21-bar skip
    dates = pd.bdate_range("2019-01-02", periods=n_bars)
    ret_dates = dates[1:]

    np.random.seed(11)
    beta_true = 1.5
    idio_drift = 0.0005  # constant idiosyncratic daily drift, no noise
    spy_ret = pd.Series(np.random.normal(0.0004, 0.01, n_bars - 1), index=ret_dates)
    stock_ret = beta_true * spy_ret + idio_drift

    spy_close = pd.Series(index=dates, dtype=float)
    spy_close.iloc[0] = 100.0
    spy_close.iloc[1:] = 100.0 * (1 + spy_ret).cumprod().values

    stock_close = pd.Series(index=dates, dtype=float)
    stock_close.iloc[0] = 50.0
    stock_close.iloc[1:] = 50.0 * (1 + stock_ret).cumprod().values

    close = pd.DataFrame({"SYNTH": stock_close})

    result = _fetch_price_factors_batch(["SYNTH"], close=close, spy_close=spy_close)
    vol, beta, resid_mom = result["SYNTH"]

    # 1. beta recovers beta_true almost exactly (additive constant doesn't move cov).
    assert beta == pytest.approx(beta_true, abs=1e-6)

    # 2. vol: stock_ret std == beta_true * spy_ret std (constant drift doesn't
    #    change std either); check the exact analytic value plus a ballpark range.
    expected_vol = float(stock_ret.std() * (252 ** 0.5) * 100)
    assert vol == pytest.approx(expected_vol, rel=1e-6)
    assert 10.0 < vol < 40.0  # ~1.5 * 1%/day * sqrt(252) * 100 ~= 24%

    # 3. resid_mom: residual = stock_ret - beta*spy_ret = idio_drift (constant),
    #    compounded over the window that excludes the last ~21 bars.
    aligned_len = len(ret_dates)
    window_len = aligned_len - 21
    assert window_len > _MIN_MOMENTUM_BARS
    expected_resid_mom = ((1 + idio_drift) ** window_len - 1) * 100
    assert resid_mom == pytest.approx(expected_resid_mom, rel=1e-4)
