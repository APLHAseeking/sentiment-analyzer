"""Tests for SEC XBRL fundamentals — SUE, accruals, net payout (offline)."""
from datetime import date

import pytest

from screener.xbrl_fundamentals import (
    _completed_quarters,
    _frame_cik_map,
    accruals_ratio,
    fetch_xbrl_factors,
    sue_from_quarterly_eps,
)


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_completed_quarters_newest_first():
    qs = _completed_quarters(date(2026, 7, 7), 13)
    assert qs[0] == (2026, 2)      # Q2 2026 ended June 30 — completed
    assert qs[1] == (2026, 1)
    assert qs[2] == (2025, 4)      # year rollover
    assert qs[-1] == (2023, 2)
    assert len(qs) == 13


def test_completed_quarters_january_steps_into_prior_year():
    qs = _completed_quarters(date(2026, 1, 15), 2)
    assert qs == [(2025, 4), (2025, 3)]


def test_sue_known_value():
    # Latest YoY change 0.4; prior changes [0.1, 0.3] -> pstdev 0.1 -> SUE 4.0.
    eps = [1.4, 1.0, 1.2, 1.0, 1.0, 0.9, 0.9, None, None]
    assert sue_from_quarterly_eps(eps) == pytest.approx(4.0)


def test_sue_none_when_history_too_short():
    assert sue_from_quarterly_eps([1.0, 1.0, 1.0, 1.0, 0.9]) is None


def test_sue_anchor_shifts_past_unfiled_latest_quarter():
    """Slot 0 (just-completed quarter) is empty for ~40 days after quarter end —
    the anchor must shift to the newest available quarter, not return None."""
    eps = [None, 1.4, 1.0, 1.2, 1.0, 1.0, 0.9, 0.9, None, None]
    assert sue_from_quarterly_eps(eps) == pytest.approx(4.0)


def test_sue_none_when_newest_data_too_stale():
    eps = [None, None, None, 1.4, 1.0, 1.2, 1.0, 1.0, 0.9, 0.9]
    assert sue_from_quarterly_eps(eps) is None  # 3 quarters stale > max 2


def test_sue_none_when_seasonal_lag_missing():
    base = [1.4, 1.0, 1.2, 1.0, 1.0, 0.9, 0.9, 0.8, 0.8]
    assert sue_from_quarterly_eps(base[:4] + [None] + base[5:]) is None  # t-4 missing


def test_sue_none_when_too_few_prior_changes():
    # Only one computable prior change -> below _MIN_SUE_CHANGE_OBS.
    eps = [1.4, 1.0, None, None, 1.0, 0.9, None, None, None]
    assert sue_from_quarterly_eps(eps) is None


def test_sue_none_when_prior_changes_flat():
    # All prior YoY changes exactly 0.25 (binary-exact) -> std 0 -> None.
    eps = [2.5, 1.5, 1.5, 1.5, 1.5, 1.25, 1.25, 1.25, 1.25, 1.0, 1.0, 1.0, 1.0]
    prior = [eps[i] - eps[i + 4] for i in range(1, len(eps) - 4)]
    assert set(prior) == {0.25}
    assert sue_from_quarterly_eps(eps) is None


def test_accruals_ratio():
    assert accruals_ratio(100.0, 120.0, 1000.0) == pytest.approx(-0.02)
    assert accruals_ratio(None, 120.0, 1000.0) is None
    assert accruals_ratio(100.0, 120.0, 0.0) is None


def test_frame_cik_map_parses_frames_payload():
    payload = {
        "data": [
            {"cik": 320193, "entityName": "Apple Inc.", "val": 1.52},
            {"cik": "789019", "val": "2.93"},
            {"entityName": "broken row"},
            {"cik": 1, "val": None},
        ]
    }
    assert _frame_cik_map(payload) == {320193: 1.52, 789019: 2.93}
    assert _frame_cik_map(None) == {}
    assert _frame_cik_map({}) == {}


# ── fetch_xbrl_factors (network fully mocked) ───────────────────────────────

def test_fetch_xbrl_factors_assembles_per_ticker(mocker):
    from screener import xbrl_fundamentals as xf
    mocker.patch.object(xf.shelve, "open", side_effect=OSError("no cache"))
    mocker.patch.object(xf, "_fetch_ticker_cik_map", return_value={"AAPL": 320193})

    from screener.xbrl_fundamentals import _EPS_QUARTERS
    eps_seq = [1.4, 1.0, 1.2, 1.0, 1.0, 0.9, 0.9]
    eps_seq += [None] * (_EPS_QUARTERS - len(eps_seq))  # newest first
    annual = {
        "NetIncomeLoss": {320193: 100.0},
        "NetCashProvidedByUsedInOperatingActivities": {320193: 120.0},
        "Assets": {320193: 1000.0},
        "PaymentsForRepurchaseOfCommonStock": {320193: 50.0},
        "PaymentsOfDividends": {},
        "PaymentsOfDividendsCommonStock": {320193: 10.0},
    }
    state = {"i": 0}

    def fake_frame(concept, unit, frame, cache):
        if concept == "EarningsPerShareDiluted":
            v = eps_seq[state["i"]]
            state["i"] += 1
            return {320193: v} if v is not None else {}
        return annual.get(concept, {})

    mocker.patch.object(xf, "_fetch_frame", side_effect=fake_frame)
    out = fetch_xbrl_factors(["AAPL", "ZZZZ"])
    assert out["AAPL"]["sue"] == pytest.approx(4.0)
    assert out["AAPL"]["accruals"] == pytest.approx(-0.02)
    assert out["AAPL"]["net_payout_usd"] == pytest.approx(60.0)  # buyback + common div
    assert out["ZZZZ"] == {"sue": None, "accruals": None, "net_payout_usd": None}


def test_fetch_xbrl_factors_returns_empty_without_cik_map(mocker):
    from screener import xbrl_fundamentals as xf
    mocker.patch.object(xf.shelve, "open", side_effect=OSError("no cache"))
    mocker.patch.object(xf, "_fetch_ticker_cik_map", return_value={})
    frame = mocker.patch.object(xf, "_fetch_frame")
    assert fetch_xbrl_factors(["AAPL"]) == {}
    frame.assert_not_called()


def test_fetch_xbrl_factors_empty_tickers_no_network(mocker):
    from screener import xbrl_fundamentals as xf
    get = mocker.patch.object(xf.requests, "get")
    assert fetch_xbrl_factors([]) == {}
    get.assert_not_called()
