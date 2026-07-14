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

    df = pd.DataFrame(facts).reindex(columns=["start", "end", "val", "form", "filed", "accn"])
    df.to_parquet(cache_path)
    return df


_MIN_QUARTER_DAYS = 80
_MAX_QUARTER_DAYS = 100


def original_quarterly_eps(facts: pd.DataFrame) -> pd.DataFrame:
    """Reduce raw companyfacts EPS facts to one row per single fiscal quarter:
    the value+date as ORIGINALLY reported (earliest-filed, non-`/A` form).

    Calendar-quarter label assignment mirrors SEC frames' own "CYyyyyQq"
    convention closely enough for backtest purposes: single-quarter duration
    (80-100 days) facts are bucketed by their `end` month. Not used to
    reproduce the frames VALUE (companyfacts is the sole source of truth
    here) — only to align with `_completed_quarters`'s calendar-quarter walk.

    Returns columns [cy_year, cy_quarter, val, filed], one row per quarter,
    sorted by filed date. Periods with no non-amendment filing are dropped —
    never fall back to an amendment.
    """
    if facts.empty:
        return pd.DataFrame(columns=["cy_year", "cy_quarter", "val", "filed"])

    df = facts.copy()
    df["start"] = pd.to_datetime(df["start"])
    df["end"] = pd.to_datetime(df["end"])
    # Kept separate from the original `filed` column (below) so the output
    # preserves the raw filed value/format rather than a stringified Timestamp.
    df["_filed_dt"] = pd.to_datetime(df["filed"])
    duration_days = (df["end"] - df["start"]).dt.days
    df = df[(duration_days >= _MIN_QUARTER_DAYS) & (duration_days <= _MAX_QUARTER_DAYS)]
    df = df[~df["form"].str.contains("/A", na=False)]
    if df.empty:
        return pd.DataFrame(columns=["cy_year", "cy_quarter", "val", "filed"])

    df = df.sort_values("_filed_dt")
    earliest = df.groupby(["start", "end"], as_index=False).first()

    end_month = earliest["end"].dt.month
    earliest["cy_year"] = earliest["end"].dt.year
    earliest["cy_quarter"] = ((end_month - 1) // 3) + 1

    result = earliest.sort_values("_filed_dt")[["cy_year", "cy_quarter", "val", "filed"]]
    return result.reset_index(drop=True)


from screener.xbrl_fundamentals import _completed_quarters, sue_from_quarterly_eps


def _parse_filed(raw) -> date | None:
    """Parse a raw `filed` value; fail soft (None, warn) rather than crash the
    whole backtest on one malformed row (NaN/None/malformed string)."""
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError):
        log.warning("Unparseable filed date %r — excluding this quarter from PIT lookup", raw)
        return None


def pit_eps_asof(quarterly: pd.DataFrame, as_of: date, n_quarters: int) -> list[float | None]:
    """Build the `eps_newest_first` list `sue_from_quarterly_eps` expects, as
    it would have looked to someone standing on `as_of` — mirrors
    `_completed_quarters(as_of, n_quarters)`'s calendar-quarter walk exactly,
    but a quarter's value is only visible if its true `filed` date <= as_of
    (not merely calendar-completed, which is what `_completed_quarters`
    alone assumes for the LIVE frames-sourced path).

    `quarterly["filed"]` is a raw ISO date STRING (e.g. "2022-05-01"), not a
    Timestamp — `original_quarterly_eps` deliberately keeps the original
    string. Parse explicitly with `date.fromisoformat`; do not call `.date()`
    on it directly (str has no such method) and do not assume `.dt`
    accessors work on this column.
    """
    quarters = _completed_quarters(as_of, n_quarters)
    lookup = {}
    for r in quarterly.itertuples():
        filed = _parse_filed(r.filed)
        if filed is not None and filed <= as_of:
            lookup[(int(r.cy_year), int(r.cy_quarter))] = r
    return [
        float(lookup[(y, q)].val) if (y, q) in lookup else None
        for y, q in quarters
    ]


def pit_sue_asof(quarterly: pd.DataFrame, as_of: date) -> float | None:
    """PIT-correct SUE as of `as_of`, using the unmodified production formula."""
    from screener.xbrl_fundamentals import _EPS_QUARTERS
    series = pit_eps_asof(quarterly, as_of, n_quarters=_EPS_QUARTERS)
    return sue_from_quarterly_eps(series)
