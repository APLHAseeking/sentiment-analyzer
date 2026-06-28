import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, UTC
import os
import json

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
CREATE INDEX IF NOT EXISTS idx_disclosures_ticker ON disclosures(ticker);
CREATE INDEX IF NOT EXISTS idx_disclosures_disclosure_date ON disclosures(disclosure_date);

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
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    entry_price REAL NOT NULL,
    shares REAL NOT NULL,
    position_pct REAL NOT NULL,
    entry_date TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    rationale TEXT NOT NULL,
    peak_price REAL,
    signal_source TEXT NOT NULL DEFAULT 'congressional'
);

CREATE TABLE IF NOT EXISTS closed_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    shares REAL NOT NULL,
    entry_date TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    realized_pnl REAL NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    closed_at TEXT NOT NULL,
    signal_source TEXT NOT NULL DEFAULT 'congressional'
);
CREATE INDEX IF NOT EXISTS idx_closed_positions_exit_date ON closed_positions(exit_date);

CREATE TABLE IF NOT EXISTS portfolio_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_nav REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS regime_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    regime_label TEXT NOT NULL,
    regime_index INTEGER NOT NULL,
    confidence REAL NOT NULL,
    is_stable INTEGER NOT NULL DEFAULT 1,
    n_regimes INTEGER NOT NULL,
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regime_log_date ON regime_log(date);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    description TEXT NOT NULL,
    data TEXT NOT NULL,
    logged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    train_start TEXT NOT NULL,
    train_end TEXT NOT NULL,
    test_start TEXT NOT NULL,
    test_end TEXT NOT NULL,
    n_regimes INTEGER NOT NULL,
    metrics TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regime_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    from_label TEXT NOT NULL,
    to_label TEXT NOT NULL,
    from_index INTEGER NOT NULL,
    to_index INTEGER NOT NULL,
    confidence REAL NOT NULL,
    n_regimes INTEGER NOT NULL,
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regime_transitions_date ON regime_transitions(date);

CREATE TABLE IF NOT EXISTS fundamental_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    composite_score INTEGER NOT NULL,
    position_pct REAL NOT NULL,
    rationale TEXT NOT NULL,
    signal_source TEXT NOT NULL DEFAULT 'fundamental',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fundamental_signals_date ON fundamental_signals(signal_date);
CREATE INDEX IF NOT EXISTS idx_fundamental_signals_ticker ON fundamental_signals(ticker);

CREATE TABLE IF NOT EXISTS insider_disclosures (
    id TEXT PRIMARY KEY,
    insider_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insider_disclosures_ticker ON insider_disclosures(ticker);
CREATE INDEX IF NOT EXISTS idx_insider_disclosures_disclosure_date ON insider_disclosures(disclosure_date);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

def _db_path() -> str:
    from system.paths import resolve
    return resolve(os.environ.get("DB_PATH", "trading.db"))

@contextmanager
def get_conn():
    conn = sqlite3.connect(_db_path())
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads during bot writes
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
)

_MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "Add signal_source to positions",
        "ALTER TABLE positions ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'congressional'",
    ),
    (
        2,
        "Add signal_source to closed_positions",
        "ALTER TABLE closed_positions ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'congressional'",
    ),
    (
        3,
        "Add take_profit_taken to positions",
        "ALTER TABLE positions ADD COLUMN take_profit_taken INTEGER NOT NULL DEFAULT 0",
    ),
    (
        4,
        "Add entry_commission to positions",
        "ALTER TABLE positions ADD COLUMN entry_commission REAL NOT NULL DEFAULT 0",
    ),
    (
        5,
        "Add stop_pct to positions",
        "ALTER TABLE positions ADD COLUMN stop_pct REAL NOT NULL DEFAULT 15.0",
    ),
]


