import logging
import os
import shelve
import time
from typing import Optional

import requests
import yaml

from monitoring.logger import EventType, emit_event

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
    # Appropriations committees were removed: their "All" catch-all caused every
    # member's trade to pass the sector filter for any ticker, generating false
    # positives across the entire universe. They have no sector-specific informational edge.
    "Senate Foreign Relations": ["Energy", "Basic Materials", "Industrials"],
    "House Foreign Affairs": ["Energy", "Basic Materials", "Industrials"],
    "Senate Judiciary": ["Technology", "Communication Services"],
    "House Judiciary": ["Technology", "Communication Services"],
}

# unitedstates/congress-legislators YAML URLs (free, maintained, no auth required)
_LEGISLATORS_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators"
    "/main/legislators-current.yaml"
)
_COMMITTEE_MEMBERSHIP_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators"
    "/main/committee-membership-current.yaml"
)
_COMMITTEES_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators"
    "/main/committees-current.yaml"
)

_COMMITTEE_CACHE_PATH = "propublica_committee_cache"  # shelve adds .db extension
_in_memory_cache: dict[str, tuple[str, ...]] = {}

# Module-level cache for the full committee map (one download per process lifetime)
_committee_map_cache: Optional[dict[str, list[str]]] = None


def _fetch_yaml(url: str) -> object:
    """Download and parse a YAML file. Returns the parsed Python object."""
    resp = requests.get(url, headers={"User-Agent": "congress-bot/1.0; research-only"}, timeout=30)
    resp.raise_for_status()
    return yaml.safe_load(resp.text)


def _build_committee_map() -> dict[str, list[str]]:
    """Build a mapping of normalised legislator name → list of committee names.

    Downloads three YAML files from unitedstates/congress-legislators:
      - legislators-current.yaml  (bioguide_id → official_full name)
      - committee-membership-current.yaml  (thomas_id → list of {bioguide_id, ...})
      - committees-current.yaml  (thomas_id → committee name)

    Returns a dict keyed by lower-cased full name, value is a list of committee name strings.
    """
    # 1. Build bioguide → name mapping from legislators-current.yaml
    legislators_data = _fetch_yaml(_LEGISLATORS_URL)
    bioguide_to_name: dict[str, str] = {}
    for legislator in legislators_data or []:
        bio_id = (legislator.get("id") or {}).get("bioguide", "")
        official_name = (legislator.get("name") or {}).get("official_full", "")
        if bio_id and official_name:
            bioguide_to_name[bio_id] = official_name

    # 2. Build thomas_id → committee name mapping from committees-current.yaml
    committees_data = _fetch_yaml(_COMMITTEES_URL)
    thomas_to_name: dict[str, str] = {}
    for committee in committees_data or []:
        thomas_id = committee.get("thomas_id", "")
        name = committee.get("name", "")
        if thomas_id and name:
            thomas_to_name[thomas_id] = name
            # Also index sub-committees if present
            for sub in committee.get("subcommittees", []) or []:
                sub_thomas = sub.get("thomas_id", "")
                sub_name = sub.get("name", "")
                if sub_thomas and sub_name:
                    # Sub-committee thomas_id is the parent id + sub id combined
                    thomas_to_name[thomas_id + sub_thomas] = sub_name

    # 3. Build membership map from committee-membership-current.yaml
    membership_data = _fetch_yaml(_COMMITTEE_MEMBERSHIP_URL)
    # membership_data is a dict keyed by thomas_id
    name_to_committees: dict[str, list[str]] = {}
    for thomas_id, members in (membership_data or {}).items():
        committee_name = thomas_to_name.get(thomas_id, "")
        if not committee_name:
            continue
        for member in members or []:
            bio_id = member.get("bioguide_id", "")
            legislator_name = bioguide_to_name.get(bio_id, "")
            if not legislator_name:
                continue
            key = legislator_name.lower()
            name_to_committees.setdefault(key, [])
            if committee_name not in name_to_committees[key]:
                name_to_committees[key].append(committee_name)

    return name_to_committees


def _get_committee_map() -> dict[str, list[str]]:
    """Return the module-level committee map, building it if not yet cached."""
    global _committee_map_cache
    if _committee_map_cache is None:
        _committee_map_cache = _build_committee_map()
    return _committee_map_cache


def _fetch_committees_from_yaml(name: str) -> tuple[str, ...]:
    """Look up committee memberships for a politician by name using YAML data."""
    committee_map = _get_committee_map()
    # Try exact lower-case match first
    key = name.strip().lower()
    committees = committee_map.get(key, [])
    if committees:
        return tuple(committees)
    # Fallback: strict full first + last name match
    name_lower = name.strip().lower()
    name_parts = name_lower.split()
    for map_name, map_committees in committee_map.items():
        if map_name.lower() == name_lower:
            return tuple(map_committees)
        map_parts = map_name.lower().split()
        if (len(name_parts) >= 2 and len(map_parts) >= 2
                and name_parts[-1] == map_parts[-1]
                and name_parts[0] == map_parts[0]):
            return tuple(map_committees)
    return ()


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

    # 3. Live lookup via unitedstates/congress-legislators YAML — falls back to stale cache or empty
    try:
        result = _fetch_committees_from_yaml(name)
    except Exception as exc:
        # YAML download failed; try returning expired cache entry
        log.warning("Committee YAML lookup failed for %r: %s", name, exc)
        try:
            with shelve.open(_COMMITTEE_CACHE_PATH) as cache:
                if name in cache:
                    entry = cache[name]
                    stale_result: tuple[str, ...] = entry[1] if (
                        isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], float)
                    ) else entry
                    emit_event(
                        log, EventType.DEAD_FEED,
                        f"Committee YAML failed for {name!r} — using stale cache fallback",
                        alert=True,
                    )
                    _in_memory_cache[name] = stale_result
                    return stale_result
        except Exception:
            pass
        emit_event(
            log, EventType.DEAD_FEED,
            f"Committee lookup failed for {name!r} and no cache available — returning empty",
            alert=True,
        )
        result = ()

    _in_memory_cache[name] = result
    try:
        with shelve.open(_COMMITTEE_CACHE_PATH) as cache:
            cache[name] = (now, result)  # store with timestamp
    except Exception as exc:
        log.debug("Disk cache write failed for %r: %s", name, exc)
    return result


def clear_committee_cache() -> None:
    global _committee_map_cache
    _in_memory_cache.clear()
    _committee_map_cache = None
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
