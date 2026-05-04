from unittest.mock import MagicMock
import pytest
from bot.researcher import ResearchReport, format_research_for_prompt, gather_research


def _make_mock_ticker(mocker, info_overrides=None):
    import pandas as pd
    info = {
        "shortName": "Apple Inc.",
        "sector": "Technology",
        "marketCap": 3_000_000_000_000,
        "trailingPE": 28.5,
        "forwardPE": 24.0,
        "priceToBook": 45.0,
        "priceToSalesTrailing12Months": 8.5,
        "pegRatio": 2.1,
        "enterpriseToEbitda": 22.0,
        "returnOnEquity": 1.45,
        "returnOnAssets": 0.28,
        "profitMargins": 0.25,
        "debtToEquity": 150.0,
        "currentRatio": 0.99,
        "freeCashflow": 90_000_000_000,
        "revenueGrowth": 0.08,
        "earningsGrowth": 0.12,
        "beta": 1.2,
        "fiftyTwoWeekHigh": 200.0,
        "fiftyTwoWeekLow": 120.0,
        "recommendationKey": "buy",
        "targetMeanPrice": 210.0,
        "shortPercentOfFloat": 0.008,
        "averageVolume": 55_000_000,
        "regularMarketPrice": 185.0,
        "numberOfAnalystOpinions": 40,
    }
    if info_overrides:
        info.update(info_overrides)

    prices = [150.0 + i * 0.5 for i in range(63)]
    hist_mock = MagicMock()
    hist_mock.empty = False
    hist_mock.__len__ = lambda self: 63
    close_series = MagicMock()
    close_series.iloc = MagicMock()
    close_series.iloc.__getitem__ = lambda self, i: prices[i]
    hist_mock.__getitem__ = lambda self, key: close_series

    mock_ticker = MagicMock()
    mock_ticker.info = info
    mock_ticker.history.return_value = hist_mock
    mock_ticker.news = [{"content": {"title": "Test headline", "summary": "Test summary"}}]

    mocker.patch("bot.researcher.yf.Ticker", return_value=mock_ticker)
    mocker.patch("bot.researcher._try_fincept", return_value=None)
    return mock_ticker


def test_gather_research_returns_report(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("AAPL")
    assert isinstance(report, ResearchReport)
    assert report.ticker == "AAPL"


def test_gather_research_includes_short_interest(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("AAPL")
    assert report.short_interest_pct is not None
    assert abs(report.short_interest_pct - 0.8) < 0.01  # 0.008 * 100


def test_gather_research_includes_avg_daily_volume_usd(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("AAPL")
    assert report.avg_daily_volume_usd is not None
    assert abs(report.avg_daily_volume_usd - 55_000_000 * 185.0) < 1.0


def test_gather_research_includes_num_analysts(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("AAPL")
    assert report.num_analysts == 40


def test_gather_research_returns_none_on_failure(mocker):
    mocker.patch("bot.researcher.yf.Ticker", side_effect=Exception("network error"))
    report = gather_research("BADTICKER")
    assert report is None


def test_format_research_includes_short_interest(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("AAPL")
    formatted = format_research_for_prompt(report)
    assert "Short interest" in formatted
    assert "ADV" in formatted


def test_format_research_includes_analyst_count(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("AAPL")
    formatted = format_research_for_prompt(report)
    assert "40 analysts" in formatted


def test_gather_research_ticker_uppercase(mocker):
    _make_mock_ticker(mocker)
    report = gather_research("aapl")
    assert report.ticker == "AAPL"


def test_gather_research_no_short_interest_when_missing(mocker):
    _make_mock_ticker(mocker, info_overrides={"shortPercentOfFloat": None})
    report = gather_research("AAPL")
    assert report.short_interest_pct is None


def test_gather_research_no_adv_when_price_missing(mocker):
    _make_mock_ticker(mocker, info_overrides={"regularMarketPrice": 0})
    report = gather_research("AAPL")
    assert report.avg_daily_volume_usd is None


def test_rating_strong_buy_normalises_to_buy(mocker):
    _make_mock_ticker(mocker, info_overrides={"recommendationKey": "strong_buy"})
    report = gather_research("AAPL")
    assert report.analyst_rating == "Buy"


def test_rating_strong_sell_normalises_to_sell(mocker):
    _make_mock_ticker(mocker, info_overrides={"recommendationKey": "strong_sell"})
    report = gather_research("AAPL")
    assert report.analyst_rating == "Sell"


def test_rating_unknown_key_returns_none(mocker):
    _make_mock_ticker(mocker, info_overrides={"recommendationKey": "underperform"})
    report = gather_research("AAPL")
    assert report.analyst_rating is None


import json as _json


def test_score_sentiment_returns_none_tuple_on_api_failure(mocker):
    from bot.researcher import _score_sentiment
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("network error")
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    assert _score_sentiment("Some headlines") == (None, None, ())


def test_score_sentiment_parses_valid_response(mocker):
    from bot.researcher import _score_sentiment
    payload = _json.dumps({
        "sentiment": "bullish", "strength": 2, "key_themes": ["earnings beat", "guidance raised"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    label, strength, themes = _score_sentiment("headlines block")
    assert label == "bullish"
    assert strength == 2
    assert "earnings beat" in themes


def test_gather_research_populates_sentiment_fields(mocker):
    _make_mock_ticker(mocker)
    payload = _json.dumps({
        "sentiment": "bearish", "strength": 3, "key_themes": ["guidance cut"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    report = gather_research("AAPL")
    assert report.sentiment_label == "bearish"
    assert report.sentiment_strength == 3
    assert "guidance cut" in report.sentiment_themes


def test_gather_research_handles_sentiment_failure_gracefully(mocker):
    _make_mock_ticker(mocker)
    mocker.patch("bot.researcher._get_sentiment_client", side_effect=RuntimeError("no key"))
    report = gather_research("AAPL")
    assert report is not None
    assert report.sentiment_label is None
    assert report.sentiment_strength is None
    assert report.sentiment_themes == ()


def test_format_research_includes_sentiment_line_when_present(mocker):
    _make_mock_ticker(mocker)
    payload = _json.dumps({
        "sentiment": "bullish", "strength": 2, "key_themes": ["revenue growth", "margin expansion"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    report = gather_research("AAPL")
    formatted = format_research_for_prompt(report)
    assert "Sentiment" in formatted
    assert "bullish/2" in formatted
    assert "revenue growth" in formatted


def test_format_research_omits_sentiment_line_when_none(mocker):
    _make_mock_ticker(mocker)
    mocker.patch("bot.researcher._get_sentiment_client", side_effect=RuntimeError("no key"))
    report = gather_research("AAPL")
    formatted = format_research_for_prompt(report)
    assert "Sentiment" not in formatted


def test_score_sentiment_returns_none_on_invalid_label(mocker):
    from bot.researcher import _score_sentiment
    payload = _json.dumps({
        "sentiment": "positive", "strength": 2, "key_themes": ["growth"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    assert _score_sentiment("headlines") == (None, None, ())


def test_score_sentiment_returns_none_on_invalid_strength(mocker):
    from bot.researcher import _score_sentiment
    payload = _json.dumps({
        "sentiment": "bullish", "strength": 5, "key_themes": ["growth"]
    })
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.researcher._get_sentiment_client", return_value=mock_client)
    assert _score_sentiment("headlines") == (None, None, ())
