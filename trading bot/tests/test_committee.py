from unittest.mock import patch
from bot.committee import (
    COMMITTEE_SECTOR_MAP,
    get_committees_for_politician,
    sector_has_committee_overlap,
)

def test_map_has_entries():
    assert len(COMMITTEE_SECTOR_MAP) >= 10
    assert "Financial Services" in COMMITTEE_SECTOR_MAP["Senate Banking"]

def test_overlap_true():
    assert sector_has_committee_overlap("Financial Services", ["Senate Banking"]) is True

def test_overlap_false():
    assert sector_has_committee_overlap("Technology", ["Senate Agriculture"]) is False

def test_finance_committee_covers_all_sectors():
    assert sector_has_committee_overlap("Technology", ["Senate Finance"]) is True

def test_get_committees_calls_propublica(mocker):
    mocker.patch("bot.committee._search_propublica_member", return_value={
        "results": [{
            "roles": [{"committees": [{"name": "Senate Banking, Housing, and Urban Affairs"}]}]
        }]
    })
    committees = get_committees_for_politician("Jane Doe")
    assert any("Banking" in c for c in committees)

def test_get_committees_returns_empty_for_unknown(mocker):
    mocker.patch("bot.committee._search_propublica_member", return_value={"results": []})
    assert get_committees_for_politician("Nobody Known") == ()
