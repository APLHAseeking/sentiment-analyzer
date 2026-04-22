import pandas as pd
from unittest.mock import patch
from bot.universe import is_in_universe, _build_universe


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
