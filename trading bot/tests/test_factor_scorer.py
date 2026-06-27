import pytest
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
    assert scored.loc["MISSING", "momentum_score"] < scored.loc["MID", "momentum_score"]


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


def test_compute_composite_returns_scores_in_range():
    infos = {t: _make_info() for t in ["A", "B", "C"]}
    momentum = {t: _mom() for t in ["A", "B", "C"]}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
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
    research_spy = mocker.patch(
        "screener.factor_scorer._gather_research_with_momentum",
        side_effect=lambda t, m1, m3: (t, None),
    )
    run_factor_screen(tickers, top_n=2)
    assert research_spy.call_count == len(tickers)
    called_tickers = {call.args[0] for call in research_spy.call_args_list}
    assert called_tickers == set(tickers)
