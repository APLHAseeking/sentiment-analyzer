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

def test_risk_flags_json_serialisation(db):
    import json
    disc = {
        "id": "test-004", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }
    db.insert_disclosures([disc])
    db.insert_signal("test-004", "AAPL", 7, 4.5, "Good", ["lag", "small size"])
    with db.get_conn() as conn:
        row = conn.execute("SELECT risk_flags FROM signals WHERE disclosure_id='test-004'").fetchone()
    flags = json.loads(row["risk_flags"])
    assert flags == ["lag", "small size"]

def test_log_closed_position_creates_record(db):
    disc = {
        "id": "cl-001", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    db.insert_disclosures([disc])
    sid = db.insert_signal("cl-001", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 150.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    db.log_closed_position(
        ticker="AAPL",
        entry_price=150.0,
        exit_price=165.0,
        shares=10.0,
        entry_date="2026-04-01",
        exit_date="2026-04-26",
        exit_reason="take_profit",
        signal_id=sid,
    )
    rows = db.get_closed_positions()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert abs(rows[0]["realized_pnl"] - 150.0) < 0.01  # (165-150)*10

def test_get_portfolio_stats_win_rate(db):
    disc_a = {
        "id": "st-001", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    disc_b = {**disc_a, "id": "st-002", "ticker": "MSFT"}
    db.insert_disclosures([disc_a, disc_b])
    sid_a = db.insert_signal("st-001", "AAPL", 8, 5.0, "Good", [])
    sid_b = db.insert_signal("st-002", "MSFT", 6, 3.0, "Ok", [])
    db.log_closed_position("AAPL", 150.0, 165.0, 10.0, "2026-04-01", "2026-04-20", "take_profit", sid_a)
    db.log_closed_position("MSFT", 300.0, 285.0, 5.0, "2026-04-01", "2026-04-20", "stop_loss", sid_b)
    stats = db.get_portfolio_stats()
    assert stats["total_trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert abs(stats["win_rate"] - 0.5) < 0.01
    # Net PnL: (165-150)*10 + (285-300)*5 = 150 - 75 = 75
    assert abs(stats["total_realized_pnl"] - 75.0) < 0.01
