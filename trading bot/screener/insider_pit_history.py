"""Point-in-time SEC EDGAR Form 4 daily-index history — a historical,
date-range-capable sibling of `bot/insider.py`'s live "last few days"
walker. Companion to `bot/insider.py`, which is the LIVE production
scraper (walks back from today only, no CIK exposed in its index parse) —
this module exists ONLY to backtest the insider signal against a real
historical PIT sample; it does not touch `bot/insider.py` and is not used
by the live pipeline.

Reuses `bot/insider.py`'s pure per-filing XML parser (`parse_form4_xml`)
and per-filing fetcher (`_fetch_form4_xml`) unmodified via import — those
need no historical-specific changes. Only the daily-index WALK is new
here: the live version only supports "today minus a small lookback" and
its own `parse_form_idx` doesn't return CIK, which this module needs to
pre-filter candidates to the S&P 500 PIT universe *before* spending a
request on any individual filing's XML — that pre-filter is what keeps a
multi-year historical pull tractable (SEC's daily index lists every
filer, not just ours).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from bot.insider import _fetch_form4_xml, _headers, _INTER_REQUEST_SLEEP, parse_form4_xml  # noqa: F401 (re-exported for callers)

log = logging.getLogger(__name__)

_DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{yyyymmdd}.idx"
_IDX_FILE_RE = re.compile(r"edgar/data/(\d+)/([\d\-]+)\.txt")
_INDEX_COLUMNS = ["cik", "accession", "href", "filing_date"]


def _daily_index_cache_path(cache_dir: Path, d: date) -> Path:
    return Path(cache_dir) / f"{d.isoformat()}.parquet"


def _parse_form_idx_with_cik(text: str, filing_date: str) -> pd.DataFrame:
    """Parse one day's form.idx into Form 4 rows, including CIK — the one
    field `bot/insider.py::parse_form_idx` doesn't expose (it only needs
    the href it builds from CIK; this module also needs the CIK itself for
    the universe pre-filter). A close mirror of that function's ~10-line
    body, not an import, since the return shape genuinely differs."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "4":
            continue
        m = _IDX_FILE_RE.search(parts[-1])
        if not m:
            continue
        cik, accession = m.group(1), m.group(2)
        rows.append({
            "cik": int(cik),
            "accession": accession,
            "href": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}",
            "filing_date": filing_date,
        })
    return pd.DataFrame(rows, columns=_INDEX_COLUMNS)


def fetch_form4_index_for_date(d: date, cache_dir: Path) -> pd.DataFrame:
    """Fetch (or load from permanent per-day cache) one day's EDGAR daily
    index, filtered to Form 4 rows. A confirmed-not-published day
    (weekend/holiday) caches an empty frame — permanent, since a holiday
    never later publishes an index. Live-verified against real SEC
    responses this session: missing daily-index files come back as HTTP
    403 with an S3 ``AccessDenied`` body (SEC's Archives are S3-backed
    with public ListBucket disabled, so a missing object 403s rather than
    404s), not the HTTP 404 bot/insider.py's live "last few days" walker
    assumes — confirmed via a direct check on 3 known dates (2 weekend
    403s with the AccessDenied body, 1 business-day 200). Any other 403
    (a real access-denial/rate-limit response, distinguishable by lacking
    that body) is treated as a transient failure, matching a plain
    RequestException. A transient failure is NOT cached (mirrors the
    Tiingo-429-cached-as-a-permanent-miss bug found and fixed in Phase 0 —
    see `market_data/pit_prices.py`'s `TiingoRateLimited` handling) so a
    later run can still find real data."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _daily_index_cache_path(cache_dir, d)
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    time.sleep(_INTER_REQUEST_SLEEP)  # only real network fetches pace themselves
    url = _DAILY_INDEX_URL.format(
        year=d.year, quarter=(d.month - 1) // 3 + 1, yyyymmdd=d.strftime("%Y%m%d"),
    )
    empty = pd.DataFrame(columns=_INDEX_COLUMNS)
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)
        not_published = resp.status_code == 404 or (
            resp.status_code == 403 and "AccessDenied" in resp.text
        )
        if not_published:
            empty.to_parquet(cache_path)
            return empty
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("EDGAR daily index fetch failed (%s): %s", url, exc)
        return empty  # NOT cached — a later run can retry

    df = _parse_form_idx_with_cik(resp.text, d.isoformat())
    df.to_parquet(cache_path)
    return df


def walk_daily_indexes(start: date, end: date, cache_dir: Path,
                        cik_filter: set[int] | None = None) -> pd.DataFrame:
    """Walk every calendar day in [start, end], fetching (or loading
    cached) each day's Form 4 index entries, optionally restricted to
    cik_filter (the S&P 500 PIT universe's CIKs) before any per-filing XML
    is ever fetched — this pre-filter is what keeps a full historical pull
    tractable."""
    frames = []
    d = start
    while d <= end:
        day_df = fetch_form4_index_for_date(d, cache_dir)
        if not day_df.empty and cik_filter is not None:
            day_df = day_df[day_df["cik"].isin(cik_filter)]
        if not day_df.empty:
            frames.append(day_df)
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=_INDEX_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def pilot_request_volume(start: date, end: date, cache_dir: Path,
                          cik_filter: set[int]) -> dict:
    """Measure real request volume for a representative window before
    committing to the full historical pull (the plan's Step 4b decision
    checkpoint). Counts index-file fetches and CIK-in-universe candidate
    filings found; deliberately does NOT fetch any individual Form 4 XML
    (the expensive per-filing cost — 2 requests each, directory listing +
    document — that the full pull would incur) so the pilot itself stays
    cheap."""
    candidates = walk_daily_indexes(start, end, cache_dir, cik_filter=cik_filter)
    window_days = (end - start).days + 1
    return {
        "window_days": window_days,
        "index_requests": window_days,
        "candidate_filings_in_universe": len(candidates),
        "candidates": candidates,
    }


_TX_COLUMNS = ["id", "insider_name", "ticker", "title", "transaction_type",
               "transaction_date", "disclosure_date", "amount_usd", "scraped_at"]


def _transactions_cache_path(cache_dir: Path, d: date) -> Path:
    return Path(cache_dir) / f"{d.isoformat()}.parquet"


def fetch_form4_transactions_for_date(
    d: date, index_cache_dir: Path, transactions_cache_dir: Path,
    cik_filter: set[int] | None = None,
) -> pd.DataFrame:
    """Fetch (or load from permanent per-day cache) every open-market
    purchase transaction from that day's Form 4 filings, restricted to
    cik_filter. Resumable at day granularity — a 7+ hour historical pull
    needs to survive interruption without re-fetching everything; if
    interrupted mid-day, only that one day's remaining candidates (at
    most ~a few hundred) are re-fetched on the next run, not the whole
    history. Reuses bot/insider.py's _fetch_form4_xml/parse_form4_xml
    unmodified — one inter-request sleep per FILING (not per sub-request
    inside a filing), mirroring run_insider_scraper()'s own pacing."""
    transactions_cache_dir = Path(transactions_cache_dir)
    transactions_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _transactions_cache_path(transactions_cache_dir, d)
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    day_index = fetch_form4_index_for_date(d, index_cache_dir)
    if cik_filter is not None and not day_index.empty:
        day_index = day_index[day_index["cik"].isin(cik_filter)]
    # A single filing can be indexed once per associated CIK (issuer +
    # each reporting owner) — dedup by accession so it's never fetched
    # twice, same rationale as run_insider_scraper()'s own dedup.
    day_index = day_index.drop_duplicates(subset="accession")

    rows: list[dict] = []
    for _, cand in day_index.iterrows():
        xml_text = _fetch_form4_xml(cand["accession"], cand["href"])
        time.sleep(_INTER_REQUEST_SLEEP)
        if not xml_text:
            continue
        rows.extend(parse_form4_xml(xml_text, cand["accession"], cand["filing_date"]))

    result = pd.DataFrame(rows, columns=_TX_COLUMNS)
    result.to_parquet(cache_path)
    return result


