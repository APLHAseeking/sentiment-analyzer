import pytest
from datetime import date, timedelta


def test_init_creates_tables(db):
    with db.get_conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"disclosures", "signals", "positions", "portfolio_log", "closed_positions"} <= tables

def test_insert_and_get_disclosure(db):
    # Relative to today, not hardcoded -- get_existing_ids() only returns
    # disclosures within its rolling 90-day window (bot/db.py), so a fixed
    # past date eventually falls outside it as real time advances.
    disclosure_date = (date.today() - timedelta(days=9)).isoformat()
    transaction_date = (date.today() - timedelta(days=18)).isoformat()
    scraped_at = date.today().isoformat() + "T08:00:00"
    disc = {
        "id": "test-001",
        "politician": "Jane Doe",
        "ticker": "AAPL",
        "transaction_date": transaction_date,
        "disclosure_date": disclosure_date,
        "transaction_type": "purchase",
        "amount_range": "$15,001 - $50,000",
        "scraped_at": scraped_at,
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

def test_insert_signal_stores_expected_return_pct(db):
    disc = {
        "id": "test-002b", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }
    db.insert_disclosures([disc])
    signal_id = db.insert_signal("test-002b", "AAPL", 7, 4.5, "Good signal", ["lag"],
                                  expected_return_pct=6.3)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT expected_return_pct FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
    assert row["expected_return_pct"] == pytest.approx(6.3)

def test_insert_signal_defaults_expected_return_pct_to_zero(db):
    disc = {
        "id": "test-002c", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-10",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }
    db.insert_disclosures([disc])
    signal_id = db.insert_signal("test-002c", "AAPL", 7, 4.5, "Good signal", ["lag"])
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT expected_return_pct FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
    assert row["expected_return_pct"] == pytest.approx(0.0)

def test_insert_fundamental_signal_stores_expected_return_pct(db):
    signal_id = db.insert_fundamental_signal(
        "MSFT", "2026-04-01", 85, 5.0, "Good factor score",
        expected_return_pct=3.1,
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT expected_return_pct FROM fundamental_signals WHERE id = ?", (signal_id,)
        ).fetchone()
    assert row["expected_return_pct"] == pytest.approx(3.1)

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

def test_get_recent_disclosures_for_ticker(db):
    disc = {
        "id": "rd-001", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-10", "disclosure_date": "2026-04-15",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }
    db.insert_disclosures([disc])
    rows = db.get_recent_disclosures_for_ticker("AAPL", "2026-04-01")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    # before the since_date: should return nothing
    rows2 = db.get_recent_disclosures_for_ticker("AAPL", "2026-04-20")
    assert len(rows2) == 0


def test_insert_position_stores_signal_source(db):
    db.insert_disclosures([{
        "id": "src-001", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    sid = db.insert_signal("src-001", "AAPL", 7, 4.0, "test", [])
    db.insert_position("AAPL", 100.0, 10.0, 4.0, "2026-04-28", sid, "test",
                       signal_source="fundamental")
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["signal_source"] == "fundamental"


def test_log_closed_position_stores_signal_source(db):
    db.log_closed_position(
        "AAPL", 100.0, 110.0, 10.0,
        "2026-04-01", "2026-04-28", "ai_exit", None,
        signal_source="fundamental",
    )
    rows = db.get_closed_positions()
    assert rows[0]["signal_source"] == "fundamental"


def test_signal_source_defaults_to_congressional(db):
    db.log_closed_position(
        "MSFT", 200.0, 220.0, 5.0,
        "2026-04-01", "2026-04-28", "ai_exit", None,
    )
    rows = db.get_closed_positions()
    assert rows[0]["signal_source"] == "congressional"


def test_get_performance_by_source_groups_by_source(db):
    db.log_closed_position("A", 100.0, 110.0, 10.0, "2026-04-01", "2026-04-28",
                           "ai_exit", None, signal_source="fundamental")
    db.log_closed_position("B", 100.0, 90.0, 10.0, "2026-04-01", "2026-04-28",
                           "ai_exit", None, signal_source="congressional")
    result = db.get_performance_by_source()
    assert "fundamental" in result
    assert "congressional" in result
    assert pytest.approx(result["fundamental"][0]) == 100.0
    assert pytest.approx(result["congressional"][0]) == -100.0


def test_log_regime_transition(db):
    db.log_regime_transition(
        date="2026-01-15",
        from_label="bear",
        to_label="bull",
        from_index=0,
        to_index=2,
        confidence=0.75,
        n_regimes=3,
    )
    rows = db.get_regime_transitions(days=365)
    assert len(rows) == 1
    assert rows[0]["from_label"] == "bear"
    assert rows[0]["to_label"] == "bull"
    assert float(rows[0]["confidence"]) == pytest.approx(0.75)
    assert int(rows[0]["n_regimes"]) == 3


def test_migration_idempotent_column_already_exists(tmp_path, monkeypatch):
    """If a migration column already exists (partial migration), _migrate_db() must not raise.

    This simulates a DB where the column was added outside the migration system
    (e.g. a manual ALTER TABLE or a schema rebuild), so the column is present but
    schema_version has no record of the migration.  The fix uses PRAGMA table_info
    instead of catching 'duplicate column' exception text.
    """
    import importlib
    import sqlite3

    monkeypatch.setenv("DB_PATH", str(tmp_path / "migrate_test.db"))
    import bot.db
    importlib.reload(bot.db)

    # Build the base schema (tables only, no migrations applied yet).
    db_path = str(tmp_path / "migrate_test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(bot.db._SCHEMA)
    conn.commit()

    # Manually add the column that migration v5 would add — simulating a partial migration
    # where the column exists in the DB but schema_version has no record of v5.
    # (v5 = stop_pct; confirmed absent from the base _SCHEMA CREATE TABLE.)
    conn.execute(
        "ALTER TABLE positions ADD COLUMN stop_pct REAL NOT NULL DEFAULT 15.0"
    )
    conn.commit()
    conn.close()

    # init_db() must not raise even though v5's column already exists.
    bot.db.init_db()


def test_get_regime_transitions_empty(db):
    rows = db.get_regime_transitions(days=90)
    assert rows == []


def test_get_regime_transitions_filters_by_days(db):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    db.log_regime_transition("2020-01-01", "bear", "bull", 0, 2, 0.8, 3)
    db.log_regime_transition(yesterday, "bull", "neutral", 2, 1, 0.7, 3)
    all_rows = db.get_regime_transitions(days=9999)
    recent_rows = db.get_regime_transitions(days=120)
    assert len(all_rows) == 2
    assert len(recent_rows) == 1
    assert recent_rows[0]["from_label"] == "bull"


def test_insert_fundamental_signal_and_retrieve(db):
    sig_id = db.insert_fundamental_signal(
        ticker="NVDA",
        signal_date="2026-03-15",
        composite_score=8,
        position_pct=4.5,
        rationale="Strong momentum and value",
        signal_source="fundamental",
    )
    assert isinstance(sig_id, int) and sig_id > 0

    rows = db.get_fundamental_signals()
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "NVDA"
    assert row["date"] == "2026-03-15"
    assert row["composite_score"] == 8
    assert pytest.approx(row["position_pct"]) == 4.5
    assert row["conviction"] == 5  # synthetic constant from query


def test_get_fundamental_signals_filters_by_date(db):
    db.insert_fundamental_signal("AAPL", "2026-01-10", 7, 3.0, "Value play")
    db.insert_fundamental_signal("MSFT", "2026-03-20", 9, 5.0, "Growth momentum")

    # Both returned when since_date is early enough
    all_rows = db.get_fundamental_signals(since_date="2026-01-01")
    assert len(all_rows) == 2

    # Only the newer one returned when filtering by since_date
    recent_rows = db.get_fundamental_signals(since_date="2026-02-01")
    assert len(recent_rows) == 1
    assert recent_rows[0]["ticker"] == "MSFT"


def test_insert_position_defaults_stop_pct_to_15(db):
    db.insert_disclosures([{
        "id": "sp-001", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    sid = db.insert_signal("sp-001", "AAPL", 7, 4.0, "test", [])
    db.insert_position("AAPL", 100.0, 10.0, 4.0, "2026-04-28", sid, "test")
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["stop_pct"] == pytest.approx(15.0)


def test_insert_position_stores_custom_stop_pct(db):
    db.insert_disclosures([{
        "id": "sp-002", "politician": "Jane", "ticker": "MSFT",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    sid = db.insert_signal("sp-002", "MSFT", 7, 4.0, "test", [])
    db.insert_position("MSFT", 200.0, 5.0, 4.0, "2026-04-28", sid, "test", stop_pct=3.5)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "MSFT")
    assert pos["stop_pct"] == pytest.approx(3.5)


def test_job_ran_today_false_when_no_run_recorded(db):
    assert db.job_ran_today("run_morning_pipeline", "2026-07-10") is False


def test_record_job_run_makes_job_ran_today_true(db):
    db.record_job_run("run_morning_pipeline", "2026-07-10")
    assert db.job_ran_today("run_morning_pipeline", "2026-07-10") is True


def test_job_ran_today_is_per_day(db):
    db.record_job_run("run_morning_pipeline", "2026-07-09")
    assert db.job_ran_today("run_morning_pipeline", "2026-07-10") is False


def test_record_job_run_is_idempotent_same_day(db):
    """Calling record_job_run twice for the same job/day (e.g. catch-up firing
    on top of a job that already ran) must not raise or duplicate the row."""
    db.record_job_run("run_morning_pipeline", "2026-07-10")
    db.record_job_run("run_morning_pipeline", "2026-07-10")
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM job_runs WHERE job_name = ? AND run_date = ?",
            ("run_morning_pipeline", "2026-07-10"),
        ).fetchall()
    assert len(rows) == 1


def test_insert_position_defaults_to_long_direction(db):
    db.insert_position("AAPL", 150.0, 10.0, 5.0, "2026-07-17", None, "test")
    rows = db.get_open_positions()
    assert rows[0]["direction"] == "long"


def test_insert_position_accepts_short_direction(db):
    db.insert_position("TSLA", 250.0, 5.0, 4.0, "2026-07-17", None, "test", direction="short")
    rows = db.get_open_positions()
    assert rows[0]["direction"] == "short"


def test_log_closed_position_long_pnl_unchanged(db):
    # Existing long formula: profit when exit > entry
    db.log_closed_position(
        ticker="AAPL", entry_price=100.0, exit_price=110.0, shares=10.0,
        entry_date="2026-07-01", exit_date="2026-07-10", exit_reason="test",
        signal_id=None,
    )
    rows = db.get_closed_positions()
    assert rows[0]["realized_pnl"] == pytest.approx(100.0)  # (110-100)*10
    assert rows[0]["direction"] == "long"


def test_log_closed_position_short_pnl_profits_on_price_drop(db):
    db.log_closed_position(
        ticker="TSLA", entry_price=250.0, exit_price=230.0, shares=5.0,
        entry_date="2026-07-01", exit_date="2026-07-10", exit_reason="test",
        signal_id=None, direction="short",
    )
    rows = db.get_closed_positions()
    assert rows[0]["realized_pnl"] == pytest.approx(100.0)  # (250-230)*5
    assert rows[0]["direction"] == "short"


def test_update_position_extreme_short_only_moves_down(db):
    db.insert_position("TSLA", 250.0, 10.0, 4.0, "2026-07-14", None, "Test", direction="short")
    db.update_position_extreme("TSLA", 230.0, "short")
    db.update_position_extreme("TSLA", 240.0, "short")  # higher — must NOT overwrite
    rows = db.get_open_positions()
    assert rows[0]["peak_price"] == pytest.approx(230.0)
