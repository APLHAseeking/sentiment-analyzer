# tests/test_simfin_fundamentals_ratios.py
"""Tests for compute_fundamentals_snapshots — combining raw SimFin statements
+ price data into the actual PIT ratio schema. Hand-computed fixtures so
every ratio can be checked against a value worked out by hand, not just
"did it run without error"."""
from __future__ import annotations

import pandas as pd
import pytest

from screener.simfin_fundamentals import compute_fundamentals_snapshots

_KEY_COLS = ["Ticker", "Report Date", "Publish Date"]


def _quarterly_rows(ticker: str, quarters: list[tuple[str, str, float, float]]):
    """quarters: list of (report_date, publish_date, revenue, net_income)."""
    return [
        {"Ticker": ticker, "Report Date": r, "Publish Date": p,
         "Revenue": rev, "Net Income": ni, "Shares (Diluted)": 10.0}
        for r, p, rev, ni in quarters
    ]


_AAA_QUARTERS = [
    ("2020-03-31", "2020-05-01", 100.0, 10.0),
    ("2020-06-30", "2020-08-01", 110.0, 11.0),
    ("2020-09-30", "2020-11-01", 120.0, 12.0),
    ("2020-12-31", "2021-02-01", 130.0, 13.0),  # first row with a full trailing 4Q window
    ("2021-03-31", "2021-05-01", 140.0, 14.0),
]


def _make_dfs(equity_by_report_date: dict[str, float]):
    income_df = pd.DataFrame(_quarterly_rows("AAA", _AAA_QUARTERS))
    balance_rows = []
    cashflow_rows = []
    for i, (report, publish, _, _) in enumerate(_AAA_QUARTERS):
        equity = equity_by_report_date[report]
        balance_rows.append({
            "Ticker": "AAA", "Report Date": report, "Publish Date": publish,
            "Total Equity": equity, "Short Term Debt": 5.0, "Long Term Debt": 15.0,
        })
        # Operating cash flow tracks net income + 2, capex is a steady -2/quarter.
        net_income = _AAA_QUARTERS[i][3]
        cashflow_rows.append({
            "Ticker": "AAA", "Report Date": report, "Publish Date": publish,
            "Net Cash from Operating Activities": net_income + 2.0,
            "Change in Fixed Assets & Intangibles": -2.0,
        })
    return income_df, pd.DataFrame(balance_rows), pd.DataFrame(cashflow_rows)


_EQUITY_BY_REPORT_DATE = {
    "2020-03-31": 200.0, "2020-06-30": 210.0, "2020-09-30": 220.0,
    "2020-12-31": 230.0, "2021-03-31": 240.0,
}


def test_trailing_ratios_none_before_four_quarters_of_history():
    """The first 3 quarters (only 1-3 prior quarters on record) must get
    None for every trailing-figure ratio — a partial-window sum would
    silently understate the real trailing-12-month figure."""
    income_df, balance_df, cashflow_df = _make_dfs(_EQUITY_BY_REPORT_DATE)

    def price_lookup(ticker, as_of):
        return 100.0

    result = compute_fundamentals_snapshots(
        income_df, balance_df, cashflow_df, {"AAA": "Technology"}, price_lookup,
    )

    first_three = result.iloc[:3]
    assert first_three["freeCashflow"].isna().all()
    assert first_three["returnOnEquity"].isna().all()
    assert first_three["profitMargins"].isna().all()
    assert first_three["trailingPE"].isna().all()


def test_point_in_time_ratios_available_even_without_full_trailing_window():
    """priceToBook/marketCap/debtToEquity don't need a trailing window —
    they must be populated from row 1, not withheld until row 4."""
    income_df, balance_df, cashflow_df = _make_dfs(_EQUITY_BY_REPORT_DATE)

    def price_lookup(ticker, as_of):
        return 100.0

    result = compute_fundamentals_snapshots(
        income_df, balance_df, cashflow_df, {"AAA": "Technology"}, price_lookup,
    )

    first_row = result.iloc[0]
    assert first_row["priceToBook"] == pytest.approx(100.0 / (200.0 / 10.0))  # 5.0
    assert first_row["marketCap"] == pytest.approx(100.0 * 10.0)  # 1000.0
    assert first_row["debtToEquity"] == pytest.approx((5.0 + 15.0) / 200.0)


