import sqlite3
from contextlib import contextmanager
from datetime import datetime, UTC
import os

_SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    id TEXT PRIMARY KEY,
    politician TEXT NOT NULL,
    ticker TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    amount_range TEXT NOT NULL,
    scraped_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disclosure_id TEXT NOT NULL REFERENCES disclosures(id),
    ticker TEXT NOT NULL,
    conviction INTEGER NOT NULL,
    position_pct REAL NOT NULL,
    rationale TEXT NOT NULL,
    risk_flags TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    entry_price REAL NOT NULL,
    shares REAL NOT NULL,
    position_pct REAL NOT NULL,
    entry_date TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    rationale TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_nav REAL NOT NULL
);
"""

def _db_path() -> str:
    return os.environ.get("DB_PATH", "trading.db")

@contextmanager
def get_conn():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)

def get_existing_ids() -> set[str]:
    with get_conn() as conn:
        return {row[0] for row in conn.execute("SELECT id FROM disclosures").fetchall()}

def insert_disclosures(disclosures: list[dict]) -> None:
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO disclosures
               (id, politician, ticker, transaction_date, disclosure_date,
                transaction_type, amount_range, scraped_at)
               VALUES (:id, :politician, :ticker, :transaction_date, :disclosure_date,
                       :transaction_type, :amount_range, :scraped_at)""",
            disclosures,
        )

def insert_signal(disclosure_id: str, ticker: str, conviction: int,
                  position_pct: float, rationale: str, risk_flags: list[str]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO signals (disclosure_id, ticker, conviction, position_pct,
               rationale, risk_flags, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (disclosure_id, ticker, conviction, position_pct, rationale,
             str(risk_flags), datetime.now(UTC).isoformat()),
        )
        return cur.lastrowid

def insert_position(ticker: str, entry_price: float, shares: float,
                    position_pct: float, entry_date: str,
                    signal_id: int, rationale: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO positions
               (ticker, entry_price, shares, position_pct, entry_date, signal_id, rationale)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ticker, entry_price, shares, position_pct, entry_date, signal_id, rationale),
        )

def get_open_positions() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM positions").fetchall()

def delete_position(ticker: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))

def log_portfolio(date: str, cash: float, positions_value: float, total_nav: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_log (date, cash, positions_value, total_nav) VALUES (?, ?, ?, ?)",
            (date, cash, positions_value, total_nav),
        )
