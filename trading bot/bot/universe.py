import io
import json
import logging
import os
import re

import pandas as pd
import requests

from monitoring.logger import EventType, emit_event
from bot.scraper import _normalize_ticker

log = logging.getLogger(__name__)

_UNIVERSE: set[str] = set()
_CACHE_FILE = "universe_cache.json"


def _fetch_sp500_ishares() -> pd.DataFrame:
    """Fallback S&P 500 list via iShares IVV holdings CSV.

    Same skiprows / column-validation logic as _fetch_russell1000; only the URL
    and the output column name differ. Wikipedia is the preferred source because
    it returns standard Symbol names; this is the fallback when Wikipedia is blocked.
    """
    url = (
        "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
    )
    resp = requests.get(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}, timeout=30
    )
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), skiprows=9)
    if "Ticker" not in df.columns:
        raise ValueError(
            f"Unexpected iShares IVV CSV format. Columns found: {df.columns.tolist()}"
        )
    tickers = df[["Ticker"]].dropna()
    _VALID_TICKER = re.compile(r"^[A-Z]{1,5}$")
    valid = tickers[tickers["Ticker"].str.match(_VALID_TICKER, na=False)]
    if len(valid) < 100:
        raise ValueError(
            f"IVV holdings CSV only {len(valid)} valid tickers — format may have changed."
        )
    return valid.rename(columns={"Ticker": "Symbol"})


def _fetch_sp500() -> pd.DataFrame:
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"},
            timeout=30,
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        return tables[0][["Symbol"]]
    except Exception as wiki_exc:
        log.warning(
            "Wikipedia S&P 500 blocked (%s) — trying iShares IVV fallback", wiki_exc
        )
        return _fetch_sp500_ishares()


def _fetch_russell1000() -> pd.DataFrame:
    url = (
        "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), skiprows=9)
    if "Ticker" not in df.columns:
        raise ValueError(f"Unexpected iShares CSV format. Columns found: {df.columns.tolist()}")
    tickers = df[["Ticker"]].dropna()
    # Validate: tickers should be 1-5 uppercase letters, not numeric strings or headers
    _VALID_TICKER = re.compile(r"^[A-Z]{1,5}$")
    valid = tickers[tickers["Ticker"].str.match(_VALID_TICKER, na=False)]
    if len(valid) < 100:
        raise ValueError(
            f"Russell 1000 CSV parsed only {len(valid)} valid tickers — "
            f"skiprows value or CSV format may have changed."
        )
    return valid


def _build_universe() -> set[str]:
    sp500 = {_normalize_ticker(t) for t in _fetch_sp500()["Symbol"].str.strip().str.upper()}
    try:
        russell = {_normalize_ticker(t) for t in _fetch_russell1000()["Ticker"].str.strip().str.upper()}
    except Exception as exc:
        log.warning(
            "Russell 1000 fetch failed (%s) — falling back to S&P 500 only (%d tickers)",
            exc, len(sp500),
        )
        return sp500
    return sp500 | russell


def _save_cache(universe: set[str]) -> None:
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(sorted(universe), f)
    except Exception as exc:
        log.warning("Could not save universe cache: %s", exc)


def _load_cache() -> set[str] | None:
    try:
        if not os.path.exists(_CACHE_FILE):
            return None
        with open(_CACHE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            return None
        return set(data)
    except Exception as exc:
        log.warning("Could not load universe cache: %s", exc)
        return None


def refresh_universe() -> None:
    """Fetch the current S&P 500 + Russell 1000 universe.

    On network failure, falls back to a local cache written by the previous
    successful run. Raises RuntimeError only if the live fetch fails AND no
    cache exists — the bot cannot operate without any universe.
    """
    global _UNIVERSE
    try:
        _UNIVERSE = _build_universe()
        _save_cache(_UNIVERSE)
        log.info("Universe refreshed: %d tickers", len(_UNIVERSE))
    except Exception as exc:
        cached = _load_cache()
        if cached:
            _UNIVERSE = cached
            log.warning(
                "Universe refresh failed (%s) — using cached universe (%d tickers)",
                exc, len(_UNIVERSE),
            )
        else:
            raise RuntimeError(
                f"Universe fetch failed and no local cache found: {exc}"
            ) from exc


def is_in_universe(ticker: str) -> bool:
    if not _UNIVERSE:
        log.warning(
            "Universe is empty — call refresh_universe() first. "
            "Treating %s as not in universe.", ticker,
        )
        return False
    return ticker.strip().upper() in _UNIVERSE


def get_universe() -> set[str]:
    """Return a copy of the current universe. Empty set if not yet initialized.

    Emits a DEAD_FEED alert if the universe is empty, because an empty universe
    means the signal pipeline has no candidates to screen.
    """
    result = set(_UNIVERSE)
    if not result:
        emit_event(
            log, EventType.DEAD_FEED,
            "get_universe() returned empty set — universe not initialised or fetch failed",
            alert=True,
        )
    return result
