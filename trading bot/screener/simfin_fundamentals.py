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
