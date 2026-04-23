from unittest.mock import patch
from bot.signal_engine import compute_lag_days, is_qualified_signal, filter_disclosures

def _disc(**kwargs):
    base = {
        "id": "x1", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-10", "disclosure_date": "2026-04-15",
        "amount_range": "$15,001 - $50,000",
    }
    return {**base, **kwargs}

def test_compute_lag_days():
    assert compute_lag_days("2026-04-01", "2026-04-10") == 9

def test_sale_disqualifies():
    assert is_qualified_signal(_disc(transaction_type="sale")) is False

def test_lag_over_45_disqualifies():
    disc = _disc(transaction_date="2026-01-01", disclosure_date="2026-04-22")
    assert is_qualified_signal(disc) is False

def test_not_in_universe_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=False):
        assert is_qualified_signal(disc) is False

def test_no_committees_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=[]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Technology"):
        assert is_qualified_signal(disc) is False

def test_uninitialized_universe_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", side_effect=RuntimeError("Universe not initialized")):
        assert is_qualified_signal(disc) is False

def test_no_sector_overlap_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Agriculture"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Technology"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=False):
        assert is_qualified_signal(disc) is False

def test_qualified_purchase_passes():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        assert is_qualified_signal(disc) is True

def test_filter_disclosures():
    discs = [_disc(id="a"), _disc(id="b", transaction_type="sale")]
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        result = filter_disclosures(discs)
    assert len(result) == 1
    assert result[0]["id"] == "a"
