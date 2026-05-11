import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from screener.factor_scorer import (
    _build_factor_df,
    _compute_composite,
    run_factor_screen,
    FactorCandidate,
)


def _make_info(pe=15.0, pb=2.0, fcf=1e9, mcap=10e9, roe=0.15, margin=0.20, de=0.5):
    return {
        "trailingPE": pe,
        "priceToBook": pb,
        "freeCashflow": fcf,
        "marketCap": mcap,
        "returnOnEquity": roe,
        "profitMargins": margin,
        "debtToEquity": de,
    }


def test_build_factor_df_basic():
    infos = {"AAPL": _make_info(), "MSFT": _make_info(pe=30.0, pb=4.0)}
    momentum = {"AAPL": (5.0, 10.0), "MSFT": (1.0, 2.0)}
    df = _build_factor_df(infos, momentum)
    assert set(df.index) == {"AAPL", "MSFT"}
    assert "pe_inv" in df.columns
    assert df.loc["AAPL", "mom_1m"] == pytest.approx(5.0)


def test_build_factor_df_skips_none_info():
    infos = {"AAPL": _make_info(), "BAD": None}
    momentum = {"AAPL": (5.0, 10.0), "BAD": (None, None)}
    df = _build_factor_df(infos, momentum)
    assert "BAD" not in df.index
    assert "AAPL" in df.index


def test_compute_composite_excludes_sparse_data():
    infos = {
        "GOOD": _make_info(),
        "SPARSE": _make_info(pe=None, pb=None, fcf=None, mcap=None),
    }
    momentum = {"GOOD": (5.0, 10.0), "SPARSE": (None, None)}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert "GOOD" in scored.index
    assert "SPARSE" not in scored.index


def test_compute_composite_prefers_low_pe():
    infos = {
        "CHEAP": _make_info(pe=8.0, pb=1.0),
        "EXPENSIVE": _make_info(pe=60.0, pb=6.0),
    }
    momentum = {"CHEAP": (5.0, 10.0), "EXPENSIVE": (5.0, 10.0)}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["CHEAP", "value_score"] > scored.loc["EXPENSIVE", "value_score"]


def test_compute_composite_returns_scores_in_range():
    infos = {t: _make_info() for t in ["A", "B", "C"]}
    momentum = {t: (5.0, 10.0) for t in ["A", "B", "C"]}
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
        "screener.factor_scorer._fetch_info",
        side_effect=lambda t: (t, mock_info),
    )
    prices = {t: [float(100 + i) for i in range(63)] for t in tickers}
    mock_close = pd.DataFrame(prices, index=pd.date_range("2026-01-01", periods=63))
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={t: (float(i), float(i * 2)) for i, t in enumerate(tickers)},
    )
    mocker.patch(
        "screener.factor_scorer.gather_research_batch",
        return_value={t: None for t in tickers},
    )
    result = run_factor_screen(tickers, top_n=3)
    assert len(result) <= 3
    assert all(isinstance(c, FactorCandidate) for c in result)


def test_run_factor_screen_all_none_returns_empty(mocker):
    mocker.patch(
        "screener.factor_scorer._fetch_info",
        side_effect=lambda t: (t, None),
    )
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={"AAPL": (None, None), "MSFT": (None, None)},
    )
    result = run_factor_screen(["AAPL", "MSFT"], top_n=5)
    assert result == []


def test_run_factor_screen_uses_gather_research_batch(mocker):
    mocker.patch(
        "screener.factor_scorer._fetch_info",
        side_effect=lambda t: (t, _make_info()),
    )
    mocker.patch(
        "screener.factor_scorer._fetch_momentum_batch",
        return_value={"AAPL": (5.0, 10.0), "MSFT": (3.0, 8.0)},
    )
    batch_spy = mocker.patch(
        "screener.factor_scorer.gather_research_batch",
        return_value={"AAPL": None, "MSFT": None},
    )
    run_factor_screen(["AAPL", "MSFT"], top_n=2)
    batch_spy.assert_called_once()
    tickers_arg = batch_spy.call_args[0][0]
    assert set(tickers_arg) == {"AAPL", "MSFT"}
