# tests/test_backtest_sue_pit_events.py
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from backtesting.backtest_sue_pit import SAMPLE_END, SAMPLE_START, build_pit_sue_events


def test_build_pit_sue_events_passes_full_history_not_window_truncated():
    """Regression test for a real bug found while running Task 7 against real
    data: an earlier version filtered the quarterly history down to
    [SAMPLE_START, SAMPLE_END] BEFORE computing SUE, starving the seasonal-
    random-walk denominator of legitimate pre-window history it should have
    access to (using genuinely-filed older quarters isn't look-ahead — they
    were real, PIT-known history as of any as_of date after them). This
    occasionally produced a near-zero-variance denominator and an absurd SUE
    value (reproduced concretely on real PTC data: 7.2e15 with a 43-quarter
    truncated history vs 1.3 with PTC's full 51-quarter history). The sample
    window must only control which events are OUTPUT, not what history
    pit_sue_asof is allowed to see.
    """
    quarterly = pd.DataFrame([
        {"cy_year": 2008, "cy_quarter": 1, "val": 0.10, "filed": "2008-05-01"},
        {"cy_year": 2008, "cy_quarter": 2, "val": 0.20, "filed": "2008-08-01"},
        {"cy_year": 2009, "cy_quarter": 1, "val": 0.30, "filed": "2009-05-01"},
        {"cy_year": 2013, "cy_quarter": 1, "val": 0.40, "filed": "2013-05-01"},
        {"cy_year": 2013, "cy_quarter": 2, "val": 0.50, "filed": "2013-08-01"},
    ])
    seen_lengths: list[int] = []

    def _fake_pit_sue_asof(q, as_of):
        seen_lengths.append(len(q))
        return 0.5  # value irrelevant to this test — only the input history matters

    with patch("backtesting.backtest_sue_pit._fetch_ticker_cik_map", return_value={"ZZZ": 999}), \
         patch("backtesting.backtest_sue_pit.fetch_companyfacts_eps", return_value=pd.DataFrame()), \
         patch("backtesting.backtest_sue_pit.original_quarterly_eps", return_value=quarterly), \
         patch("backtesting.backtest_sue_pit.pit_sue_asof", side_effect=_fake_pit_sue_asof):
        events = build_pit_sue_events({"ZZZ"})

    # Only the two 2013 quarters are inside [SAMPLE_START, SAMPLE_END] — those
    # are the only events that should be OUTPUT.
    assert len(events) == 2
    assert events["filed_date"].min() >= SAMPLE_START
    assert events["filed_date"].max() <= SAMPLE_END

    # But pit_sue_asof must have been called with the FULL 5-row history each
    # time, not a window-truncated slice — this is the actual bug/fix.
    assert seen_lengths == [5, 5]


def test_build_pit_sue_events_excludes_implausible_sue_values():
    """Second, independent safety net: even with the full-history fix above,
    a residual near-zero-variance case (e.g. a company with genuinely flat
    EPS for an unusually long stretch) should not silently produce a
    meaningless extreme SUE value in the output — excluded, not clipped,
    matching this module's "unknown/degenerate is not neutral" convention.
    """
    quarterly = pd.DataFrame([
        {"cy_year": 2013, "cy_quarter": 1, "val": 0.40, "filed": "2013-05-01"},
        {"cy_year": 2013, "cy_quarter": 2, "val": 0.50, "filed": "2013-08-01"},
    ])

    def _fake_pit_sue_asof(q, as_of):
        return 1e12 if str(as_of) == "2013-05-01" else 0.8

    with patch("backtesting.backtest_sue_pit._fetch_ticker_cik_map", return_value={"ZZZ": 999}), \
         patch("backtesting.backtest_sue_pit.fetch_companyfacts_eps", return_value=pd.DataFrame()), \
         patch("backtesting.backtest_sue_pit.original_quarterly_eps", return_value=quarterly), \
         patch("backtesting.backtest_sue_pit.pit_sue_asof", side_effect=_fake_pit_sue_asof):
        events = build_pit_sue_events({"ZZZ"})

    assert len(events) == 1
    assert events.iloc[0]["sue"] == 0.8
