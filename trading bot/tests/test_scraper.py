import pytest
import requests
from bot.scraper import (
    _parse_trades_page,
    _parse_json_response,
    _validate_trade,
    _fetch_page,
    _fetch_page_json,
    run_scraper,
)

SAMPLE_HTML = """
<html><body><table class="q-table"><tbody>
<tr data-id="abc123">
  <td><a>Nancy Pelosi</a></td>
  <td>House</td>
  <td>NVDA</td>
  <td>Purchase</td>
  <td>2026-04-01</td>
  <td>2026-04-10</td>
  <td>$50,001 - $100,000</td>
</tr>
<tr data-id="def456">
  <td><a>John Smith</a></td>
  <td>Senate</td>
  <td>LMT</td>
  <td>Sale</td>
  <td>2026-03-15</td>
  <td>2026-04-01</td>
  <td>$15,001 - $50,000</td>
</tr>
</tbody></table></body></html>
"""

def test_parse_returns_all_rows():
    trades = _parse_trades_page(SAMPLE_HTML)
    assert len(trades) == 2

def test_parse_fields():
    trades = _parse_trades_page(SAMPLE_HTML)
    t = trades[0]
    assert t["id"] == "abc123"
    assert t["politician"] == "Nancy Pelosi"
    assert t["ticker"] == "NVDA"
    assert t["transaction_type"] == "buy"
    assert t["transaction_date"] == "2026-04-01"
    assert t["disclosure_date"] == "2026-04-10"
    assert t["amount_range"] == "$50,001 - $100,000"

def test_parse_normalizes_purchase_and_sale_to_buy_sell_vocabulary():
    """HTML fallback must translate Capitol Trades' 'Purchase'/'Sale' labels into the
    'buy'/'sell' vocabulary that bot.signal_engine gates on — not just lowercase them."""
    trades = _parse_trades_page(SAMPLE_HTML)
    assert trades[0]["transaction_type"] == "buy"
    assert trades[1]["transaction_type"] == "sell"

def test_parse_skips_rows_missing_id_or_ticker():
    html = "<html><body><table class='q-table'><tbody><tr><td></td></tr></tbody></table></body></html>"
    assert _parse_trades_page(html) == []