def _migrate_db() -> None:
    """Apply pending schema migrations. Each migration runs exactly once."""
    with get_conn() as conn:
        applied = {
            row[0] for row in conn.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
        }
        for version, description, sql in _MIGRATIONS:
            if version in applied:
                continue
            m = _ADD_COLUMN_RE.search(sql)
            if m:
                table_name, col_name = m.group(1), m.group(2)
                existing_cols = {
                    row[1] for row in conn.execute(
                        f"PRAGMA table_info({table_name})"
                    ).fetchall()
                }
                if col_name in existing_cols:
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                        (version, f"{description} (retroactive)", datetime.now(UTC).isoformat()),
                    )
                    continue
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, datetime.now(UTC).isoformat()),
            )


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
    _migrate_db()

def get_existing_ids() -> set[str]:
    # Limit to the last 90 days — disclosures older than that are never re-submitted
    with get_conn() as conn:
        return {row[0] for row in conn.execute(
            "SELECT id FROM disclosures WHERE disclosure_date >= date('now', '-90 days')"
        ).fetchall()}

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
             json.dumps(risk_flags), datetime.now(UTC).isoformat()),
        )
        return cur.lastrowid

def insert_position(ticker: str, entry_price: float, shares: float,
                    position_pct: float, entry_date: str,
                    signal_id: int | None, rationale: str,
                    signal_source: str = "congressional",
                    entry_commission: float = 0.0,
                    stop_pct: float = 15.0) -> None:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO positions
               (ticker, entry_price, shares, position_pct, entry_date, signal_id,
                rationale, peak_price, signal_source, entry_commission, stop_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, entry_price, shares, position_pct, entry_date, signal_id,
             rationale, entry_price, signal_source, entry_commission, stop_pct),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Position already exists for {ticker} — cannot open duplicate. "
                "Close the existing position before re-entering."
            )

def position_exists(ticker: str) -> bool:
    """Return True if an open position for ticker already exists in the DB."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE ticker = ? LIMIT 1", (ticker,)
        ).fetchone()
        return row is not None

def get_open_positions() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM positions").fetchall()

def update_position_shares(ticker: str, shares: float) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE positions SET shares = ? WHERE ticker = ?", (shares, ticker))

def mark_take_profit_taken(ticker: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET take_profit_taken = 1 WHERE ticker = ?", (ticker,)
        )

def update_position_peak(ticker: str, peak_price: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET peak_price = ? WHERE ticker = ? AND (peak_price IS NULL OR peak_price < ?)",
            (peak_price, ticker, peak_price),
        )

def delete_position(ticker: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))

def log_closed_position(ticker: str, entry_price: float, exit_price: float,
                        shares: float, entry_date: str, exit_date: str,
                        exit_reason: str, signal_id: int | None,
                        signal_source: str = "congressional",
                        costs: float = 0.0,
                        entry_commission: float = 0.0) -> None:
    # Realized PnL = gross proceeds - entry cost (including both-side commissions)
    gross_proceeds = exit_price * shares - costs          # exit proceeds after commission
    entry_cost = entry_price * shares + entry_commission  # entry cost including commission
    realized_pnl = gross_proceeds - entry_cost
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO closed_positions
               (ticker, entry_price, exit_price, shares, entry_date, exit_date,
                exit_reason, realized_pnl, signal_id, closed_at, signal_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, entry_price, exit_price, shares, entry_date, exit_date,
             exit_reason, realized_pnl, signal_id, datetime.now(UTC).isoformat(),
             signal_source),
        )

def get_closed_positions() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM closed_positions ORDER BY exit_date DESC"
        ).fetchall()

def get_portfolio_stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT realized_pnl FROM closed_positions"
        ).fetchall()
    if not rows:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_realized_pnl": 0.0}
    pnls = [r["realized_pnl"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)  # break-even counted as loss
    return {
        "total_trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(pnls),
        "total_realized_pnl": sum(pnls),
    }

def get_performance_by_source() -> dict[str, list[float]]:
    """Returns {signal_source: [realized_pnl, ...]} for all closed positions."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT signal_source, realized_pnl FROM closed_positions"
        ).fetchall()
    result: dict[str, list[float]] = {}
    for row in rows:
        src = row["signal_source"] or "congressional"
        result.setdefault(src, []).append(float(row["realized_pnl"]))
    return result


def log_portfolio(date: str, cash: float, positions_value: float, total_nav: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_log (date, cash, positions_value, total_nav) VALUES (?, ?, ?, ?)",
            (date, cash, positions_value, total_nav),
        )

