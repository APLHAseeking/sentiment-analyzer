from bot.scraper import _parse_trades_page

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