def test_validate_trade_passes_valid():
    trade = {
        "id": "abc123",
        "politician": "Nancy Pelosi",
        "ticker": "NVDA",
        "transaction_type": "buy",
        "transaction_date": "2026-04-01",
        "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is True


def test_validate_trade_rejects_bad_date():
    trade = {
        "id": "abc123", "politician": "Nancy Pelosi", "ticker": "NVDA",
        "transaction_type": "buy",
        "transaction_date": "April 1, 2026",
        "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_validate_trade_rejects_empty_ticker():
    trade = {
        "id": "abc123", "politician": "Nancy Pelosi", "ticker": "",
        "transaction_type": "buy",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000", "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_validate_trade_rejects_non_alpha_ticker():
    trade = {
        "id": "abc123", "politician": "Nancy Pelosi", "ticker": "123XY",
        "transaction_type": "buy",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000", "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_validate_trade_rejects_missing_id():
    trade = {
        "id": "", "politician": "Nancy Pelosi", "ticker": "NVDA",
        "transaction_type": "buy",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000", "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_fetch_page_retries_on_transient_error(mocker):
    call_count = {"n": 0}
    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise requests.exceptions.ConnectionError("timeout")
        mock_resp = mocker.MagicMock()
        mock_resp.text = "<html></html>"
        mock_resp.raise_for_status = mocker.MagicMock()
        return mock_resp
    mocker.patch("bot.scraper.requests.get", side_effect=flaky)
    mocker.patch("bot.scraper.time.sleep")
    result = _fetch_page(1)
    assert call_count["n"] == 3
    assert result == "<html></html>"


def test_fetch_page_raises_after_all_retries_exhausted(mocker):
    mocker.patch("bot.scraper.requests.get",
                 side_effect=requests.exceptions.ConnectionError("always fails"))
    mocker.patch("bot.scraper.time.sleep")
    with pytest.raises(requests.exceptions.ConnectionError):
        _fetch_page(1, max_retries=3)


def _http_error_response(status_code, retry_after=None):
    resp = requests.models.Response()
    resp.status_code = status_code
    if retry_after is not None:
        resp.headers["Retry-After"] = str(retry_after)
    return resp


def test_fetch_page_json_retries_on_429_instead_of_falling_back(mocker):
    """429 is a rate limit, not a missing/wrong endpoint — the JSON path must
    retry it like a transient error, not immediately give up and fall back to
    HTML (the original bug: any sub-500 status short-circuited to [])."""
    call_count = {"n": 0}
    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            resp = _http_error_response(429)
            raise requests.exceptions.HTTPError(response=resp)
        mock_resp = mocker.MagicMock()
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {"data": []}
        return mock_resp
    mocker.patch("bot.scraper.requests.get", side_effect=flaky)
    sleep_mock = mocker.patch("bot.scraper.time.sleep")

    result = _fetch_page_json(1)

    assert call_count["n"] == 3
    assert result == []
    assert sleep_mock.call_count == 2


def test_fetch_page_json_honours_retry_after_header(mocker):
    """A 429 with a Retry-After header must wait at least that long, not the
    (possibly shorter) default exponential-backoff delay."""
    call_count = {"n": 0}
    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            resp = _http_error_response(429, retry_after=30)
            raise requests.exceptions.HTTPError(response=resp)
        mock_resp = mocker.MagicMock()
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {"data": []}
        return mock_resp
    mocker.patch("bot.scraper.requests.get", side_effect=flaky)
    sleep_mock = mocker.patch("bot.scraper.time.sleep")

    _fetch_page_json(1)

    sleep_mock.assert_called_once_with(30.0)


def test_fetch_page_json_gives_up_immediately_on_404(mocker):
    """A genuine 404 (wrong/missing endpoint) must still fall back to HTML
    right away — only 429 gets the retry treatment."""
    mocker.patch(
        "bot.scraper.requests.get",
        side_effect=requests.exceptions.HTTPError(response=_http_error_response(404)),
    )
    sleep_mock = mocker.patch("bot.scraper.time.sleep")

    result = _fetch_page_json(1)

    assert result == []
    sleep_mock.assert_not_called()


def test_run_scraper_returns_empty_on_persistent_fetch_failure(mocker, db):
    mocker.patch("bot.scraper.requests.get",
                 side_effect=requests.exceptions.ConnectionError("always fails"))
    mocker.patch("bot.scraper.time.sleep")
    result = run_scraper(max_pages=1)
    assert result == []


def test_run_scraper_emits_dead_feed_alert_on_empty_page1(mocker, db):
    """run_scraper must fire a dead-feed alert when page 1 returns 0 trades."""
    # Return a valid HTTP response but with empty HTML (no table rows)
    mock_resp = mocker.MagicMock()
    mock_resp.text = "<html><body><table class='q-table'><tbody></tbody></table></body></html>"
    mock_resp.raise_for_status = mocker.MagicMock()
    mocker.patch("bot.scraper.requests.get", return_value=mock_resp)
    # fire_alert is imported into monitoring.logger and called from emit_event there
    mock_fire_alert = mocker.patch("monitoring.logger.fire_alert")

    result = run_scraper(max_pages=1)

    assert result == []
    mock_fire_alert.assert_called_once()
    # emit_event calls fire_alert(event=..., message=..., data=...) with keyword args
    call = mock_fire_alert.call_args
    event_value = call.kwargs.get("event") or (call.args[0] if call.args else None)
    assert event_value == "dead_feed"


# --- JSON endpoint parser tests ---

SAMPLE_JSON_RESPONSE = {
    "data": [
        {
            "id": "abc123",
            "politician": {"name": "John Smith"},
            "issuer": {"ticker": "AAPL"},
            "txDate": "2025-10-15",
            "disclDate": "2025-10-20",
            "txType": "buy",
            "amount": "$50,001 - $100,000",
        },
        {
            "id": "def456",
            "politician": {"name": "Nancy Pelosi"},
            "issuer": {"ticker": "NVDA"},
            "txDate": "2025-11-01",
            "disclDate": "2025-11-10",
            "txType": "sell",
            "amount": "$100,001 - $250,000",
        },
    ]
}


def test_parse_json_response_returns_correct_count():
    trades = _parse_json_response(SAMPLE_JSON_RESPONSE)
    assert len(trades) == 2


def test_parse_json_response_fields_match_trade_dict_shape():
    """JSON parser must produce the same dict shape as the HTML parser."""
    trades = _parse_json_response(SAMPLE_JSON_RESPONSE)
    t = trades[0]
    assert t["id"] == "abc123"
    assert t["politician"] == "John Smith"
    assert t["ticker"] == "AAPL"
    assert t["transaction_type"] == "buy"
    assert t["transaction_date"] == "2025-10-15"
    assert t["disclosure_date"] == "2025-10-20"
    assert t["amount_range"] == "$50,001 - $100,000"
    # scraped_at must be present (ISO string)
    assert "scraped_at" in t and t["scraped_at"]


def test_parse_json_response_normalizes_ticker_dots_to_hyphens():
    data = {
        "data": [{
            "id": "x1",
            "politician": {"name": "Jane Doe"},
            "issuer": {"ticker": "BRK.B"},
            "txDate": "2025-10-15",
            "disclDate": "2025-10-20",
            "txType": "buy",
            "amount": "$1,001 - $15,000",
        }]
    }
    trades = _parse_json_response(data)
    assert len(trades) == 1
    assert trades[0]["ticker"] == "BRK-B"


def test_parse_json_response_skips_invalid_items():
    """Items missing id or with bad dates must be skipped."""
    data = {
        "data": [
            # Missing id
            {
                "id": "",
                "politician": {"name": "John Smith"},
                "issuer": {"ticker": "AAPL"},
                "txDate": "2025-10-15",
                "disclDate": "2025-10-20",
                "txType": "buy",
                "amount": "$50,001 - $100,000",
            },
            # Bad transaction date
            {
                "id": "valid1",
                "politician": {"name": "John Smith"},
                "issuer": {"ticker": "MSFT"},
                "txDate": "October 15 2025",  # not ISO
                "disclDate": "2025-10-20",
                "txType": "buy",
                "amount": "$50,001 - $100,000",
            },
        ]
    }
    trades = _parse_json_response(data)
    assert trades == []


def test_parse_json_response_handles_flat_politician_string():
    """Politician field may be a plain string (not a nested dict)."""
    data = {
        "data": [{
            "id": "z99",
            "politician": "Jane Doe",
            "issuer": {"ticker": "TSLA"},
            "txDate": "2025-09-01",
            "disclDate": "2025-09-10",
            "txType": "buy",
            "amount": "$1,001 - $15,000",
        }]
    }
    trades = _parse_json_response(data)
    assert len(trades) == 1
    assert trades[0]["politician"] == "Jane Doe"


def test_parse_json_response_handles_trades_key():
    """Some responses may use 'trades' instead of 'data' as the top-level key."""
    data = {
        "trades": [{
            "id": "t01",
            "politician": {"name": "Bob Jones"},
            "issuer": {"ticker": "LMT"},
            "txDate": "2025-08-01",
            "disclDate": "2025-08-15",
            "txType": "sell",
            "amount": "$15,001 - $50,000",
        }]
    }
    trades = _parse_json_response(data)
    assert len(trades) == 1
    assert trades[0]["id"] == "t01"


def test_run_scraper_uses_json_when_available(mocker, db):
    """run_scraper must use JSON trades when the JSON endpoint returns valid data."""
    # JSON endpoint returns valid data
    json_resp = mocker.MagicMock()
    json_resp.raise_for_status = mocker.MagicMock()
    json_resp.headers = {"Content-Type": "application/json"}
    json_resp.json.return_value = {
        "data": [{
            "id": "json001",
            "politician": {"name": "Test Politician"},
            "issuer": {"ticker": "MSFT"},
            "txDate": "2026-01-10",
            "disclDate": "2026-01-15",
            "txType": "buy",
            "amount": "$50,001 - $100,000",
        }]
    }

    def fake_get(url, **kwargs):
        if "api/trades" in url:
            return json_resp
        # HTML fallback should not be called when JSON works
        raise AssertionError("HTML fallback should not be called when JSON succeeds")

    mocker.patch("bot.scraper.requests.get", side_effect=fake_get)
    result = run_scraper(max_pages=1)
    assert len(result) == 1
    assert result[0]["id"] == "json001"
    assert result[0]["ticker"] == "MSFT"
