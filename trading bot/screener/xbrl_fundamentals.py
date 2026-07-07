"""SEC XBRL fundamentals: earnings surprise (SUE), accruals, net payout.

Data comes from the free SEC XBRL "frames" API (data.sec.gov), which returns
one concept for EVERY filer in a single request — so the whole universe costs
~20 cacheable HTTP requests per screen and zero LLM calls:

- SUE (PEAD input): seasonal-random-walk standardized unexpected earnings from
  quarterly diluted EPS — (EPS_q − EPS_{q−4}) / std(prior YoY changes). Feeds
  the momentum sleeve (earnings momentum family).
- Accruals (Sloan 1996 quality-of-earnings): (NI − CFO) / total assets from the
  latest annual frames — annual because many filers tag cash flow YTD-only,
  which the quarterly frames then omit. Lower is better (fed inverted).
- Net payout: buybacks + common dividends (annual, USD). The caller divides by
  market cap for a net payout yield in the value sleeve.

PIT note: live scoring uses the latest published frame (as-reported facts).
The Phase 0 caveat still applies to any HISTORICAL backtest of these signals;
the companyfacts API's `filed` dates are the honest route for a future PIT
backtest (see docs/EDGE_BACKLOG.md).

SEC fair-access: descriptive User-Agent with contact email and <10 req/s —
honoured here, sharing InsiderConfig.sec_user_agent with bot/insider.py.
"""
from __future__ import annotations

import logging
import shelve
import statistics
import time
from datetime import date, datetime, UTC

import requests

from system.config import settings

log = logging.getLogger(__name__)

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/{unit}/{frame}.json"
_CACHE_PATH = "xbrl_frames_cache"   # shelve adds its own extension
_CACHE_TTL_SECONDS = 20 * 3600      # frames move with filings; refresh roughly daily
_INTER_REQUEST_SLEEP = 0.12         # ~8 req/s, under SEC's 10 req/s fair-access ceiling
_EPS_QUARTERS = 14                  # quarterly EPS history to request (covers anchor shifting)
_MIN_EPS_QUARTERS = 6               # SUE needs at least this much aligned history
_MIN_SUE_CHANGE_OBS = 2             # and at least this many PRIOR YoY changes for the std
_MAX_SUE_STALENESS_QUARTERS = 2     # newest usable EPS may be at most this many quarters old


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.insider.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _completed_quarters(today: date, n: int) -> list[tuple[int, int]]:
    """Most recent ``n`` completed calendar quarters, newest first, as (year, q)."""
    y, q = today.year, (today.month - 1) // 3 + 1
    out: list[tuple[int, int]] = []
    for _ in range(n):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        out.append((y, q))
    return out


def sue_from_quarterly_eps(eps_newest_first: list[float | None]) -> float | None:
    """Seasonal-random-walk SUE from calendar-aligned quarterly diluted EPS.

    ``eps_newest_first[0]`` is the latest completed calendar quarter; missing
    quarters must be present as None so the seasonal (t−4) lag stays aligned.
    The anchor shifts to the company's newest AVAILABLE quarter (at most
    _MAX_SUE_STALENESS_QUARTERS back) — the just-completed quarter has no
    filings for ~40 days, and Q4 EPS is often reported only in FY context, so
    slot 0 is routinely empty for everyone. Returns None when the latest YoY
    change or enough prior changes are unavailable, or when the prior changes
    are flat (std 0 — a z-score is meaningless).
    """
    i0 = next((i for i, v in enumerate(eps_newest_first) if v is not None), None)
    if i0 is None or i0 > _MAX_SUE_STALENESS_QUARTERS:
        return None
    eps = eps_newest_first[i0:]
    if len(eps) < _MIN_EPS_QUARTERS or eps[4] is None:
        return None
    latest_change = eps[0] - eps[4]
    prior = [
        eps[i] - eps[i + 4]
        for i in range(1, len(eps) - 4)
        if eps[i] is not None and eps[i + 4] is not None
    ]
    if len(prior) < _MIN_SUE_CHANGE_OBS:
        return None
    sd = statistics.pstdev(prior)
    if sd <= 0:
        return None
    return latest_change / sd


def accruals_ratio(
    net_income: float | None, cfo: float | None, assets: float | None
) -> float | None:
    """Sloan (1996) total-accruals proxy: (NI − CFO) / total assets. Lower is better."""
    if net_income is None or cfo is None or assets is None or assets <= 0:
        return None
    return (net_income - cfo) / assets


