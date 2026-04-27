import pytest
import requests
from bot.scraper import _parse_trades_page, _validate_trade, _fetch_page, run_scraper

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
    assert t["transaction_type"] == "purchase"
    assert t["transaction_date"] == "2026-04-01"
    assert t["disclosure_date"] == "2026-04-10"
    assert t["amount_range"] == "$50,001 - $100,000"

def test_parse_skips_rows_missing_id_or_ticker():
    html = "<html><body><table class='q-table'><tbody><tr><td></td></tr></tbody></table></body></html>"
    assert _parse_trades_page(html) == []


def test_validate_trade_passes_valid():
    trade = {
        "id": "abc123",
        "politician": "Nancy Pelosi",
        "ticker": "NVDA",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-01",
        "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is True


def test_validate_trade_rejects_bad_date():
    trade = {
        "id": "abc123", "politician": "Nancy Pelosi", "ticker": "NVDA",
        "transaction_type": "purchase",
        "transaction_date": "April 1, 2026",
        "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_validate_trade_rejects_empty_ticker():
    trade = {
        "id": "abc123", "politician": "Nancy Pelosi", "ticker": "",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000", "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_validate_trade_rejects_non_alpha_ticker():
    trade = {
        "id": "abc123", "politician": "Nancy Pelosi", "ticker": "123XY",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "amount_range": "$50,001 - $100,000", "scraped_at": "2026-04-26T08:00:00",
    }
    assert _validate_trade(trade) is False


def test_validate_trade_rejects_missing_id():
    trade = {
        "id": "", "politician": "Nancy Pelosi", "ticker": "NVDA",
        "transaction_type": "purchase",
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


def test_run_scraper_returns_empty_on_persistent_fetch_failure(mocker, db):
    mocker.patch("bot.scraper.requests.get",
                 side_effect=requests.exceptions.ConnectionError("always fails"))
    mocker.patch("bot.scraper.time.sleep")
    result = run_scraper(max_pages=1)
    assert result == []