def fetch_form4_transactions(start: date, end: date, index_cache_dir: Path,
                              transactions_cache_dir: Path,
                              cik_filter: set[int] | None = None) -> pd.DataFrame:
    """Walk every calendar day in [start, end], fetching (or loading
    cached) each day's open-market purchase transactions. This is the
    Step 4c full historical pull — expensive (one request pair per
    candidate filing) but fully resumable via the per-day cache, so an
    interrupted run can simply be re-invoked."""
    frames = []
    d = start
    n_days = (end - start).days + 1
    i = 0
    while d <= end:
        i += 1
        day_df = fetch_form4_transactions_for_date(
            d, index_cache_dir, transactions_cache_dir, cik_filter=cik_filter,
        )
        if not day_df.empty:
            frames.append(day_df)
        if i % 50 == 0 or i == n_days:
            log.info("Insider PIT history: %d/%d days processed (%s)", i, n_days, d.isoformat())
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=_TX_COLUMNS)
    return pd.concat(frames, ignore_index=True)


_INDEX_CACHE_DIR = Path("pit_cache/insider_index")
_TRANSACTIONS_CACHE_DIR = Path("pit_cache/insider_transactions")
_TRANSACTIONS_OUTPUT = Path("pit_cache/insider_transactions_full.parquet")


def run_full_fetch() -> pd.DataFrame:
    """The Step 4c full historical pull — same sample window as Phase 0
    (backtesting/backtest_factor_pit.py's SAMPLE_START/SAMPLE_END) so the
    resulting insider PIT backtest is directly comparable. Pilot-measured
    at ~107k candidate filings / ~215k requests / ~7.2h at the compliant
    pacing this session; fully resumable via the per-day cache if
    interrupted. Writes the combined result to a single parquet file so
    the Step 4d backtest driver doesn't need to know about the per-day
    cache layout."""
    from backtesting.backtest_factor_pit import SAMPLE_END, SAMPLE_START, universe_tickers
    from screener.xbrl_fundamentals import _fetch_ticker_cik_map

    tickers = universe_tickers()
    cik_map = _fetch_ticker_cik_map(cache=None)
    cik_filter = {cik_map[t] for t in tickers if t in cik_map}
    unresolved = sorted(t for t in tickers if t not in cik_map)
    log.info("Insider PIT history: %d/%d universe tickers resolved to a CIK (%d unresolved)",
              len(cik_filter), len(tickers), len(unresolved))
    if unresolved:
        log.info("Unresolved tickers (no CIK match, excluded): %s", unresolved)

    result = fetch_form4_transactions(
        SAMPLE_START, SAMPLE_END, _INDEX_CACHE_DIR, _TRANSACTIONS_CACHE_DIR,
        cik_filter=cik_filter,
    )
    _TRANSACTIONS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(_TRANSACTIONS_OUTPUT)
    log.info("Insider PIT history: %d open-market purchase transactions written to %s",
              len(result), _TRANSACTIONS_OUTPUT)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_full_fetch()
