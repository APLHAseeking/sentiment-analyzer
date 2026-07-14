# screener/xbrl_pit_sue.py
"""Point-in-time SUE: SEC companyfacts-sourced EPS with true `filed` dates.

Companion to screener/xbrl_fundamentals.py, which is the LIVE production
fetcher (frames API — universe-wide, but carries no filing date and can
silently reflect later amendments; confirmed empirically for AAPL FY2007:
the frames value traces to a 2010-01-25 10-K/A, not the original
2009-10-27 10-K). This module exists ONLY to backtest the SUE signal with
correct point-in-time dating — it is not used by the live pipeline.

The SUE formula itself is not redefined here: `pit_sue_asof` (a later task)
calls the unmodified `sue_from_quarterly_eps` from xbrl_fundamentals.py.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from screener.xbrl_fundamentals import _headers, _INTER_REQUEST_SLEEP

log = logging.getLogger(__name__)

_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_EPS_CONCEPT = "EarningsPerShareDiluted"
_EPS_UNIT = "USD/shares"  # companyfacts' unit key differs from frames' "USD-per-shares"


def fetch_companyfacts_eps(cik: int, cache_dir: Path) -> pd.DataFrame:
    """Fetch (or load from parquet cache) every EPS fact SEC has for `cik`.

    Returns columns [start, end, val, form, filed, accn] — one row per
    filed fact instance, newest and oldest, original filings AND
    amendments (callers filter). Empty DataFrame if the concept/company
    has no data.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{cik:010d}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    import time
    url = _COMPANYFACTS_URL.format(cik=cik)
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)
        time.sleep(_INTER_REQUEST_SLEEP)
        if resp.status_code == 404:
            df = pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"])
            df.to_parquet(cache_path)
            return df
        resp.raise_for_status()
        payload = resp.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        log.warning("companyfacts fetch failed (CIK %010d): %s", cik, exc)
        return pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"])

    facts = (
        payload.get("facts", {})
        .get("us-gaap", {})
        .get(_EPS_CONCEPT, {})
        .get("units", {})
        .get(_EPS_UNIT, [])
    )
    if not facts:
        df = pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"])
        df.to_parquet(cache_path)
        return df

    df = pd.DataFrame(facts)[["start", "end", "val", "form", "filed", "accn"]]
    df.to_parquet(cache_path)
    return df
