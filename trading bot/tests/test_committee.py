from unittest.mock import patch
from bot.committee import (
    COMMITTEE_SECTOR_MAP,
    get_committees_for_politician,
    sector_has_committee_overlap,
    clear_committee_cache,
    _in_memory_cache,
    _COMMITTEE_CACHE_PATH,
)

def test_map_has_entries():
    # Appropriations committees were removed (they mapped to "All", causing false positives)
    assert len(COMMITTEE_SECTOR_MAP) >= 19
    assert "Senate Appropriations" not in COMMITTEE_SECTOR_MAP
    assert "House Appropriations" not in COMMITTEE_SECTOR_MAP
    assert "Financial Services" in COMMITTEE_SECTOR_MAP["Senate Banking"]

def test_overlap_true():
    assert sector_has_committee_overlap("Financial Services", ["Senate Banking"]) is True

def test_overlap_false():
    assert sector_has_committee_overlap("Technology", ["Senate Agriculture"]) is False

def test_finance_committee_covers_financial_services():
    # Senate Finance covers tax/trade/Medicare — financial services is in scope
    assert sector_has_committee_overlap("Financial Services", ["Senate Finance"]) is True

def test_get_committees_via_yaml_map(mocker):
    """Committee lookup resolves from YAML-built map (no network call needed)."""
    clear_committee_cache()
    mock_map = {
        "john smith": ["Senate Banking, Housing, and Urban Affairs"],
    }
    mocker.patch("bot.committee._build_committee_map", return_value=mock_map)
    # Reset module-level map cache so the mock is picked up
    import bot.committee as cm
    cm._committee_map_cache = None

    committees = get_committees_for_politician("John Smith")
    assert any("Banking" in c for c in committees)


def test_get_committees_returns_empty_for_unknown(mocker):
    clear_committee_cache()
    import bot.committee as cm
    cm._committee_map_cache = None
    mocker.patch("bot.committee._build_committee_map", return_value={})
    assert get_committees_for_politician("Nobody Known") == ()


def test_intelligence_committee_covers_technology():
    assert sector_has_committee_overlap("Technology", ["Senate Intelligence"]) is True

def test_appropriations_removed_from_map():
    # Appropriations was removed: it mapped "All" which generated false positives
    # across every sector for every member. The correct behavior is no overlap.
    assert sector_has_committee_overlap("Healthcare", ["Senate Appropriations"]) is False
    assert sector_has_committee_overlap("Technology", ["House Appropriations"]) is False

# --- New tests for Bug 2 and Bug 3 fixes ---

def test_senate_finance_does_not_cover_technology():
    assert sector_has_committee_overlap("Technology", ("Senate Finance",)) is False

def test_committee_disk_cache_survives_clear(mocker, tmp_path, monkeypatch):
    """Write to disk cache, wipe in-memory dict, verify data is recovered from disk."""
    import bot.committee as cm
    # Point shelve at a temp path to avoid polluting the real cache
    monkeypatch.setattr(cm, "_COMMITTEE_CACHE_PATH", str(tmp_path / "test_cache"))
    # Clear both layers first
    cm._in_memory_cache.clear()
    cm._committee_map_cache = None

    mocker.patch("bot.committee._build_committee_map", return_value={
        "test politician": ["Senate Banking"],
    })

    # First call — hits live lookup and writes to disk
    result1 = cm.get_committees_for_politician("Test Politician")
    assert result1 == ("Senate Banking",)

    # Evict from in-memory cache only (simulate process restart)
    cm._in_memory_cache.clear()
    # Keep _committee_map_cache in place (module-level cache persists in process)

    # Second call — should recover from disk without calling _build_committee_map again
    result2 = cm.get_committees_for_politician("Test Politician")
    assert result2 == ("Senate Banking",)


# --- New test: YAML source integration (mocked) ---

def test_build_committee_map_from_yaml(mocker):
    """_build_committee_map correctly maps legislator names to committee names from YAML data."""
    import bot.committee as cm

    legislators_yaml = [
        {
            "id": {"bioguide": "S000001"},
            "name": {"official_full": "Jane Smith"},
            "terms": [{"type": "sen"}],
        }
    ]
    committees_yaml = [
        {"thomas_id": "SSBN", "name": "Senate Banking, Housing, and Urban Affairs"},
    ]
    membership_yaml = {
        "SSBN": [
            {"bioguide_id": "S000001", "rank": 1, "party": "D"},
        ]
    }

    def fake_fetch_yaml(url):
        if "legislators-current" in url:
            return legislators_yaml
        elif "committees-current" in url:
            return committees_yaml
        elif "committee-membership-current" in url:
            return membership_yaml
        return {}

    mocker.patch("bot.committee._fetch_yaml", side_effect=fake_fetch_yaml)

    result = cm._build_committee_map()
    assert "jane smith" in result
    assert "Senate Banking, Housing, and Urban Affairs" in result["jane smith"]
