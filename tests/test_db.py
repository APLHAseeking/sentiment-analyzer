def test_init_creates_tables(db):
    with db.get_conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"disclosures", "signals", "positions", "portfolio_log"} <= tables

def test_insert_and_get_disclosure(db):
    disc = {
        "id": "test-001",
        "politician": "Jane Doe",
        "ticker": "AAPL",
        "transaction_date": "2026-04-01",
        "disclosure_date": "2026-04-10",
        "transaction_type": "purchase",
        "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }
    db.insert_disclosures([disc])
    assert "test-001" in db.get_existing_ids()

def test_insert_signal_returns_id(db):
    disc = {
        "id": "test-002", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }
    db.insert_disclosures([disc])
    signal_id = db.insert_signal("test-002", "AAPL", 7, 4.5, "Good signal", ["lag"])
    assert isinstance(signal_id, int) and signal_id > 0

def test_insert_and_delete_position(db):
    disc = {
        "id": "test-003", "politician": "Jane Doe", "ticker": "MSFT",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }
    db.insert_disclosures([disc])
    sid = db.insert_signal("test-003", "MSFT", 8, 5.0, "Test", [])
    db.insert_position("MSFT", 300.0, 16.6, 5.0, "2026-04-22", sid, "Test")
    positions = db.get_open_positions()
    assert any(p["ticker"] == "MSFT" for p in positions)
    db.delete_position("MSFT")
    assert not any(p["ticker"] == "MSFT" for p in db.get_open_positions())
