import pytest
import pandas as pd
from unittest.mock import patch
from bot.universe import is_in_universe, _build_universe, refresh_universe


def test_is_in_universe_match():
    with patch("bot.universe._UNIVERSE", {"AAPL", "MSFT"}):
        assert is_in_universe("AAPL") is True


def test_is_in_universe_no_match():
    with patch("bot.universe._UNIVERSE", {"AAPL", "MSFT"}):
        assert is_in_universe("XYZ") is False


def test_is_in_universe_case_insensitive():
    with patch("bot.universe._UNIVERSE", {"AAPL"}):
        assert is_in_universe("aapl") is True


def test_build_universe_unions_sp500_and_russell(mocker):
    sp500_df = pd.DataFrame({"Symbol": ["AAPL", "MSFT"]})
    russell_df = pd.DataFrame({"Ticker": ["AAPL", "AMZN", "GOOG"]})
    mocker.patch("bot.universe._fetch_sp500", return_value=sp500_df)
    mocker.patch("bot.universe._fetch_russell1000", return_value=russell_df)
    result = _build_universe()
    assert result == {"AAPL", "MSFT", "AMZN", "GOOG"}


def test_refresh_universe_builds_from_fetchers(mocker):
    sp500_df = pd.DataFrame({"Symbol": ["AAPL"]})
    russell_df = pd.DataFrame({"Ticker": ["GOOGL"]})
    mocker.patch("bot.universe._fetch_sp500", return_value=sp500_df)
    mocker.patch("bot.universe._fetch_russell1000", return_value=russell_df)
    import bot.universe
    refresh_universe()
    assert bot.universe._UNIVERSE == {"AAPL", "GOOGL"}


def test_is_in_universe_returns_false_when_empty(mocker):
    # Empty universe now returns False (with a warning) instead of crashing.
    # The bot continues gracefully; the signal is just skipped.
    import bot.universe
    original = bot.universe._UNIVERSE
    bot.universe._UNIVERSE = set()
    try:
        assert is_in_universe("AAPL") is False
    finally:
        bot.universe._UNIVERSE = original


def test_is_in_universe_strips_whitespace():
    with patch("bot.universe._UNIVERSE", {"AAPL"}):
        assert is_in_universe(" AAPL ") is True


def test_build_universe_normalizes_class_share_tickers(mocker):
    """Wikipedia's S&P 500 table uses dots for class shares (BRK.B); the
    scraper normalizes tickers to hyphens (BRK-B) via _normalize_ticker.
    _build_universe must apply the same normalization so a disclosure for
    'BRK-B' matches the universe."""
    sp500_df = pd.DataFrame({"Symbol": ["AAPL", "BRK.B"]})
    russell_df = pd.DataFrame({"Ticker": ["AAPL", "AMZN"]})
    mocker.patch("bot.universe._fetch_sp500", return_value=sp500_df)
    mocker.patch("bot.universe._fetch_russell1000", return_value=russell_df)

    result = _build_universe()

    assert "BRK-B" in result
    assert "BRK.B" not in result


def test_build_universe_normalizes_russell_only_class_share_tickers(mocker):
    """A dotted class-share ticker present ONLY in the Russell 1000 source
    (not in S&P 500) must still be normalized to its hyphenated form —
    the Russell 1000 fetch path must apply _normalize_ticker the same way
    the S&P 500 path does, or it silently zeroes the congressional signal
    for that ticker (scraper output is always hyphenated)."""
    sp500_df = pd.DataFrame({"Symbol": ["AAPL"]})
    russell_df = pd.DataFrame({"Ticker": ["AAPL", "TEST.B"]})
    mocker.patch("bot.universe._fetch_sp500", return_value=sp500_df)
    mocker.patch("bot.universe._fetch_russell1000", return_value=russell_df)

    result = _build_universe()

    assert "TEST-B" in result
    assert "TEST.B" not in result
