import logging
import os
import shelve
import time

import requests

from bot.config import PROPUBLICA_API_KEY

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days — committee memberships change slowly

COMMITTEE_SECTOR_MAP: dict[str, list[str]] = {
    "Senate Banking": ["Financial Services", "Real Estate"],
    "House Financial Services": ["Financial Services", "Real Estate"],
    "Senate Commerce": ["Consumer Cyclical", "Communication Services", "Technology"],
    "House Energy and Commerce": ["Energy", "Utilities", "Healthcare"],
    "Senate Armed Services": ["Industrials"],
    "House Armed Services": ["Industrials"],
    "Senate Agriculture": ["Consumer Defensive", "Basic Materials"],
    "House Agriculture": ["Consumer Defensive", "Basic Materials"],
    "Senate Finance": ["Financial Services", "Healthcare", "Consumer Defensive", "Consumer Cyclical"],
    "Senate HELP": ["Healthcare"],
    "Senate Environment": ["Utilities", "Energy", "Basic Materials"],
    "House Ways and Means": ["Financial Services", "Healthcare", "Consumer Defensive", "Consumer Cyclical"],
    "House Science": ["Technology"],
    "Senate Commerce Science": ["Technology", "Communication Services"],
    "Senate Intelligence": ["Technology", "Communication Services", "Industrials"],
    "House Intelligence": ["Technology", "Communication Services", "Industrials"],
    "Senate Appropriations": ["All"],
    "House Appropriations": ["All"],
    "Senate Foreign Relations": ["Energy", "Basic Materials", "Industrials"],
    "House Foreign Affairs": ["Energy", "Basic Materials", "Industrials"],
    "Senate Judiciary": ["Technology", "Communication Services"],
    "House Judiciary": ["Technology", "Communication Services"],
}

_PROPUBLICA_BASE = "https://api.propublica.org/congress/v1"
_HEADERS = {"X-API-Key": PROPUBLICA_API_KEY}

_COMMITTEE_CACHE_PATH = "propublica_committee_cache"  # shelve adds .db extension
_in_memory_cache: dict[str, tuple[str, ...]] = {}


def _search_propublica_member(name: str) -> dict:
    try:
        resp = requests.get(
            f"{_PROPUBLICA_BASE}/members/search.json",
            params={"query": name},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"ProPublica lookup failed for {name!r}") from exc


def _fetch_committees(name: str) -> tuple[str, ...]:
    data = _search_propublica_member(name)
    results = data.get("results", [])
    if not results:
        return ()
    if len(results) > 1:
        log.warning(
            "ProPublica returned %d members for %r — using first match only",
            len(results), name,
        )
    committees: list[str] = []
    for role in results[0].get("roles", []):
        for c in role.get("committees", []):
            committees.append(c.get("name", ""))
    return tuple(committees)


def get_committees_for_politician(name: str) -> tuple[str, ...]:
    now = time.time()

    # 1. In-memory fast path (no TTL — cleared on restart)
    if name in _in_memory_cache:
        return _in_memory_cache[name]

    # 2. Disk cache with TTL
    try:
        with shelve.open(_COMMITTEE_CACHE_PATH) as cache:
            if name in cache:
                entry = cache[name]
                # Support both legacy format (bare tuple) and new format (ts, tuple)
                if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], float):
                    ts, result = entry
                    if now - ts < _CACHE_TTL_SECONDS:
                        _in_memory_cache[name] = result
                        return result
                    log.debug("Cache expired for %r (age=%.0fd)", name, (now - ts) / 86400)
                else:
                    # Legacy entry — treat as valid, refresh on next TTL cycle
                    _in_memory_cache[name] = entry
                    return entry
    except Exception as exc:
        log.debug("Disk cache read failed for %r: %s", name, exc)

    # 3. Live lookup
    result = _fetch_committees(name)
    _in_memory_cache[name] = result
    try:
        with shelve.open(_COMMITTEE_CACHE_PATH) as cache:
            cache[name] = (now, result)  # store with timestamp
    except Exception as exc:
        log.debug("Disk cache write failed for %r: %s", name, exc)
    return result


def clear_committee_cache() -> None:
    _in_memory_cache.clear()
    try:
        for ext in ("", ".db", ".dir", ".bak", ".dat"):
            path = _COMMITTEE_CACHE_PATH + ext
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        pass


def _committee_covers_sector(committee_name: str, sector: str) -> bool:
    for key, sectors in COMMITTEE_SECTOR_MAP.items():
        if key.lower() in committee_name.lower():
            return "All" in sectors or sector in sectors
    return False


def sector_has_committee_overlap(sector: str, committees: tuple[str, ...] | list[str]) -> bool:
    return any(_committee_covers_sector(c, sector) for c in committees)