def test_trailing_ratios_correct_on_fourth_quarter():
    """Hand-computed: Q4 2020-12-31 is the first row with 4 prior quarters.
    trailing revenue = 100+110+120+130=460, trailing net income =
    10+11+12+13=46, trailing op cash flow = 12+13+14+15=54 (net_income+2 per
    quarter), trailing capex = -2*4=-8."""
    income_df, balance_df, cashflow_df = _make_dfs(_EQUITY_BY_REPORT_DATE)

    def price_lookup(ticker, as_of):
        return 92.0

    result = compute_fundamentals_snapshots(
        income_df, balance_df, cashflow_df, {"AAA": "Technology"}, price_lookup,
    )

    q4 = result.iloc[3]
    assert q4["date"] == "2021-02-01"
    assert q4["freeCashflow"] == pytest.approx(54.0 - 8.0)  # 46.0
    assert q4["returnOnEquity"] == pytest.approx(46.0 / 230.0)
    assert q4["profitMargins"] == pytest.approx(46.0 / 460.0)
    assert q4["trailingPE"] == pytest.approx(92.0 / (46.0 / 10.0))  # 20.0
    assert q4["priceToBook"] == pytest.approx(92.0 / (230.0 / 10.0))  # 4.0
    assert q4["marketCap"] == pytest.approx(92.0 * 10.0)  # 920.0
    assert q4["sector"] == "Technology"


def test_missing_price_leaves_price_dependent_fields_none_but_keeps_others():
    """A ticker/date with no price available must get None for
    trailingPE/priceToBook/marketCap specifically, but NOT lose
    returnOnEquity/profitMargins/debtToEquity, which don't need a price."""
    income_df, balance_df, cashflow_df = _make_dfs(_EQUITY_BY_REPORT_DATE)

    def price_lookup(ticker, as_of):
        return None  # simulates a PIT price gap

    result = compute_fundamentals_snapshots(
        income_df, balance_df, cashflow_df, {"AAA": "Technology"}, price_lookup,
    )

    q4 = result.iloc[3]
    assert q4["trailingPE"] is None
    assert q4["priceToBook"] is None
    assert q4["marketCap"] is None
    assert q4["returnOnEquity"] == pytest.approx(46.0 / 230.0)
    assert q4["profitMargins"] == pytest.approx(46.0 / 460.0)
    assert q4["debtToEquity"] == pytest.approx(20.0 / 230.0)


def test_negative_trailing_earnings_gives_none_pe_not_negative_ratio():
    """A negative trailing-12-month net income must not produce a negative
    or nonsensical trailingPE — matches yfinance's own convention of
    omitting P/E for negative earnings (screener/factor_scorer.py already
    treats this the same way for the live pipeline)."""
    quarters = [
        ("2020-03-31", "2020-05-01", 100.0, -50.0),
        ("2020-06-30", "2020-08-01", 100.0, -50.0),
        ("2020-09-30", "2020-11-01", 100.0, -50.0),
        ("2020-12-31", "2021-02-01", 100.0, -50.0),
    ]
    income_df = pd.DataFrame(_quarterly_rows("BBB", quarters))
    balance_df = pd.DataFrame([
        {"Ticker": "BBB", "Report Date": r, "Publish Date": p,
         "Total Equity": 500.0, "Short Term Debt": 1.0, "Long Term Debt": 1.0}
        for r, p, _, _ in quarters
    ])
    cashflow_df = pd.DataFrame([
        {"Ticker": "BBB", "Report Date": r, "Publish Date": p,
         "Net Cash from Operating Activities": -10.0,
         "Change in Fixed Assets & Intangibles": -1.0}
        for r, p, _, _ in quarters
    ])

    def price_lookup(ticker, as_of):
        return 50.0

    result = compute_fundamentals_snapshots(
        income_df, balance_df, cashflow_df, {"BBB": "Healthcare"}, price_lookup,
    )

    last_row = result.iloc[-1]
    assert last_row["trailingPE"] is None
    assert last_row["returnOnEquity"] == pytest.approx(-200.0 / 500.0)  # still computed
