# tests/test_backtest_sue_pit_events.py
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd

from backtesting.backtest_sue_pit import (
    SAMPLE_END,
    SAMPLE_START,
    add_tradable_date,
    build_pit_sue_events,
    restrict_to_pit_universe,
)


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


def test_add_tradable_date_skips_weekend():
    # Friday filing -> tradable date must be the following Monday, not Saturday.
    events = pd.DataFrame([
        {"ticker": "ZZZ", "filed_date": date(2013, 9, 6), "sue": 1.0},  # a Friday
    ])
    result = add_tradable_date(events)
    assert len(result) == 1
    assert result.iloc[0]["tradable_date"] == date(2013, 9, 9)  # the following Monday


def test_add_tradable_date_always_strictly_after_filed_date():
    events = pd.DataFrame([
        {"ticker": "ZZZ", "filed_date": date(2020, 1, 2), "sue": 1.0},
        {"ticker": "ZZZ", "filed_date": date(2020, 6, 15), "sue": -1.0},
    ])
    result = add_tradable_date(events)
    assert len(result) == 2
    assert (result["tradable_date"] > result["filed_date"]).all()


def test_restrict_to_pit_universe_drops_pre_membership_events():
    """Regression coverage for a real finding: a ticker's SUE events that
    predate its actual S&P 500 index inclusion (e.g. FIX/Comfort Systems USA,
    which only joined the index in March 2026) must be dropped, or the
    universe side silently reintroduces survivorship bias even though the
    fundamentals side is PIT-correct. Verified against the real 25,545-event
    set: 6,253 events (24.5%) dropped, concentrated in recently-added names.
    """
    constituents = pd.DataFrame([
        {"date": date(2020, 1, 1), "ticker": "REAL"},
        {"date": date(2023, 1, 1), "ticker": "LATE"},  # joined the index later
    ])
    events = pd.DataFrame([
        {"ticker": "REAL", "filed_date": date(2021, 1, 1), "sue": 1.0},
        {"ticker": "LATE", "filed_date": date(2021, 1, 1), "sue": 1.0},  # before LATE joined
        {"ticker": "LATE", "filed_date": date(2023, 6, 1), "sue": 1.0},  # after LATE joined
    ])
    events = add_tradable_date(events)

    with patch(
        "backtesting.backtest_sue_pit.fetch_sp500_pit_constituents",
        return_value=constituents,
    ):
        result = restrict_to_pit_universe(events)

    kept = set(zip(result["ticker"], result["filed_date"]))
    assert (("REAL", date(2021, 1, 1))) in kept
    assert (("LATE", date(2021, 1, 1))) not in kept  # pre-membership — dropped
    assert (("LATE", date(2023, 6, 1))) in kept  # post-membership — kept


def test_build_pit_sue_events_dedupes_same_day_multi_quarter_filings():
    """Regression test for a real bug found running Task 7 against real
    data: some filings (typically an annual report's "selected quarterly
    financial data" footnote) report SEVERAL distinct historical quarters
    under the same filed date. original_quarterly_eps correctly keeps all
    of them as distinct (cy_year, cy_quarter) rows (they're genuinely
    different periods), but pit_sue_asof(quarterly, as_of) depends only on
    as_of — so iterating per quarter-row generated identical-value duplicate
    "events" on the same day for the same ticker. Found on real data: 1,652
    such rows (659 distinct ticker/date groups), always with matching SUE
    values, confirming duplicated computation of the same signal rather than
    independent information. Must collapse to exactly one event per
    (ticker, filed_date).
    """
    quarterly = pd.DataFrame([
        {"cy_year": 2011, "cy_quarter": 4, "val": 0.70, "filed": "2012-10-26"},
        {"cy_year": 2012, "cy_quarter": 1, "val": 0.46, "filed": "2012-10-26"},
        {"cy_year": 2012, "cy_quarter": 2, "val": 0.79, "filed": "2012-10-26"},
        {"cy_year": 2012, "cy_quarter": 3, "val": 0.78, "filed": "2012-10-26"},
    ])

    with patch("backtesting.backtest_sue_pit._fetch_ticker_cik_map", return_value={"ZZZ": 999}), \
         patch("backtesting.backtest_sue_pit.fetch_companyfacts_eps", return_value=pd.DataFrame()), \
         patch("backtesting.backtest_sue_pit.original_quarterly_eps", return_value=quarterly), \
         patch("backtesting.backtest_sue_pit.pit_sue_asof", return_value=1.23):
        events = build_pit_sue_events({"ZZZ"})

    assert len(events) == 1  # not 4
    assert events.iloc[0]["filed_date"] == date(2012, 10, 26)
    assert events.iloc[0]["sue"] == 1.23
