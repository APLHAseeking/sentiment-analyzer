# screener/simfin_fundamentals.py
"""Fetch and cache raw SimFin bulk fundamental datasets for point-in-time
backtesting (income statements, balance sheets, cash-flow statements, plus
the companies/industries reference tables needed for a sector mapping).

SimFin's free tier does NOT include the `derived` dataset (pre-computed
ratios like trailingPE/priceToBook) — confirmed empirically this session:
`https://prod.simfin.com/api/bulk-download/s3?dataset=derived&...` returns
HTTP 500 "Premium dataset selected, please upgrade to at least a BASIC
subscription". This module fetches the raw statements instead; computing
the actual ratios (which also need share-price data) happens later, once
point-in-time prices exist (see docs/PIT_DATA_REQUIREMENTS.md's
fundamentals.csv spec and the harness-wiring step of the Phase 0 plan).

Each dataset's real column set was confirmed via a live API call before
writing this module (not assumed from docs): income/balance/cashflow all
share Ticker/SimFinId/Currency/Fiscal Year/Fiscal Period/Report Date/
Publish Date/Restated Date as their first 8 columns; companies has
Ticker/IndustryId (not a plain sector string); industries maps
IndustryId -> Industry/Sector.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

_BASE_URL = "https://prod.simfin.com/api/bulk-download/s3"
_TIMEOUT_SECONDS = 60


def _api_key() -> str:
    from system.config import settings
    key = settings.credentials.simfin_api_key
    if not key:
        raise RuntimeError("Missing required env var: SIMFIN_API_KEY")
    return key


def _fetch_dataset(dataset: str, market: str = "us", variant: str | None = None) -> pd.DataFrame:
    """Fetch one SimFin bulk dataset (a zip containing one semicolon-delimited CSV)."""
    params = f"dataset={dataset}&market={market}"
    if variant:
        params += f"&variant={variant}"
    url = f"{_BASE_URL}?{params}"
    headers = {"Authorization": "api-key " + _api_key()}
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        with z.open(z.namelist()[0]) as f:
            return pd.read_csv(f, sep=";")


def fetch_simfin_dataset(
    dataset: str, cache_path: Path, market: str = "us", variant: str | None = None,
) -> pd.DataFrame:
    """Fetch (or load from permanent cache) a SimFin bulk dataset.

    Cache never expires, matching this repo's other permanent PIT caches
    (screener/xbrl_pit_sue.py, backtesting/pit_constituents.py,
    screener/ff_factors.py) — historical filings don't get revised after
    the fact (SimFin tracks that separately via its own Restated Date
    column, already preserved in the raw data). Static reference tables
    (companies, industries) are also cached permanently here; delete the
    cache file manually to force a refresh if SimFin's company/industry
    list needs updating.
    """
    if cache_path.exists():
        return pd.read_parquet(cache_path)
    df = _fetch_dataset(dataset, market=market, variant=variant)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    log.info("Fetched SimFin dataset %r (%d rows), cached to %s", dataset, len(df), cache_path)
    return df


_TRAILING_QUARTERS = 4


def compute_fundamentals_snapshots(
    income_df: pd.DataFrame,
    balance_df: pd.DataFrame,
    cashflow_df: pd.DataFrame,
    sector_by_ticker: dict[str, str],
    price_lookup,
) -> pd.DataFrame:
    """Combine raw SimFin statements into PIT-correct ratio snapshots.

    One row per (ticker, Publish Date) — matches docs/PIT_DATA_REQUIREMENTS.md's
    fundamentals.csv schema: date, ticker, trailingPE, priceToBook,
    freeCashflow, marketCap, returnOnEquity, profitMargins, debtToEquity,
    sector. SimFin's free tier has no pre-computed ratios (see module
    docstring), so this is where they're actually computed.

    Trailing-twelve-month figures (P/E, ROE, profit margin, free cash flow)
    use a rolling sum of the trailing `_TRAILING_QUARTERS` quarters' Net
    Income / Revenue / Operating Cash Flow / capex, ordered by each
    ticker's own Report Date (fiscal-period order, not Publish Date —
    filings for different tickers arrive on different schedules, but a
    single ticker's own quarters must roll in the order they occurred).
    Point-in-time ratios (P/B, D/E, market cap) use the single balance-sheet
    snapshot published alongside that quarter. A ticker/quarter with fewer
    than `_TRAILING_QUARTERS` prior quarters on record gets `None` for the
    trailing-figure ratios rather than a partial-window value that would
    silently understate/overstate the trailing figure.

    `price_lookup(ticker, as_of)` must return the latest available price ON
    OR BEFORE `as_of` (never a future price) or None if unavailable — the
    PIT guarantee is the caller's responsibility (see market_data/pit_prices.py).
    Rows with no price available get `None` for the three price-dependent
    fields (trailingPE, priceToBook, marketCap) but keep the price-independent
    ones (returnOnEquity, profitMargins, debtToEquity, sector) rather than
    being dropped entirely.
    """
    key_cols = ["Ticker", "Report Date", "Publish Date"]
    merged = (
        income_df[key_cols + ["Revenue", "Net Income", "Shares (Diluted)"]]
        .merge(
            balance_df[key_cols + ["Total Equity", "Short Term Debt", "Long Term Debt"]],
            on=key_cols, how="inner",
        )
        .merge(
            cashflow_df[key_cols + [
                "Net Cash from Operating Activities",
                "Change in Fixed Assets & Intangibles",
            ]],
            on=key_cols, how="inner",
        )
    )
    merged["Report Date"] = pd.to_datetime(merged["Report Date"])
    merged["Publish Date"] = pd.to_datetime(merged["Publish Date"])
    merged = merged.sort_values(["Ticker", "Report Date"]).reset_index(drop=True)

    grouped = merged.groupby("Ticker", group_keys=False)
    merged["_trailing_revenue"] = grouped["Revenue"].transform(
        lambda s: s.rolling(_TRAILING_QUARTERS, min_periods=_TRAILING_QUARTERS).sum()
    )
    merged["_trailing_net_income"] = grouped["Net Income"].transform(
        lambda s: s.rolling(_TRAILING_QUARTERS, min_periods=_TRAILING_QUARTERS).sum()
    )
    merged["_trailing_op_cashflow"] = grouped["Net Cash from Operating Activities"].transform(
        lambda s: s.rolling(_TRAILING_QUARTERS, min_periods=_TRAILING_QUARTERS).sum()
    )
    merged["_trailing_capex"] = grouped["Change in Fixed Assets & Intangibles"].transform(
        lambda s: s.rolling(_TRAILING_QUARTERS, min_periods=_TRAILING_QUARTERS).sum()
    )

    rows = []
    for _, row in merged.iterrows():
        ticker = row["Ticker"]
        publish_date = row["Publish Date"].date()
        shares = row["Shares (Diluted)"]
        equity = row["Total Equity"]
        debt = (row["Short Term Debt"] or 0) + (row["Long Term Debt"] or 0)
        trailing_revenue = row["_trailing_revenue"]
        trailing_net_income = row["_trailing_net_income"]
        trailing_op_cf = row["_trailing_op_cashflow"]
        trailing_capex = row["_trailing_capex"]

        price = price_lookup(ticker, publish_date)

        trailing_pe = None
        price_to_book = None
        market_cap = None
        if price is not None and shares and shares > 0:
            market_cap = price * shares
            if pd.notna(trailing_net_income) and trailing_net_income > 0:
                trailing_pe = price / (trailing_net_income / shares)
            if equity and equity > 0:
                price_to_book = price / (equity / shares)

        free_cash_flow = (
            trailing_op_cf + trailing_capex
            if pd.notna(trailing_op_cf) and pd.notna(trailing_capex) else None
        )
        return_on_equity = (
            trailing_net_income / equity
            if pd.notna(trailing_net_income) and equity else None
        )
        profit_margins = (
            trailing_net_income / trailing_revenue
            if pd.notna(trailing_net_income) and pd.notna(trailing_revenue)
            and trailing_revenue != 0 else None
        )
        debt_to_equity = debt / equity if equity else None

        rows.append({
            "date": publish_date.isoformat(),
            "ticker": ticker,
            "trailingPE": trailing_pe,
            "priceToBook": price_to_book,
            "freeCashflow": free_cash_flow,
            "marketCap": market_cap,
            "returnOnEquity": return_on_equity,
            "profitMargins": profit_margins,
            "debtToEquity": debt_to_equity,
            "sector": sector_by_ticker.get(ticker),
        })

    return pd.DataFrame(rows)


def sector_map(companies_df: pd.DataFrame, industries_df: pd.DataFrame) -> dict[str, str]:
    """Build {ticker: sector} from the companies + industries reference tables.

    Rows with no Ticker (SimFin carries some non-ticker entities, e.g.
    private-company placeholders) or no IndustryId match are excluded
    rather than mapped to a guessed sector.
    """
    merged = companies_df.merge(
        industries_df[["IndustryId", "Sector"]], on="IndustryId", how="inner",
    )
    merged = merged.dropna(subset=["Ticker"])
    return dict(zip(merged["Ticker"], merged["Sector"]))
