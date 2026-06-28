"""Tests for the insider (SEC Form 4) signal source — parser, qualification, cluster."""
import pytest

from bot.insider import parse_form4_xml


def _form4_xml(code="P", acq="A", shares="1000", price="150",
               ticker="AAPL", title="CEO", is_officer="1", name="Cook Tim",
               table="nonDerivative"):
    rel = ""
    if is_officer:
        rel = f"<isOfficer>{is_officer}</isOfficer>"
    if title:
        rel += f"<officerTitle>{title}</officerTitle>"
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>{rel}</reportingOwnerRelationship>
  </reportingOwner>
  <{table}Table>
    <{table}Transaction>
      <transactionDate><value>2026-06-25</value></transactionDate>
      <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{acq}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </{table}Transaction>
  </{table}Table>
</ownershipDocument>"""


def test_parse_form4_extracts_open_market_purchase():
    rows = parse_form4_xml(_form4_xml(), "0001-23-456789", "2026-06-26")
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "AAPL"
    assert r["transaction_type"] == "buy"
    assert r["amount_usd"] == pytest.approx(150_000.0)
    assert r["title"] == "CEO"
    assert r["transaction_date"] == "2026-06-25"
    assert r["disclosure_date"] == "2026-06-26"
    assert r["id"] == "0001-23-456789:0"


def test_parse_form4_ignores_sales():
    assert parse_form4_xml(_form4_xml(code="S"), "acc", "2026-06-26") == []


def test_parse_form4_ignores_disposals():
    # Code P but disposed (D) — defensively excluded (purchases are acquisitions).
    assert parse_form4_xml(_form4_xml(acq="D"), "acc", "2026-06-26") == []


def test_parse_form4_ignores_option_grants_in_derivative_table():
    # A buy recorded only in the derivative table must not be emitted.
    xml = _form4_xml(table="derivative")
    assert parse_form4_xml(xml, "acc", "2026-06-26") == []


def test_parse_form4_missing_ticker_returns_empty():
    assert parse_form4_xml(_form4_xml(ticker=""), "acc", "2026-06-26") == []


def test_parse_form4_class_share_ticker_normalized():
    rows = parse_form4_xml(_form4_xml(ticker="BRK.B"), "acc", "2026-06-26")
    assert rows and rows[0]["ticker"] == "BRK-B"


def test_parse_form4_director_title():
    xml = _form4_xml(is_officer="", title="", name="Jane Doe")
    # Inject a director relationship flag manually.
    xml = xml.replace("<reportingOwnerRelationship></reportingOwnerRelationship>",
                      "<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>")
    rows = parse_form4_xml(xml, "acc", "2026-06-26")
    assert rows and rows[0]["title"] == "Director"


def test_parse_form4_malformed_xml_returns_empty():
    assert parse_form4_xml("<not valid", "acc", "2026-06-26") == []


# ── Qualification + cluster ────────────────────────────────────────────────

def _disc(ticker="AAPL", amount=150_000.0, tx="2026-06-20", disc="2026-06-22",
          ttype="buy", title="CEO", name="Cook Tim"):
    return {
        "id": f"{ticker}:{name}", "insider_name": name, "ticker": ticker,
        "title": title, "transaction_type": ttype, "transaction_date": tx,
        "disclosure_date": disc, "amount_usd": amount, "scraped_at": "2026-06-22T00:00:00",
    }


def test_is_qualified_insider_accepts_large_recent_buy(mocker):
    mocker.patch("bot.insider_signal.is_in_universe", return_value=True)
    from bot.insider_signal import is_qualified_insider
    assert is_qualified_insider(_disc()) is True


def test_is_qualified_insider_rejects_small_buy(mocker):
    mocker.patch("bot.insider_signal.is_in_universe", return_value=True)
    from bot.insider_signal import is_qualified_insider
    assert is_qualified_insider(_disc(amount=1_000.0)) is False


def test_is_qualified_insider_rejects_sale(mocker):
    mocker.patch("bot.insider_signal.is_in_universe", return_value=True)
    from bot.insider_signal import is_qualified_insider
    assert is_qualified_insider(_disc(ttype="sell")) is False


def test_is_qualified_insider_rejects_stale_filing(mocker):
    mocker.patch("bot.insider_signal.is_in_universe", return_value=True)
    from bot.insider_signal import is_qualified_insider
    # 100-day lag exceeds max_lag_days
    assert is_qualified_insider(_disc(tx="2026-01-01", disc="2026-04-15")) is False


def test_is_qualified_insider_rejects_off_universe(mocker):
    mocker.patch("bot.insider_signal.is_in_universe", return_value=False)
    from bot.insider_signal import is_qualified_insider
    assert is_qualified_insider(_disc()) is False


def test_is_c_suite_detection():
    from bot.insider_signal import is_c_suite
    assert is_c_suite("Chief Executive Officer")
    assert is_c_suite("CFO")
    assert not is_c_suite("VP of Sales")


def test_cluster_count_counts_distinct_insiders(db):
    db.insert_insider_disclosures([
        {"id": "a", "insider_name": "Alice", "ticker": "AAPL", "title": "CEO",
         "transaction_date": "2026-06-20", "disclosure_date": "2026-06-22",
         "transaction_type": "buy", "amount_usd": 100_000.0, "scraped_at": "x"},
        {"id": "b", "insider_name": "Bob", "ticker": "AAPL", "title": "Director",
         "transaction_date": "2026-06-21", "disclosure_date": "2026-06-23",
         "transaction_type": "buy", "amount_usd": 80_000.0, "scraped_at": "x"},
        {"id": "c", "insider_name": "Alice", "ticker": "AAPL", "title": "CEO",
         "transaction_date": "2026-06-22", "disclosure_date": "2026-06-24",
         "transaction_type": "buy", "amount_usd": 50_000.0, "scraped_at": "x"},
    ])
    from bot.insider_signal import get_insider_cluster_count
    assert get_insider_cluster_count("AAPL", "2026-06-01") == 2  # Alice + Bob, deduped


def test_filter_ranks_c_suite_and_clusters_first(db, mocker):
    mocker.patch("bot.insider_signal.is_in_universe", return_value=True)
    from bot.insider_signal import filter_insider_disclosures
    discs = [
        _disc(ticker="LOW", amount=60_000.0, title="VP", name="V"),
        _disc(ticker="TOP", amount=300_000.0, title="CEO", name="C"),
    ]
    ranked = filter_insider_disclosures(discs)
    assert [d["ticker"] for d in ranked][0] == "TOP"