def _frame_cik_map(payload: dict | None) -> dict[int, float]:
    """Map cik → val from a frames API payload ({} when payload is None/malformed)."""
    if not payload:
        return {}
    out: dict[int, float] = {}
    for row in payload.get("data", []):
        try:
            out[int(row["cik"])] = float(row["val"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_json(url: str, cache: "shelve.Shelf | None") -> dict | None:
    """GET a JSON URL with shelve TTL caching. Returns None on any failure/404."""
    now = time.time()
    if cache is not None:
        hit = cache.get(url)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)
        time.sleep(_INTER_REQUEST_SLEEP)
        if resp.status_code == 404:  # frame not published (no filers for it yet)
            return None
        resp.raise_for_status()
        payload = resp.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        log.warning("XBRL fetch failed (%s): %s", url, exc)
        return None
    if cache is not None:
        cache[url] = (now, payload)
    return payload


def _fetch_ticker_cik_map(cache: "shelve.Shelf | None") -> dict[str, int]:
    """SEC ticker → CIK map (company_tickers.json already uses hyphenated class shares)."""
    payload = _fetch_json(_TICKER_MAP_URL, cache)
    if not payload:
        return {}
    out: dict[str, int] = {}
    for row in payload.values():
        try:
            out[str(row["ticker"]).upper().strip()] = int(row["cik_str"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _fetch_frame(concept: str, unit: str, frame: str, cache) -> dict[int, float]:
    return _frame_cik_map(
        _fetch_json(_FRAMES_URL.format(concept=concept, unit=unit, frame=frame), cache)
    )


def fetch_xbrl_factors(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    """Fetch {ticker: {"sue", "accruals", "net_payout_usd"}} for the universe.

    ~20 requests total regardless of universe size (frames are universe-wide),
    cached ~20h in a shelve file so re-runs are free. Fail-soft: a missing
    concept/company yields None for that field (ranked neutral upstream);
    total failure (no ticker→CIK map) returns {}.
    """
    if not tickers:
        return {}
    today = datetime.now(UTC).date()

    try:
        cache: shelve.Shelf | None = shelve.open(_CACHE_PATH)
    except Exception as exc:
        log.warning("XBRL cache unavailable (%s) — fetching uncached", exc)
        cache = None
    try:
        cik_by_ticker = _fetch_ticker_cik_map(cache)
        if not cik_by_ticker:
            log.error("XBRL: ticker→CIK map unavailable — skipping XBRL factors this run")
            return {}

        # Quarterly diluted EPS, calendar-aligned, newest first (for SUE).
        eps_frames = [
            _fetch_frame("EarningsPerShareDiluted", "USD-per-shares", f"CY{y}Q{q}", cache)
            for y, q in _completed_quarters(today, _EPS_QUARTERS)
        ]

        # Latest published annual frames (filers report FY with a lag; fall back a year).
        ni_map: dict[int, float] = {}
        fy = None
        for y in (today.year - 1, today.year - 2):
            ni_map = _fetch_frame("NetIncomeLoss", "USD", f"CY{y}", cache)
            if ni_map:
                fy = y
                break
        cfo_map: dict[int, float] = {}
        assets_map: dict[int, float] = {}
        buyback_map: dict[int, float] = {}
        div_map: dict[int, float] = {}
        div_common_map: dict[int, float] = {}
        if fy is not None:
            cfo_map = _fetch_frame(
                "NetCashProvidedByUsedInOperatingActivities", "USD", f"CY{fy}", cache)
            assets_map = _fetch_frame("Assets", "USD", f"CY{fy}Q4I", cache)
            buyback_map = _fetch_frame(
                "PaymentsForRepurchaseOfCommonStock", "USD", f"CY{fy}", cache)
            div_map = _fetch_frame("PaymentsOfDividends", "USD", f"CY{fy}", cache)
            div_common_map = _fetch_frame(
                "PaymentsOfDividendsCommonStock", "USD", f"CY{fy}", cache)

        result: dict[str, dict[str, float | None]] = {}
        for t in tickers:
            cik = cik_by_ticker.get(t.upper())
            if cik is None:
                result[t] = {"sue": None, "accruals": None, "net_payout_usd": None}
                continue
            sue = sue_from_quarterly_eps([fm.get(cik) for fm in eps_frames])
            accr = accruals_ratio(ni_map.get(cik), cfo_map.get(cik), assets_map.get(cik))
            buyback = buyback_map.get(cik)
            dividends = div_map.get(cik)
            if dividends is None:
                dividends = div_common_map.get(cik)
            if buyback is None and dividends is None:
                payout = None  # neither tag filed — unknown, not zero
            else:
                payout = (buyback if buyback is not None else 0.0) + (
                    dividends if dividends is not None else 0.0)
            result[t] = {"sue": sue, "accruals": accr, "net_payout_usd": payout}
        n_sue = sum(1 for v in result.values() if v["sue"] is not None)
        log.info("XBRL factors: %d/%d tickers with SUE, annual FY=%s", n_sue, len(tickers), fy)
        return result
    finally:
        if cache is not None:
            cache.close()
