"""Tests for market_data/pit_prices.py — PIT historical price fetch/cache
(yfinance-first, Tiingo-fallback, with explicit gap tracking)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from market_data.pit_prices import (
    _fetch_tiingo, _fetch_yfinance, fetch_pit_prices, fetch_ticker_prices,
)


def _mock_history(closes: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": list(closes.values())}, index=pd.to_datetime(list(closes.keys())))


def _mock_tiingo_response(status_code: int, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or []
    return resp


# ---------------------------------------------------------------------------
# _fetch_yfinance
# ---------------------------------------------------------------------------

def test_fetch_yfinance_returns_close_series(mocker):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_history({"2024-01-02": 100.0, "2024-01-03": 101.0})
    mocker.patch("market_data.pit_prices.yf.Ticker", return_value=mock_ticker)

    series = _fetch_yfinance("AAPL", "2024-01-01", "2024-01-05", session=None)

    assert series is not None
    assert len(series) == 2
    assert series.iloc[0] == 100.0


def test_fetch_yfinance_returns_none_on_empty_history(mocker):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    mocker.patch("market_data.pit_prices.yf.Ticker", return_value=mock_ticker)

    assert _fetch_yfinance("NOSUCHTICKER", "2024-01-01", "2024-01-05", session=None) is None


def test_fetch_yfinance_returns_none_on_exception(mocker):
    mock_ticker = MagicMock()
    mock_ticker.history.side_effect = RuntimeError("network error")
    mocker.patch("market_data.pit_prices.yf.Ticker", return_value=mock_ticker)

    assert _fetch_yfinance("AAPL", "2024-01-01", "2024-01-05", session=None) is None


# ---------------------------------------------------------------------------
# _fetch_tiingo
# ---------------------------------------------------------------------------

def test_fetch_tiingo_returns_series_on_200(mocker):
    payload = [
        {"date": "2023-01-03T00:00:00.000Z", "adjClose": 225.22},
        {"date": "2023-01-04T00:00:00.000Z", "adjClose": 230.0},
    ]
    mocker.patch("market_data.pit_prices.requests.get",
                 return_value=_mock_tiingo_response(200, payload))
    mocker.patch("market_data.pit_prices.time.sleep")  # skip the real inter-request delay

    series = _fetch_tiingo("SIVB", "2023-01-01", "2023-02-01")

    assert series is not None
    assert len(series) == 2
    assert series.iloc[0] == pytest.approx(225.22)


def test_fetch_tiingo_returns_none_on_404(mocker):
    mocker.patch("market_data.pit_prices.requests.get",
                 return_value=_mock_tiingo_response(404))
    mocker.patch("market_data.pit_prices.time.sleep")

    assert _fetch_tiingo("FRC", "2023-01-01", "2023-02-01") is None


def test_fetch_tiingo_raises_without_api_key():
    import dataclasses
    from system.config import settings as real_settings
    no_key_settings = dataclasses.replace(
        real_settings,
        credentials=dataclasses.replace(real_settings.credentials, tiingo_api_key=""),
    )
    with patch("system.config.settings", no_key_settings):
        with pytest.raises(RuntimeError, match="TIINGO_API_KEY"):
            _fetch_tiingo("AAPL", "2023-01-01", "2023-02-01")


# ---------------------------------------------------------------------------
# fetch_ticker_prices — yfinance-first, Tiingo-fallback, permanent cache
# ---------------------------------------------------------------------------

def test_fetch_ticker_prices_prefers_yfinance_over_tiingo(tmp_path, mocker):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_history({"2024-01-02": 100.0})
    mocker.patch("market_data.pit_prices.yf.Ticker", return_value=mock_ticker)
    tiingo_spy = mocker.patch("market_data.pit_prices._fetch_tiingo")

    series = fetch_ticker_prices("AAPL", "2024-01-01", "2024-01-05", tmp_path)

    assert series is not None
    tiingo_spy.assert_not_called()


def test_fetch_ticker_prices_falls_back_to_tiingo_when_yfinance_empty(tmp_path, mocker):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    mocker.patch("market_data.pit_prices.yf.Ticker", return_value=mock_ticker)
    mocker.patch(
        "market_data.pit_prices._fetch_tiingo",
        return_value=pd.Series([225.22], index=pd.to_datetime(["2023-01-03"]), name="SIVB"),
    )

    series = fetch_ticker_prices("SIVB", "2023-01-01", "2023-02-01", tmp_path)

    assert series is not None
    assert series.iloc[0] == pytest.approx(225.22)


def test_fetch_ticker_prices_records_gap_when_neither_source_has_data(tmp_path, mocker):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    mocker.patch("market_data.pit_prices.yf.Ticker", return_value=mock_ticker)
    mocker.patch("market_data.pit_prices._fetch_tiingo", return_value=None)

    result = fetch_ticker_prices("FRC", "2023-01-01", "2023-02-01", tmp_path)

    assert result is None
    assert (tmp_path / "FRC.parquet").exists()  # the miss itself is cached


def test_fetch_ticker_prices_uses_cache_without_any_network_call(tmp_path, mocker):
    cache_path = tmp_path / "AAPL.parquet"
    pd.DataFrame({"close": [100.0, 101.0]}, index=pd.to_datetime(["2024-01-02", "2024-01-03"])).to_parquet(cache_path)

    yf_spy = mocker.patch("market_data.pit_prices._fetch_yfinance")
    tiingo_spy = mocker.patch("market_data.pit_prices._fetch_tiingo")

    series = fetch_ticker_prices("AAPL", "2024-01-01", "2024-01-05", tmp_path)

    yf_spy.assert_not_called()
    tiingo_spy.assert_not_called()
    assert len(series) == 2


def test_fetch_ticker_prices_cached_miss_returns_none_without_refetching(tmp_path, mocker):
    pd.DataFrame({"close": []}).to_parquet(tmp_path / "FRC.parquet")
    yf_spy = mocker.patch("market_data.pit_prices._fetch_yfinance")

    result = fetch_ticker_prices("FRC", "2023-01-01", "2023-02-01", tmp_path)

    assert result is None
    yf_spy.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_pit_prices — batch aggregation into wide format + gap list
# ---------------------------------------------------------------------------

def test_fetch_pit_prices_builds_wide_dataframe_and_gap_list(tmp_path, mocker):
    mocker.patch("market_data.pit_prices.make_shared_yf_session", return_value=None)

    def fake_fetch_ticker_prices(ticker, start, end, cache_dir, session=None):
        if ticker == "MISSING":
            return None
        return pd.Series([100.0, 101.0], index=pd.to_datetime(["2024-01-02", "2024-01-03"]), name=ticker)

    mocker.patch("market_data.pit_prices.fetch_ticker_prices", side_effect=fake_fetch_ticker_prices)

    wide, missing = fetch_pit_prices(["AAPL", "MSFT", "MISSING"], "2024-01-01", "2024-01-05", tmp_path)

    assert list(wide.columns) == ["AAPL", "MSFT"]
    assert missing == ["MISSING"]
    assert wide.index.name == "date"
