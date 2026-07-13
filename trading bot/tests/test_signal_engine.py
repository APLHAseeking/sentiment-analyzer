from unittest.mock import patch, MagicMock
from bot.signal_engine import (
    compute_lag_days, is_qualified_signal, filter_disclosures,
    parse_amount_min_usd, is_large_enough_trade, get_cluster_count,
    get_sector_for_ticker, clear_sector_cache,
    get_etf_close_history, clear_etf_cache,
)

def _disc(**kwargs):
    base = {
        "id": "x1", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_type": "buy",
        "transaction_date": "2026-04-10", "disclosure_date": "2026-04-15",
        "amount_range": "$15,001 - $50,000",
    }
    return {**base, **kwargs}

def test_compute_lag_days():
    assert compute_lag_days("2026-04-01", "2026-04-10") == 9

def test_sale_disqualifies():
    assert is_qualified_signal(_disc(transaction_type="sell")) is False

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
    # is_in_universe now returns False (not raises) when universe is empty
    with patch("bot.signal_engine.is_in_universe", return_value=False):
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
    discs = [_disc(id="a"), _disc(id="b", transaction_type="sell")]
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        result = filter_disclosures(discs)
    assert len(result) == 1
    assert result[0]["id"] == "a"

# --- Amount range parsing ---

def test_parse_amount_min_usd_small():
    assert parse_amount_min_usd("$1,001 - $15,000") == 1001

def test_parse_amount_min_usd_medium():
    assert parse_amount_min_usd("$15,001 - $50,000") == 15001

def test_parse_amount_min_usd_large():
    assert parse_amount_min_usd("$50,001 - $100,000") == 50001

def test_parse_amount_min_usd_unknown():
    assert parse_amount_min_usd("Unknown") == 0

# --- Trade size filter ---

def test_small_trade_disqualifies():
    disc = _disc(amount_range="$1,001 - $15,000")
    assert is_qualified_signal(disc) is False

def test_large_trade_qualifies():
    disc = _disc(amount_range="$50,001 - $100,000")
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        assert is_qualified_signal(disc) is True

# --- Cluster detection ---

def test_get_cluster_count_returns_int(mocker):
    mocker.patch("bot.signal_engine.db.get_recent_disclosures_for_ticker", return_value=[
        {"transaction_type": "buy", "politician": "Jane Doe"},
        {"transaction_type": "buy", "politician": "John Smith"},
    ])
    count = get_cluster_count("AAPL", since_date="2026-03-26")
    assert count == 2

def test_get_cluster_count_excludes_sales(mocker):
    mocker.patch("bot.signal_engine.db.get_recent_disclosures_for_ticker", return_value=[
        {"transaction_type": "buy", "politician": "Jane Doe"},
        {"transaction_type": "sell", "politician": "John Smith"},
    ])
    count = get_cluster_count("AAPL", since_date="2026-03-26")
    assert count == 1

# --- Sector cache test ---

def test_get_sector_is_cached():
    """get_sector_for_ticker should call yf.Ticker only once for repeated lookups."""
    clear_sector_cache()
    mock_ticker = MagicMock()
    mock_ticker.info = {"sector": "Technology"}
    with patch("bot.signal_engine.yf.Ticker", return_value=mock_ticker) as mock_yf:
        result1 = get_sector_for_ticker("AAPL")
        result2 = get_sector_for_ticker("AAPL")
    assert result1 == "Technology"
    assert result2 == "Technology"
    assert mock_yf.call_count == 1
    assert mock_yf.call_args.args == ("AAPL",)
    clear_sector_cache()  # cleanup


def test_get_cluster_count_deduplicates_same_politician(mocker):
    mocker.patch("bot.signal_engine.db.get_recent_disclosures_for_ticker", return_value=[
        {"transaction_type": "buy", "politician": "Jane Doe"},
        {"transaction_type": "buy", "politician": "Jane Doe"},  # same politician twice
    ])
    count = get_cluster_count("AAPL", since_date="2026-03-26")
    assert count == 1  # same person buying twice = 1, not 2


def test_qualified_signal_recognizes_real_scraper_buy_value():
    """bot/scraper.py produces transaction_type='buy' (Capitol Trades' real
    vocabulary), not 'purchase'. is_qualified_signal must recognize it —
    otherwise every real disclosure is disqualified at the first check."""
    disc = _disc(transaction_type="buy")
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        assert is_qualified_signal(disc) is True


def test_get_cluster_count_recognizes_real_scraper_buy_value(mocker):
    """get_cluster_count must count transaction_type='buy' rows (the scraper's
    real vocabulary), not 'purchase'."""
    mocker.patch("bot.signal_engine.db.get_recent_disclosures_for_ticker", return_value=[
        {"transaction_type": "buy", "politician": "Jane Doe"},
        {"transaction_type": "buy", "politician": "John Smith"},
    ])
    count = get_cluster_count("AAPL", since_date="2026-03-26")
    assert count == 2


import pandas as pd


def test_get_etf_close_history_is_cached():
    """get_etf_close_history should call yf.Ticker only once for repeated lookups."""
    clear_etf_cache()
    mock_ticker = MagicMock()
    mock_hist = pd.DataFrame({"Close": [400.0, 401.0, 402.0]})
    mock_ticker.history.return_value = mock_hist
    with patch("bot.signal_engine.yf.Ticker", return_value=mock_ticker) as mock_yf:
        result1 = get_etf_close_history("SPY")
        result2 = get_etf_close_history("SPY")
    assert result1 is result2
    assert mock_yf.call_count == 1
    assert mock_yf.call_args.args == ("SPY",)
    clear_etf_cache()  # cleanup
