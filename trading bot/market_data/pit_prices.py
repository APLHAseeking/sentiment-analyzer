"""Fetch and cache historical daily prices for the full PIT constituent
universe, including delisted names.

yfinance first (free, already a dependency, no per-request budget) — falls
back to Tiingo only for tickers yfinance has no data for. Tiingo's own
delisted-ticker coverage is confirmed PARTIAL this session (2 of 3 tested
2023-collapse tickers had data, one didn't) — a ticker found in neither
source is recorded as an explicit gap, never silently dropped, matching
screener/xbrl_pit_sue.py's "fail soft with a typed empty result + warning"
convention.

Per-ticker permanent parquet cache (no TTL — a delisted name's historical
prices don't change, and an active name's PIT window is a fixed historical
range too), mirroring screener/xbrl_pit_sue.py's per-CIK cache pattern. A
"miss" (no data anywhere) is cached too, as an empty frame, so re-running
doesn't repeatedly retry a ticker already confirmed unavailable.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from market_data.yf_session import make_shared_yf_session

log = logging.getLogger(__name__)

_TIINGO_TIMEOUT_SECONDS = 15
_TIINGO_INTER_REQUEST_SLEEP = 2.0  # generous under Tiingo's 50/hr free-tier cap


def _fetch_yfinance(ticker: str, start: str, end: str, session) -> pd.Series | None:
    try:
        hist = yf.Ticker(ticker, session=session).history(
            start=start, end=end, auto_adjust=True,
        )
    except Exception as exc:
        log.warning("yfinance fetch failed for %s: %s", ticker, exc)
        return None
    if hist.empty or "Close" not in hist.columns:
        return None
    series = hist["Close"]
    series.index = pd.to_datetime(series.index).tz_localize(None)
    return series


def _fetch_tiingo(ticker: str, start: str, end: str) -> pd.Series | None:
    from system.config import settings
    key = settings.credentials.tiingo_api_key
    if not key:
        raise RuntimeError("Missing required env var: TIINGO_API_KEY")
    url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
    headers = {"Authorization": "Token " + key, "Content-Type": "application/json"}
    try:
        resp = requests.get(
            url, headers=headers, params={"startDate": start, "endDate": end},
            timeout=_TIINGO_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("Tiingo fetch failed for %s: %s", ticker, exc)
        return None
    finally:
        time.sleep(_TIINGO_INTER_REQUEST_SLEEP)
    if resp.status_code != 200:
        log.info("Tiingo has no data for %s (status %d) — recording as a gap",
                  ticker, resp.status_code)
        return None
    data = resp.json()
    if not data:
        return None
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return pd.Series(df["adjClose"].to_numpy(), index=df["date"], name=ticker)


def fetch_ticker_prices(
    ticker: str, start: str, end: str, cache_dir: Path, session=None,
) -> pd.Series | None:
    """Fetch (or load from permanent cache) one ticker's daily close-price
    series. Returns None if no data was found in either source — the caller
    must track this as an explicit gap, not silently drop the ticker.
    """
    cache_path = cache_dir / f"{ticker}.parquet"
    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        if cached.empty:
            return None  # a cached "no data anywhere" miss
        return cached["close"]

    series = _fetch_yfinance(ticker, start, end, session)
    if series is None:
        series = _fetch_tiingo(ticker, start, end)

    cache_dir.mkdir(parents=True, exist_ok=True)
    if series is None:
        pd.DataFrame({"close": []}).to_parquet(cache_path)
        return None
    series.rename("close").to_frame().to_parquet(cache_path)
    return series


def fetch_pit_prices(
    tickers: list[str], start: str, end: str, cache_dir: Path,
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch daily close prices for every ticker.

    Returns (wide-format DataFrame — date index, one column per ticker,
    matching CSVPITProvider's prices.csv schema; list of tickers found in
    neither source). One shared yfinance session for the whole batch,
    explicitly closed when done (screener/factor_scorer.py's
    _fetch_all_infos precedent) — the per-Ticker() default leaks a socket
    per call across hundreds of tickers.
    """
    session = make_shared_yf_session()
    series_by_ticker: dict[str, pd.Series] = {}
    missing: list[str] = []
    try:
        for ticker in tickers:
            series = fetch_ticker_prices(ticker, start, end, cache_dir, session=session)
            if series is None:
                missing.append(ticker)
            else:
                series_by_ticker[ticker] = series
    finally:
        if session is not None:
            session.close()

    wide = pd.DataFrame(series_by_ticker)
    wide.index.name = "date"
    return wide, missing