def get_nav_history() -> list[float]:
    """Return chronologically ordered list of total_nav values from portfolio_log."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT total_nav FROM portfolio_log ORDER BY date ASC, id ASC"
        ).fetchall()
    return [float(r["total_nav"]) for r in rows]

def log_regime(date: str, regime_label: str, regime_index: int,
               confidence: float, is_stable: bool, n_regimes: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO regime_log
               (date, regime_label, regime_index, confidence, is_stable, n_regimes, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (date, regime_label, regime_index, confidence, int(is_stable),
             n_regimes, datetime.now(UTC).isoformat()),
        )

def get_regime_history(days: int = 90) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM regime_log
               WHERE date >= date('now', ?)
               ORDER BY date ASC""",
            (f"-{days} days",),
        ).fetchall()

def get_latest_regime() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM regime_log ORDER BY date DESC LIMIT 1"
        ).fetchone()

def log_regime_transition(
    date: str,
    from_label: str,
    to_label: str,
    from_index: int,
    to_index: int,
    confidence: float,
    n_regimes: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO regime_transitions
               (date, from_label, to_label, from_index, to_index,
                confidence, n_regimes, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, from_label, to_label, from_index, to_index,
             confidence, n_regimes, datetime.now(UTC).isoformat()),
        )


def get_regime_transitions(days: int = 90) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM regime_transitions
               WHERE date >= date('now', ?)
               ORDER BY date ASC""",
            (f"-{days} days",),
        ).fetchall()


def log_risk_event(event_type: str, description: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO risk_events (event_type, description, data, logged_at)
               VALUES (?, ?, ?, ?)""",
            (event_type, description, json.dumps(data), datetime.now(UTC).isoformat()),
        )

def log_backtest_result(run_id: str, train_start: str, train_end: str,
                        test_start: str, test_end: str, n_regimes: int,
                        metrics: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO backtest_results
               (run_id, train_start, train_end, test_start, test_end,
                n_regimes, metrics, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, train_start, train_end, test_start, test_end,
             n_regimes, json.dumps(metrics), datetime.now(UTC).isoformat()),
        )

def get_recent_disclosures_for_ticker(ticker: str, since_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM disclosures
               WHERE ticker = ? AND transaction_date >= ?
               ORDER BY transaction_date DESC""",
            (ticker.upper(), since_date),
        ).fetchall()


def get_existing_insider_ids() -> set[str]:
    # Limit to the last 90 days — older Form 4 filings are never re-submitted.
    with get_conn() as conn:
        return {row[0] for row in conn.execute(
            "SELECT id FROM insider_disclosures WHERE disclosure_date >= date('now', '-90 days')"
        ).fetchall()}


def insert_insider_disclosures(disclosures: list[dict]) -> None:
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO insider_disclosures
               (id, insider_name, ticker, title, transaction_date, disclosure_date,
                transaction_type, amount_usd, scraped_at)
               VALUES (:id, :insider_name, :ticker, :title, :transaction_date,
                       :disclosure_date, :transaction_type, :amount_usd, :scraped_at)""",
            disclosures,
        )


def get_recent_insider_disclosures_for_ticker(ticker: str, since_date: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM insider_disclosures
               WHERE ticker = ? AND transaction_date >= ?
               ORDER BY transaction_date DESC""",
            (ticker.upper(), since_date),
        ).fetchall()


def insert_fundamental_signal(
    ticker: str,
    signal_date: str,
    composite_score: int,
    position_pct: float,
    rationale: str,
    signal_source: str = "fundamental",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO fundamental_signals
               (ticker, signal_date, composite_score, position_pct, rationale, signal_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ticker, signal_date, composite_score, position_pct, rationale,
             signal_source, datetime.now(UTC).isoformat()),
        )
        return cur.lastrowid


def get_fundamental_signals(since_date: str = "2000-01-01") -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT ticker, signal_date as date, composite_score, position_pct, 5 as conviction
               FROM fundamental_signals
               WHERE signal_date >= ?
               ORDER BY signal_date ASC""",
            (since_date,),
        ).fetchall()
