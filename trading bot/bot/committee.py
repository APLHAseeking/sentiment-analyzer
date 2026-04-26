import requests
from functools import lru_cache
from bot.config import PROPUBLICA_API_KEY

COMMITTEE_SECTOR_MAP: dict[str, list[str]] = {
    "Senate Banking": ["Financial Services", "Real Estate"],
    "House Financial Services": ["Financial Services", "Real Estate"],
    "Senate Commerce": ["Consumer Cyclical", "Communication Services", "Technology"],
    "House Energy and Commerce": ["Energy", "Utilities", "Healthcare"],
    "Senate Armed Services": ["Industrials"],
    "House Armed Services": ["Industrials"],
    "Senate Agriculture": ["Consumer Defensive", "Basic Materials"],
    "House Agriculture": ["Consumer Defensive", "Basic Materials"],
    "Senate Finance": ["All"],
    "Senate HELP": ["Healthcare"],
    "Senate Environment": ["Utilities", "Energy", "Basic Materials"],
    "House Ways and Means": ["All"],
    "House Science": ["Technology"],
    "Senate Commerce Science": ["Technology", "Communication Services"],
    "Senate Intelligence": ["Technology", "Communication Services", "Industrials"],
    "House Intelligence": ["Technology", "Communication Services", "Industrials"],
    "Senate Appropriations": ["All"],
    "House Appropriations": ["All"],
    "Senate Foreign Relations": ["Energy", "Basic Materials", "Industrials"],
    "Senate Judiciary": ["Technology", "Communication Services"],
    "House Judiciary": ["Technology", "Communication Services"],
}

_PROPUBLICA_BASE = "https://api.propublica.org/congress/v1"
_HEADERS = {"X-API-Key": PROPUBLICA_API_KEY}

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

@lru_cache(maxsize=512)
def get_committees_for_politician(name: str) -> tuple[str, ...]:
    data = _search_propublica_member(name)
    results = data.get("results", [])
    if not results:
        return ()
    if len(results) > 1:
        import logging
        logging.getLogger(__name__).warning(
            "ProPublica returned %d members for %r — using first match only",
            len(results), name,
        )
    committees: list[str] = []
    for role in results[0].get("roles", []):
        for c in role.get("committees", []):
            committees.append(c.get("name", ""))
    return tuple(committees)

def _committee_covers_sector(committee_name: str, sector: str) -> bool:
    for key, sectors in COMMITTEE_SECTOR_MAP.items():
        if key.lower() in committee_name.lower():
            return "All" in sectors or sector in sectors
    return False

def sector_has_committee_overlap(sector: str, committees: tuple[str, ...] | list[str]) -> bool:
    return any(_committee_covers_sector(c, sector) for c in committees)
