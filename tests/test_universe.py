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


def test_is_in_universe_raises_when_empty(mocker):
    import bot.universe
    original = bot.universe._UNIVERSE
    bot.universe._UNIVERSE = set()
    try:
        with pytest.raises(RuntimeError, match="Universe not initialized"):
            is_in_universe("AAPL")
    finally:
        bot.universe._UNIVERSE = original


def test_is_in_universe_strips_whitespace():
    with patch("bot.universe._UNIVERSE", {"AAPL"}):
        assert is_in_universe(" AAPL ") is True
