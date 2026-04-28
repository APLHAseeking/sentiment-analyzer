# Fundamental Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a parallel fundamental screener (value + momentum + quality) that runs daily alongside the congressional pipeline, merging signals so either can independently trigger trades and both together boost conviction.

**Architecture:** Six self-contained tasks. Tasks 1–2 add signal-source tracking to the DB and Portfolio layers. Task 3 builds the new factor screener module. Task 4 extends `score_entry` to handle fundamental and combined signals. Task 5 wires the Phase 2 pipeline into the scheduler. Task 6 adds per-source breakdown to the weekly report.

**Tech Stack:** Python 3.11+, yfinance, pandas, sqlite3, concurrent.futures, pytest, pytest-mock (all already in requirements.txt)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `bot/db.py` | **MODIFY** | Add `signal_source` column to `positions` + `closed_positions`; update `insert_position`, `log_closed_position`; add `get_performance_by_source()` |
| `bot/portfolio.py` | **MODIFY** | Thread `signal_source` through `open_position`, `close_position`, `reduce_position`; fix latent `signal_id or 0` bug |
| `bot/universe.py` | **MODIFY** | Add public `get_universe() -> set[str]` getter |
| `screener/__init__.py` | **CREATE** | Package init |
| `screener/factor_scorer.py` | **CREATE** | Lightweight yfinance fetch + percentile ranking + `run_factor_screen()` |
| `bot/ai_analyst.py` | **MODIFY** | Split `_ENTRY_SYSTEM` into blocks; add `signal_type`, `factor_score`, `ticker` params to `score_entry` |
| `bot/scheduler.py` | **MODIFY** | Collect `congress_skipped` in Phase 1; add Phase 2 fundamental loop |
| `bot/analytics.py` | **MODIFY** | Add per-source P&L breakdown to weekly report |
| `tests/test_db.py` | **MODIFY** | Tests for `signal_source` on positions and closed_positions |
| `tests/test_portfolio.py` | **MODIFY** | Tests for `signal_source` threading in open/close |
| `tests/test_factor_scorer.py` | **CREATE** | Tests for percentile ranking, missing-data exclusion, top-N |
| `tests/test_ai_analyst.py` | **MODIFY** | Tests for `signal_type="fundamental"` and `"both"` |
| `tests/test_scheduler.py` | **MODIFY** | Tests for Phase 2 pipeline and `signal_source` propagation |

---

## Task 1: DB — `signal_source` column + helpers

**Files:**
- Modify: `bot/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
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
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd "trading bot" && python -m pytest tests/test_db.py::test_insert_position_stores_signal_source tests/test_db.py::test_log_closed_position_stores_signal_source tests/test_db.py::test_signal_source_defaults_to_congressional tests/test_db.py::test_get_performance_by_source_groups_by_source -v
```
Expected: FAIL — `insert_position()` / `log_closed_position()` don't accept `signal_source`

- [ ] **Step 3: Implement**

In `bot/db.py`, update `_SCHEMA` to include the new columns in both tables:

```python
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
"""
```

Add a migration function and call it from `init_db()`:

```python
def _migrate_db() -> None:
    """Add columns introduced after initial schema. Safe to run on existing DBs."""
    migrations = [
        "ALTER TABLE positions ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'congressional'",
        "ALTER TABLE closed_positions ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'congressional'",
    ]
    with get_conn() as conn:
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # column already exists


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
    _migrate_db()
```

Update `insert_position` to accept and store `signal_source`:

```python
def insert_position(ticker: str, entry_price: float, shares: float,
                    position_pct: float, entry_date: str,
                    signal_id: int | None, rationale: str,
                    signal_source: str = "congressional") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO positions
               (ticker, entry_price, shares, position_pct, entry_date, signal_id,
                rationale, peak_price, signal_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, entry_price, shares, position_pct, entry_date, signal_id,
             rationale, entry_price, signal_source),
        )
```

Update `log_closed_position` to accept `signal_source` and allow `signal_id=None`:

```python
def log_closed_position(ticker: str, entry_price: float, exit_price: float,
                        shares: float, entry_date: str, exit_date: str,
                        exit_reason: str, signal_id: int | None,
                        signal_source: str = "congressional") -> None:
    realized_pnl = (exit_price - entry_price) * shares
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
```

Add `get_performance_by_source()` after `get_portfolio_stats()`:

```python
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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd "trading bot" && python -m pytest tests/test_db.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add bot/db.py tests/test_db.py
git commit -m "feat: add signal_source tracking to positions and closed_positions"
```

---

## Task 2: Portfolio — thread `signal_source` through open/close

**Files:**
- Modify: `bot/portfolio.py`
- Modify: `tests/test_portfolio.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_portfolio.py`:

```python
from bot.portfolio import Portfolio


def test_open_position_stores_signal_source(mock_broker, db):
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    db.insert_disclosures([{
        "id": "src-p1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    portfolio.open_position("AAPL", 5.0, None, "test", 100.0, signal_source="fundamental")
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["signal_source"] == "fundamental"


def test_close_position_stores_signal_source(mock_broker, db):
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.close_position(
        ticker="AAPL", shares=10.0, exit_price=110.0,
        exit_reason="ai_exit", signal_id=None,
        entry_price=100.0, entry_date="2026-04-01",
        signal_source="fundamental",
    )
    rows = db.get_closed_positions()
    assert rows[0]["signal_source"] == "fundamental"


def test_open_position_defaults_source_to_congressional(mock_broker, db):
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("MSFT", 4.0, None, "test", 200.0)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "MSFT")
    assert pos["signal_source"] == "congressional"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd "trading bot" && python -m pytest tests/test_portfolio.py::test_open_position_stores_signal_source tests/test_portfolio.py::test_close_position_stores_signal_source tests/test_portfolio.py::test_open_position_defaults_source_to_congressional -v
```
Expected: FAIL — `open_position` / `close_position` don't accept `signal_source`

- [ ] **Step 3: Implement**

Replace `bot/portfolio.py` with:

```python
from datetime import date
import bot.db as db

MAX_POSITIONS = 20
MAX_POSITIONS_PER_DAY = 3
MAX_POSITION_PCT = 8.0


class Portfolio:
    def __init__(self, broker):
        self.broker = broker
        self._opened_today = 0

    def get_cash(self) -> float:
        return self.broker.get_cash()

    def can_open_new_position(self) -> bool:
        if len(self.broker.get_positions()) >= MAX_POSITIONS:
            return False
        if self._opened_today >= MAX_POSITIONS_PER_DAY:
            return False
        return True

    def reset_daily_counter(self) -> None:
        self._opened_today = 0

    def open_position(self, ticker: str, position_pct: float, signal_id: int | None,
                      rationale: str, entry_price: float,
                      signal_source: str = "congressional") -> None:
        position_pct = min(position_pct, MAX_POSITION_PCT)
        shares = (self.get_cash() * position_pct / 100) / entry_price
        self.broker.place_order(ticker=ticker, side="buy", qty=shares)
        db.insert_position(
            ticker=ticker,
            entry_price=entry_price,
            shares=shares,
            position_pct=position_pct,
            entry_date=date.today().isoformat(),
            signal_id=signal_id,
            rationale=rationale,
            signal_source=signal_source,
        )
        self._opened_today += 1

    def close_position(self, ticker: str, shares: float, exit_price: float,
                       exit_reason: str, signal_id: int | None, entry_price: float,
                       entry_date: str, signal_source: str = "congressional") -> None:
        self.broker.place_order(ticker=ticker, side="sell", qty=shares)
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=shares,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason=exit_reason,
            signal_id=signal_id,
            signal_source=signal_source,
        )
        db.delete_position(ticker)

    def reduce_position(self, ticker: str, shares: float, exit_price: float,
                        signal_id: int | None, entry_price: float, entry_date: str,
                        signal_source: str = "congressional") -> None:
        sell_qty = shares / 2
        self.broker.place_order(ticker=ticker, side="sell", qty=sell_qty)
        db.log_closed_position(
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            shares=sell_qty,
            entry_date=entry_date,
            exit_date=date.today().isoformat(),
            exit_reason="reduce",
            signal_id=signal_id,
            signal_source=signal_source,
        )
        db.update_position_shares(ticker, shares - sell_qty)

    def enforce_stop_losses(self, stop_loss_pct: float = 15.0) -> list[str]:
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            current = pos["current_price"]
            meta = open_positions.get(ticker, {})
            peak = meta.get("peak_price") or pos["avg_entry_price"]

            db.update_position_peak(ticker, current)

            drop_from_peak = (peak - current) / peak * 100
            if drop_from_peak >= stop_loss_pct:
                self.close_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    exit_reason="stop_loss",
                    signal_id=meta.get("signal_id"),
                    entry_price=meta.get("entry_price") or pos["avg_entry_price"],
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=meta.get("signal_source", "congressional"),
                )
                closed.append(ticker)
        return closed

    def enforce_take_profits(self, take_profit_pct: float = 25.0) -> list[str]:
        reduced = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            if ticker in reduced:
                continue
            entry = pos["avg_entry_price"]
            current = pos["current_price"]
            gain_pct = (current - entry) / entry * 100

            if gain_pct >= take_profit_pct:
                meta = open_positions.get(ticker, {})
                self.reduce_position(
                    ticker=ticker,
                    shares=pos["qty"],
                    exit_price=current,
                    signal_id=meta.get("signal_id"),
                    entry_price=meta.get("entry_price") or entry,
                    entry_date=meta.get("entry_date") or date.today().isoformat(),
                    signal_source=meta.get("signal_source", "congressional"),
                )
                reduced.append(ticker)
        return reduced

    @staticmethod
    def is_sector_capped(sector: str, sector_allocation: dict[str, float],
                         cap_pct: float = 30.0) -> bool:
        return sector_allocation.get(sector, 0.0) >= cap_pct

    @staticmethod
    def is_liquid_enough(position_size_usd: float, avg_daily_volume_usd: float,
                         max_adv_pct: float = 10.0) -> bool:
        if avg_daily_volume_usd <= 0:
            return False
        return (position_size_usd / avg_daily_volume_usd * 100) <= max_adv_pct

    @staticmethod
    def is_in_drawdown(peak_nav: float, current_nav: float,
                       max_drawdown_pct: float = 10.0) -> bool:
        if peak_nav <= 0:
            return False
        return (peak_nav - current_nav) / peak_nav * 100 >= max_drawdown_pct

    def log_snapshot(self) -> None:
        positions = self.broker.get_positions()
        positions_value = sum(p["qty"] * p["current_price"] for p in positions)
        cash = self.get_cash()
        db.log_portfolio(
            date=date.today().isoformat(),
            cash=cash,
            positions_value=positions_value,
            total_nav=cash + positions_value,
        )
```

- [ ] **Step 4: Run tests**

```bash
cd "trading bot" && python -m pytest tests/test_portfolio.py tests/test_db.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add bot/portfolio.py tests/test_portfolio.py
git commit -m "feat: thread signal_source through portfolio open/close"
```

---

## Task 3: Factor Screener

**Files:**
- Modify: `bot/universe.py`
- Create: `screener/__init__.py`
- Create: `screener/factor_scorer.py`
- Create: `tests/test_factor_scorer.py`

- [ ] **Step 1: Add `get_universe()` to `bot/universe.py`**

Add after `is_in_universe()`:

```python
def get_universe() -> set[str]:
    """Return a copy of the current universe. Empty set if not yet initialized."""
    return set(_UNIVERSE)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_factor_scorer.py`:

```python
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from screener.factor_scorer import (
    _build_factor_df,
    _compute_composite,
    run_factor_screen,
    FactorCandidate,
)


def _make_info(pe=15.0, pb=2.0, fcf=1e9, mcap=10e9, roe=0.15, margin=0.20, de=0.5):
    return {
        "trailingPE": pe,
        "priceToBook": pb,
        "freeCashflow": fcf,
        "marketCap": mcap,
        "returnOnEquity": roe,
        "profitMargins": margin,
        "debtToEquity": de,
    }


def test_build_factor_df_basic():
    infos = {"AAPL": _make_info(), "MSFT": _make_info(pe=30.0, pb=4.0)}
    momentum = {"AAPL": (5.0, 10.0), "MSFT": (1.0, 2.0)}
    df = _build_factor_df(infos, momentum)
    assert set(df.index) == {"AAPL", "MSFT"}
    assert "pe_inv" in df.columns
    assert df.loc["AAPL", "mom_1m"] == pytest.approx(5.0)


def test_build_factor_df_skips_none_info():
    infos = {"AAPL": _make_info(), "BAD": None}
    momentum = {"AAPL": (5.0, 10.0), "BAD": (None, None)}
    df = _build_factor_df(infos, momentum)
    assert "BAD" not in df.index
    assert "AAPL" in df.index


def test_compute_composite_excludes_sparse_data():
    # Ticker with fewer than 4 valid metrics gets excluded
    infos = {
        "GOOD": _make_info(),
        "SPARSE": _make_info(pe=None, pb=None, fcf=None, mcap=None),
    }
    momentum = {"GOOD": (5.0, 10.0), "SPARSE": (None, None)}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert "GOOD" in scored.index
    assert "SPARSE" not in scored.index


def test_compute_composite_prefers_low_pe():
    # Cheap stock (low PE) should score better on value than expensive stock
    infos = {
        "CHEAP": _make_info(pe=8.0, pb=1.0),
        "EXPENSIVE": _make_info(pe=60.0, pb=6.0),
    }
    momentum = {"CHEAP": (5.0, 10.0), "EXPENSIVE": (5.0, 10.0)}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert scored.loc["CHEAP", "value_score"] > scored.loc["EXPENSIVE", "value_score"]


def test_compute_composite_returns_scores_in_range():
    infos = {t: _make_info() for t in ["A", "B", "C"]}
    momentum = {t: (5.0, 10.0) for t in ["A", "B", "C"]}
    df = _build_factor_df(infos, momentum)
    scored = _compute_composite(df)
    assert (scored["composite_score"] >= 0).all()
    assert (scored["composite_score"] <= 99).all()


def test_run_factor_screen_empty_tickers():
    result = run_factor_screen([], top_n=5)
    assert result == []


def test_run_factor_screen_returns_top_n(mocker):
    tickers = [f"T{i}" for i in range(10)]
    mock_info = _make_info()
    mocker.patch(
        "screener.factor_scorer._fetch_info",
        side_effect=lambda t: (t, mock_info),
    )
    mock_hist = pd.DataFrame(
        {t: [100.0] * 63 for t in tickers},
        index=pd.date_range("2026-01-01", periods=63),
    )
    mocker.patch(
        "screener.factor_scorer.yf.download",
        return_value=pd.DataFrame({"Close": mock_hist}),  # simplified
    )
    mocker.patch("screener.factor_scorer.gather_research", return_value=None)
    result = run_factor_screen(tickers, top_n=3)
    assert len(result) <= 3
    assert all(isinstance(c, FactorCandidate) for c in result)


def test_run_factor_screen_all_none_returns_empty(mocker):
    mocker.patch(
        "screener.factor_scorer._fetch_info",
        side_effect=lambda t: (t, None),
    )
    mocker.patch("screener.factor_scorer.yf.download", return_value=pd.DataFrame())
    result = run_factor_screen(["AAPL", "MSFT"], top_n=5)
    assert result == []
```

- [ ] **Step 3: Run to confirm failure**

```bash
cd "trading bot" && python -m pytest tests/test_factor_scorer.py -v
```
Expected: FAIL — `screener.factor_scorer` does not exist

- [ ] **Step 4: Create the screener package**

Create `screener/__init__.py` (empty):
```python
```

Create `screener/factor_scorer.py`:

```python
from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from bot.researcher import gather_research, ResearchReport

log = logging.getLogger(__name__)

_FACTOR_KEYS = [
    "trailingPE", "priceToBook", "freeCashflow", "marketCap",
    "returnOnEquity", "profitMargins", "debtToEquity",
]
_MIN_VALID_METRICS = 4


@dataclass(frozen=True)
class FactorCandidate:
    ticker: str
    composite_score: int
    value_score: int
    momentum_score: int
    quality_score: int
    research: ResearchReport | None


def _fetch_info(ticker: str) -> tuple[str, dict | None]:
    try:
        return ticker, yf.Ticker(ticker).info
    except Exception:
        return ticker, None


def _fetch_momentum_batch(
    tickers: list[str],
) -> dict[str, tuple[float | None, float | None]]:
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, period="3mo", auto_adjust=True, progress=False)
        # yf.download with multiple tickers returns MultiIndex columns
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        elif "Close" in raw.columns:
            close = raw[["Close"]].rename(columns={"Close": tickers[0]})
        else:
            return {t: (None, None) for t in tickers}

        result: dict[str, tuple[float | None, float | None]] = {}
        for t in tickers:
            try:
                col = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
                if len(col) < 2:
                    result[t] = (None, None)
                    continue
                current = float(col.iloc[-1])
                p1m = float(col.iloc[max(0, len(col) - 21)])
                p3m = float(col.iloc[0])
                result[t] = (
                    (current / p1m - 1) * 100 if p1m > 0 else None,
                    (current / p3m - 1) * 100 if p3m > 0 else None,
                )
            except Exception:
                result[t] = (None, None)
        return result
    except Exception:
        return {t: (None, None) for t in tickers}


def _build_factor_df(
    infos: dict[str, dict | None],
    momentum: dict[str, tuple[float | None, float | None]],
) -> pd.DataFrame:
    rows = []
    for ticker, info in infos.items():
        if info is None:
            continue
        try:
            def _f(v: object) -> float | None:
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            pe = _f(info.get("trailingPE"))
            pb = _f(info.get("priceToBook"))
            fcf = _f(info.get("freeCashflow"))
            mcap = _f(info.get("marketCap"))
            roe = _f(info.get("returnOnEquity"))
            margin = _f(info.get("profitMargins"))
            de = _f(info.get("debtToEquity"))
            fcf_yield = fcf / mcap if fcf and mcap and mcap > 0 else None
            mom1m, mom3m = momentum.get(ticker, (None, None))

            rows.append({
                "ticker": ticker,
                "pe_inv": -pe if pe and pe > 0 else None,
                "pb_inv": -pb if pb and pb > 0 else None,
                "fcf_yield": fcf_yield,
                "roe": roe,
                "margin": margin,
                "de_inv": -de if de is not None and de >= 0 else None,
                "mom_1m": mom1m,
                "mom_3m": mom3m,
            })
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ticker")


def _compute_composite(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    primary = ["pe_inv", "pb_inv", "fcf_yield", "roe", "margin", "de_inv"]
    valid_mask = df[primary].notna().sum(axis=1) >= _MIN_VALID_METRICS
    df = df[valid_mask].copy()
    if df.empty:
        return df

    ranked = df.rank(pct=True, na_option="keep")

    df["value_score"] = (
        ranked[["pe_inv", "pb_inv", "fcf_yield"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)
    df["momentum_score"] = (
        ranked[["mom_1m", "mom_3m"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)
    df["quality_score"] = (
        ranked[["roe", "margin", "de_inv"]].mean(axis=1, skipna=True) * 33
    ).fillna(0).clip(0, 33).astype(int)
    df["composite_score"] = (
        df["value_score"] + df["momentum_score"] + df["quality_score"]
    ).clip(0, 99)
    return df


def run_factor_screen(tickers: list[str], top_n: int = 12) -> list[FactorCandidate]:
    if not tickers:
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(_fetch_info, tickers))
    infos = dict(results)

    momentum = _fetch_momentum_batch(tickers)

    df = _build_factor_df(infos, momentum)
    if df.empty:
        return []

    scored = _compute_composite(df)
    if scored.empty:
        return []

    top = scored.nlargest(top_n, "composite_score")

    candidates: list[FactorCandidate] = []
    for ticker_idx, row in top.iterrows():
        t = str(ticker_idx)
        research = gather_research(t)
        candidates.append(FactorCandidate(
            ticker=t,
            composite_score=int(row["composite_score"]),
            value_score=int(row["value_score"]),
            momentum_score=int(row["momentum_score"]),
            quality_score=int(row["quality_score"]),
            research=research,
        ))
    return candidates
```

- [ ] **Step 5: Run tests**

```bash
cd "trading bot" && python -m pytest tests/test_factor_scorer.py -v
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add screener/ bot/universe.py tests/test_factor_scorer.py
git commit -m "feat: factor screener with value/momentum/quality percentile ranking"
```

---

## Task 4: AI Analyst — extend `score_entry`

**Files:**
- Modify: `bot/ai_analyst.py`
- Modify: `tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ai_analyst.py`:

```python
def test_score_entry_fundamental_omits_congressional_fields(mocker):
    payload = json.dumps({"conviction": 7, "position_pct": 4.0,
                          "rationale": "Good fundamentals", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    result = score_entry(
        disclosure=None, committees=[], sector="Technology",
        lag_days=0, estimated_cost_pct=0.05,
        signal_type="fundamental", factor_score=82, ticker="MSFT",
    )
    call_kwargs = mock_client_from(mocker)
    prompt = call_kwargs["messages"][0]["content"]
    assert "Politician" not in prompt
    assert "Committees" not in prompt
    assert "factor score" in prompt.lower()
    assert "82" in prompt
    assert isinstance(result, EntryScore)


def test_score_entry_both_includes_all_fields(mocker):
    payload = json.dumps({"conviction": 9, "position_pct": 6.0,
                          "rationale": "Strong both", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "b1", "politician": "Jane Doe", "ticker": "AAPL",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(
        disclosure=disc, committees=["House Energy"],
        sector="Technology", lag_days=2, estimated_cost_pct=0.05,
        signal_type="both", factor_score=78, cluster_count=2,
    )
    call_kwargs = mock_client_from(mocker)
    prompt = call_kwargs["messages"][0]["content"]
    assert "Politician" in prompt
    assert "factor score" in prompt.lower()
    assert "78" in prompt
    assert isinstance(result, EntryScore)


def test_score_entry_congressional_default_unchanged(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "c1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy"],
                         sector="Energy", lag_days=2, estimated_cost_pct=0.05)
    call_kwargs = mock_client_from(mocker)
    prompt = call_kwargs["messages"][0]["content"]
    assert "Politician" in prompt
    assert "factor score" not in prompt.lower()
    assert isinstance(result, EntryScore)
```

Add helper at top of `tests/test_ai_analyst.py` (after `_mock_claude`):

```python
def mock_client_from(mocker):
    """Retrieve the call_kwargs from the last messages.create call."""
    import bot.ai_analyst as m
    return m._get_client().messages.create.call_args[1]
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd "trading bot" && python -m pytest tests/test_ai_analyst.py::test_score_entry_fundamental_omits_congressional_fields tests/test_ai_analyst.py::test_score_entry_both_includes_all_fields tests/test_ai_analyst.py::test_score_entry_congressional_default_unchanged -v
```
Expected: FAIL — `score_entry` doesn't accept `signal_type`, `factor_score`, or `ticker`

- [ ] **Step 3: Implement**

Replace `bot/ai_analyst.py` with:

```python
import json
from dataclasses import dataclass
from typing import Literal

from anthropic import Anthropic
from bot.config import ANTHROPIC_API_KEY

# ── System prompt blocks ──────────────────────────────────────────────────────

_ENTRY_SCHEMA = """You are a quantitative analyst evaluating a stock trade signal.
Respond with ONLY valid JSON matching this exact schema:
{"conviction": <int 1-10>, "position_pct": <float>, "rationale": <str>, "entry": <"buy"|"skip">, "risk_flags": [<str>]}

## Conviction → Position Size Rules
- conviction 1-4: entry="skip", position_pct=0
- conviction 5-6: position_pct 1.0-2.0
- conviction 7-8: position_pct 3.0-5.0
- conviction 9-10: position_pct 6.0-8.0

## Entry Hurdle
- Only set entry="buy" if expected return exceeds estimated_cost_pct by at least 2x"""

_CONGRESSIONAL_RULES = """
## Congressional Signal Rules

## Lag Decay
- lag_days 15-30: penalise conviction -2
- lag_days 31-45: penalise conviction -3 and cap position_pct at 2.0

## Cluster Signal Boost
- cluster_count 2-3 (other members buying same stock in last 30d): +1 conviction
- cluster_count 4+: +2 conviction (strong institutional knowledge signal)

## Transaction Size
- Amount > $100,000: +1 conviction (large conviction trade)
- Amount $50,001-$100,000: full conviction
- Amount $15,001-$50,000: neutral (no bonus)"""

_FUNDAMENTAL_RULES = """
## Fundamental Factor Score Rules
The composite factor score (0-99) combines value, momentum, and quality percentile ranks within the S&P 500 + Russell 1000 universe.
- score 80-99: strong factor signal, +2 conviction
- score 60-79: moderate factor signal, +1 conviction
- score 40-59: neutral
- score <40: weak factor signal, -1 conviction"""

_BOTH_BONUS = """
## Combined Signal Bonus
Both a congressional disclosure and the fundamental factor screen flag this ticker: +1 conviction bonus."""

_RESEARCH_ADJUSTMENTS = """
## Fundamental Adjustment (if research provided)
- Cyclical company at peak earnings (high ROE, high margins, late-cycle sector like Materials/Energy): mentally normalize earnings — do NOT take headline P/E at face value
- Negative earnings (P/E = n/a): conviction -1 unless revenue growth >30% and sector is high-growth tech/biotech
- Clearly overvalued (EV/EBITDA >30x with <10% growth): conviction -2
- High short interest (>15% of float) with congressional purchase: SHORT SQUEEZE potential → +1 conviction
- Deteriorating fundamentals (revenue growth negative + margin compression): conviction -2
- Financially healthy, undervalued, positive momentum: conviction +1 to +2"""

_EXIT_SYSTEM = """You are a quantitative analyst reviewing an open stock position.
Respond with ONLY valid JSON: {"action": <"hold"|"exit"|"reduce">, "rationale": <str>}

## Actions
- exit: sell entire position at next open
- reduce: sell 50% at next open
- hold: keep position

## Exit Rules
- P&L < -12%: exit immediately (approaching hard stop — don't wait for -15%)
- P&L > +40%: exit (full profit-taking)
- P&L +25% to +40%: reduce (lock in half the gain; let the other half run)
- days_held > 60 with P&L < +5%: exit (cost of capital exceeds return; redeploy)
- days_held > 90: exit regardless (information advantage fully priced in by now)
- Hold if P&L -12% to +25% and no material negative news

## Research Adjustment
- If research shows deteriorating fundamentals (margins falling, revenue declining): exit even if P&L positive
- If research shows strong momentum + positive earnings growth: hold even near the +25% reduce level"""

_VALID_ENTRY_VALUES = {"buy", "skip"}
_VALID_ACTION_VALUES = {"hold", "exit", "reduce"}

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _build_entry_system(signal_type: str, has_disclosure: bool = True) -> str:
    parts = [_ENTRY_SCHEMA]
    # Only include congressional lag/cluster rules when actual disclosure data is present
    if signal_type in ("congressional", "both") and has_disclosure:
        parts.append(_CONGRESSIONAL_RULES)
    if signal_type in ("fundamental", "both"):
        parts.append(_FUNDAMENTAL_RULES)
    if signal_type == "both":
        parts.append(_BOTH_BONUS)
    parts.append(_RESEARCH_ADJUSTMENTS)
    return "\n".join(parts)


@dataclass(frozen=True)
class EntryScore:
    conviction: int
    position_pct: float
    rationale: str
    entry: str
    risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class ExitDecision:
    action: str
    rationale: str


def parse_entry_response(text: str) -> EntryScore:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned invalid JSON for entry: {text!r}") from exc
    conviction = int(data["conviction"])
    if not (1 <= conviction <= 10):
        raise ValueError(f"conviction {conviction} out of range 1-10")
    entry = data["entry"]
    if entry not in _VALID_ENTRY_VALUES:
        raise ValueError(f"entry {entry!r} not in {_VALID_ENTRY_VALUES}")
    return EntryScore(
        conviction=conviction,
        position_pct=float(data["position_pct"]),
        rationale=data["rationale"],
        entry=entry,
        risk_flags=tuple(data.get("risk_flags", [])),
    )


def parse_exit_response(text: str) -> ExitDecision:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned invalid JSON for exit: {text!r}") from exc
    action = data["action"]
    if action not in _VALID_ACTION_VALUES:
        raise ValueError(f"action {action!r} not in {_VALID_ACTION_VALUES}")
    return ExitDecision(action=action, rationale=data["rationale"])


def score_entry(
    disclosure: dict | None,
    committees: list[str],
    sector: str,
    lag_days: int,
    estimated_cost_pct: float,
    research: "ResearchReport | None" = None,
    cluster_count: int = 1,
    signal_type: str = "congressional",
    factor_score: int | None = None,
    ticker: str | None = None,
) -> EntryScore:
    from bot.researcher import format_research_for_prompt
    _ticker = (disclosure["ticker"] if disclosure else ticker) or "UNKNOWN"

    lines = [f"Ticker: {_ticker} | Sector: {sector}"]

    if signal_type in ("congressional", "both") and disclosure:
        lines += [
            f"Politician: {disclosure['politician']}",
            f"Transaction date: {disclosure['transaction_date']} | "
            f"Disclosure date: {disclosure['disclosure_date']}",
            f"Lag days: {lag_days}",
            f"Amount range: {disclosure['amount_range']}",
            f"Committees held: {', '.join(committees)}",
            f"Cluster count (other members buying same stock last 30d): {cluster_count}",
        ]

    if signal_type in ("fundamental", "both") and factor_score is not None:
        lines.append(f"Composite factor score: {factor_score}/99")

    lines.append(f"Estimated round-trip cost: {estimated_cost_pct:.2f}% of position")

    if research is not None:
        lines.append("\n" + format_research_for_prompt(research))

    lines.append("Score this signal.")
    prompt = "\n".join(lines)

    system_text = _build_entry_system(signal_type, has_disclosure=disclosure is not None)
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{"type": "text", "text": system_text,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_entry_response(resp.content[0].text)


def review_exit(ticker: str, entry_price: float, current_price: float,
                days_held: int, research: "ResearchReport | None" = None) -> ExitDecision:
    from bot.researcher import format_research_for_prompt
    pnl_pct = (current_price - entry_price) / entry_price * 100
    prompt = (
        f"Ticker: {ticker}\n"
        f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
        f"P&L: {pnl_pct:+.1f}%\n"
        f"Days held: {days_held}\n"
    )
    if research is not None:
        prompt += "\n" + format_research_for_prompt(research) + "\n"
    prompt += "Hold, reduce, or exit?"
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=[{"type": "text", "text": _EXIT_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_exit_response(resp.content[0].text)
```

- [ ] **Step 4: Run all AI analyst tests**

```bash
cd "trading bot" && python -m pytest tests/test_ai_analyst.py -v
```
Expected: all pass (existing + new tests)

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add bot/ai_analyst.py tests/test_ai_analyst.py
git commit -m "feat: extend score_entry with signal_type and factor_score params"
```

---

## Task 5: Scheduler — Phase 2 fundamental pipeline

**Files:**
- Modify: `bot/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scheduler.py`:

```python
def test_phase2_opens_fundamental_position(mocker, db):
    from screener.factor_scorer import FactorCandidate
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler.get_universe", return_value={"MSFT"})
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[
        FactorCandidate(ticker="MSFT", composite_score=85, value_score=30,
                        momentum_score=28, quality_score=27, research=None),
    ])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Technology")
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 300.0}
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_called_once()
    call_kwargs = portfolio.open_position.call_args[1]
    assert call_kwargs["signal_source"] == "fundamental"


def test_phase2_skips_already_opened_ticker(mocker, db):
    from screener.factor_scorer import FactorCandidate
    # Simulate MSFT already open (portfolio is mocked so doesn't write to DB;
    # mock get_open_positions directly so Phase 2 sees it)
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler.get_open_positions", return_value=[
        {"ticker": "MSFT", "entry_price": 300.0, "shares": 10.0,
         "entry_date": "2026-04-01", "signal_id": 1, "signal_source": "congressional"},
    ])
    mocker.patch("bot.scheduler.get_universe", return_value={"MSFT"})
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[
        FactorCandidate(ticker="MSFT", composite_score=85, value_score=30,
                        momentum_score=28, quality_score=27, research=None),
    ])
    mock_score = mocker.patch("bot.scheduler.score_entry")
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    # score_entry and open_position should never be called for MSFT in Phase 2
    mock_score.assert_not_called()
    portfolio.open_position.assert_not_called()


def test_phase2_uses_both_signal_type_when_congress_skipped(mocker, db):
    disc = {
        "id": "both-001", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    from screener.factor_scorer import FactorCandidate
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["House Energy"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Technology")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=1)
    mocker.patch("bot.scheduler.gather_research", return_value=None)
    mocker.patch("bot.scheduler.get_universe", return_value={"AAPL"})
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[
        FactorCandidate(ticker="AAPL", composite_score=82, value_score=29,
                        momentum_score=27, quality_score=26, research=None),
    ])
    mock_score = mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=3, position_pct=0.0, rationale="Skip", entry="skip", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 170.0}
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    # Second call (Phase 2) should use signal_type="both"
    assert mock_score.call_count >= 2
    phase2_call = mock_score.call_args_list[-1]
    assert phase2_call[1].get("signal_type") == "both"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd "trading bot" && python -m pytest tests/test_scheduler.py::test_phase2_opens_fundamental_position tests/test_scheduler.py::test_phase2_skips_already_opened_ticker tests/test_scheduler.py::test_phase2_uses_both_signal_type_when_congress_skipped -v
```
Expected: FAIL — `run_morning_pipeline` has no Phase 2

- [ ] **Step 3: Implement**

Replace `bot/scheduler.py` with:

```python
import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
import exchange_calendars as xcals
from apscheduler.schedulers.blocking import BlockingScheduler

from bot.analytics import log_weekly_report
from bot.researcher import gather_research
from bot.scraper import run_scraper
from bot.signal_engine import filter_disclosures, get_sector_for_ticker, compute_lag_days, get_cluster_count
from bot.committee import get_committees_for_politician
from bot.ai_analyst import score_entry, review_exit, EntryScore
from bot.db import get_open_positions, insert_signal
from bot.universe import refresh_universe, get_universe
from bot.portfolio import Portfolio
from screener.factor_scorer import run_factor_screen

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_ESTIMATED_COST_PCT = 0.05
_MAX_SECTOR_PCT = 30.0
_MAX_ADV_PCT = 10.0
_SCREENER_TOP_N = 12


def _is_trading_day() -> bool:
    return _NYSE.is_session(date.today().isoformat())


def _compute_sector_allocation(portfolio: Portfolio) -> dict[str, float]:
    positions = portfolio.broker.get_positions()
    if not positions:
        return {}
    nav = portfolio.get_cash() + sum(p["qty"] * p["current_price"] for p in positions)
    if nav <= 0:
        return {}
    allocation: dict[str, float] = {}
    for pos in positions:
        sector = get_sector_for_ticker(pos["ticker"])
        position_value = pos["qty"] * pos["current_price"]
        allocation[sector] = allocation.get(sector, 0.0) + (position_value / nav * 100)
    return allocation


def _try_open(portfolio: Portfolio, ticker: str, score: EntryScore,
              signal_id: int | None, research, sector_allocation: dict[str, float],
              sector: str, signal_source: str) -> bool:
    """Attempt to open a position after all risk checks. Returns True if opened."""
    if not portfolio.can_open_new_position():
        return False
    if portfolio.is_sector_capped(sector, sector_allocation, cap_pct=_MAX_SECTOR_PCT):
        log.info("Skipping %s: sector %r capped at %.1f%%", ticker, sector,
                 sector_allocation.get(sector, 0))
        return False
    if score.entry != "buy":
        return False

    entry_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
    if not entry_price:
        log.warning("No price for %s — skipping", ticker)
        return False

    position_size_usd = portfolio.get_cash() * score.position_pct / 100
    adv_usd = research.avg_daily_volume_usd if research else None
    if adv_usd and not portfolio.is_liquid_enough(position_size_usd, adv_usd, _MAX_ADV_PCT):
        log.info("Skipping %s: illiquid (position $%.0f vs ADV $%.0f)",
                 ticker, position_size_usd, adv_usd)
        return False

    portfolio.open_position(
        ticker=ticker,
        position_pct=score.position_pct,
        signal_id=signal_id,
        rationale=score.rationale,
        entry_price=entry_price,
        signal_source=signal_source,
    )
    return True


def run_morning_pipeline(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        log.info("Market closed — skipping morning pipeline")
        return
    log.info("Morning pipeline started")
    portfolio.reset_daily_counter()
    portfolio.enforce_stop_losses()
    portfolio.enforce_take_profits()

    new_disclosures = run_scraper()
    qualified = filter_disclosures(new_disclosures)
    log.info("Disclosures: %d new, %d qualified", len(new_disclosures), len(qualified))

    sector_allocation = _compute_sector_allocation(portfolio)
    congress_skipped: set[str] = set()

    # ── Phase 1: congressional signals ───────────────────────────────────────
    for disc in qualified:
        if not portfolio.can_open_new_position():
            log.info("Position limit reached — stopping Phase 1")
            break
        try:
            ticker = disc["ticker"]
            committees = get_committees_for_politician(disc["politician"])
            sector = get_sector_for_ticker(ticker)
            lag = compute_lag_days(disc["transaction_date"], disc["disclosure_date"])
            since = (date.today() - timedelta(days=30)).isoformat()
            cluster_count = get_cluster_count(ticker, since)
            research = gather_research(ticker)

            score: EntryScore = score_entry(
                disc, committees=committees, sector=sector,
                lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
                research=research, cluster_count=cluster_count,
                signal_type="congressional",
            )

            if score.entry != "buy":
                congress_skipped.add(ticker)
                log.info("Skipping %s (congressional): conviction %d", ticker, score.conviction)
                continue

            signal_id = insert_signal(
                disc["id"], ticker, score.conviction,
                score.position_pct, score.rationale, list(score.risk_flags),
            )
            opened = _try_open(portfolio, ticker, score, signal_id, research,
                               sector_allocation, sector, "congressional")
            if opened:
                sector_allocation = _compute_sector_allocation(portfolio)
                log.info("Opened %s (congressional) conviction=%d cluster=%d",
                         ticker, score.conviction, cluster_count)

        except Exception:
            log.exception("Failed processing congressional signal %s — skipping",
                          disc.get("ticker", "?"))

    # ── Phase 2: fundamental screener ────────────────────────────────────────
    try:
        universe = list(get_universe())
        candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
        already_open = {pos["ticker"] for pos in get_open_positions()}

        for candidate in candidates:
            if not portfolio.can_open_new_position():
                log.info("Position limit reached — stopping Phase 2")
                break
            ticker = candidate.ticker
            if ticker in already_open:
                continue

            signal_type = "both" if ticker in congress_skipped else "fundamental"
            sector = get_sector_for_ticker(ticker)

            try:
                score = score_entry(
                    disclosure=None,
                    committees=[],
                    sector=sector,
                    lag_days=0,
                    estimated_cost_pct=_ESTIMATED_COST_PCT,
                    research=candidate.research,
                    signal_type=signal_type,
                    factor_score=candidate.composite_score,
                    ticker=ticker,
                )

                if score.entry != "buy":
                    log.info("Skipping %s (%s): conviction %d", ticker, signal_type, score.conviction)
                    continue

                opened = _try_open(portfolio, ticker, score, None, candidate.research,
                                   sector_allocation, sector, signal_type)
                if opened:
                    sector_allocation = _compute_sector_allocation(portfolio)
                    log.info("Opened %s (%s) conviction=%d factor=%d",
                             ticker, signal_type, score.conviction, candidate.composite_score)

            except Exception:
                log.exception("Failed processing fundamental candidate %s — skipping", ticker)

    except Exception:
        log.exception("Phase 2 fundamental screener failed — skipping")


def run_exit_review(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        return
    log.info("Exit review started")
    for pos in get_open_positions():
        try:
            info = yf.Ticker(pos["ticker"]).info
            current_price = info.get("regularMarketPrice", pos["entry_price"])
            days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            research = gather_research(pos["ticker"])
            decision = review_exit(
                pos["ticker"], pos["entry_price"], current_price, days_held,
                research=research,
            )
            if decision.action == "exit":
                portfolio.close_position(
                    pos["ticker"], pos["shares"],
                    exit_price=current_price,
                    exit_reason="ai_exit",
                    signal_id=pos["signal_id"],
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                    signal_source=pos.get("signal_source", "congressional"),
                )
                log.info("Closed %s: %s", pos["ticker"], decision.rationale)
            elif decision.action == "reduce":
                portfolio.reduce_position(
                    pos["ticker"], pos["shares"],
                    exit_price=current_price,
                    signal_id=pos["signal_id"],
                    entry_price=pos["entry_price"],
                    entry_date=pos["entry_date"],
                    signal_source=pos.get("signal_source", "congressional"),
                )
                log.info("Reduced %s: %s", pos["ticker"], decision.rationale)
        except Exception:
            log.exception("Exit review failed for %s — skipping", pos.get("ticker", "?"))


def run_eod_snapshot(portfolio: Portfolio) -> None:
    portfolio.log_snapshot()
    log.info("EOD snapshot logged")


def start(portfolio: Portfolio) -> None:
    scheduler = BlockingScheduler(timezone=_AMS)
    scheduler.add_job(refresh_universe, "cron", day_of_week="mon", hour=7, minute=0)
    scheduler.add_job(lambda: run_morning_pipeline(portfolio), "cron", hour=14, minute=0)
    scheduler.add_job(lambda: run_exit_review(portfolio), "cron", hour=15, minute=0)
    scheduler.add_job(lambda: run_eod_snapshot(portfolio), "cron", hour=22, minute=30)
    scheduler.add_job(log_weekly_report, "cron", day_of_week="fri", hour=22, minute=45)
    log.info("Scheduler started — running in Amsterdam time (Europe/Amsterdam)")
    scheduler.start()
```

- [ ] **Step 4: Run all scheduler tests**

```bash
cd "trading bot" && python -m pytest tests/test_scheduler.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add bot/scheduler.py tests/test_scheduler.py
git commit -m "feat: Phase 2 fundamental screener pipeline in morning run"
```

---

## Task 6: Analytics — per-source P&L breakdown

**Files:**
- Modify: `bot/analytics.py`
- Modify: `tests/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_analytics.py`:

```python
def test_log_weekly_report_includes_per_source_breakdown(db, caplog):
    import logging
    db.log_closed_position("A", 100.0, 120.0, 10.0, "2026-04-01", "2026-04-28",
                           "ai_exit", None, signal_source="fundamental")
    db.log_closed_position("B", 100.0, 90.0, 10.0, "2026-04-01", "2026-04-28",
                           "ai_exit", None, signal_source="congressional")
    with caplog.at_level(logging.INFO, logger="bot.analytics"):
        from bot.analytics import log_weekly_report
        log_weekly_report()
    assert "fundamental" in caplog.text
    assert "congressional" in caplog.text
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd "trading bot" && python -m pytest tests/test_analytics.py::test_log_weekly_report_includes_per_source_breakdown -v
```
Expected: FAIL — `log_weekly_report` doesn't log per-source breakdown

- [ ] **Step 3: Implement**

Update `log_weekly_report` in `bot/analytics.py`:

```python
def log_weekly_report() -> None:
    report = compute_performance_report()
    log.info(
        "=== WEEKLY PERFORMANCE REPORT (%s) ===\n"
        "Closed trades: %d | Wins: %d | Losses: %d | Win rate: %.1f%%\n"
        "Total realized P&L: $%.2f\n"
        "Avg hold period: %.1f days\n"
        "Best trade: $%.2f | Worst: $%.2f",
        report.report_date,
        report.total_trades, report.wins, report.losses, report.win_rate * 100,
        report.total_realized_pnl,
        report.avg_hold_days,
        report.best_trade_pnl, report.worst_trade_pnl,
    )
    by_source = db.get_performance_by_source()
    for source, pnls in sorted(by_source.items()):
        wins = sum(1 for p in pnls if p > 0)
        log.info(
            "  [%s] %d trades | %d wins | $%.2f P&L",
            source, len(pnls), wins, sum(pnls),
        )
```

- [ ] **Step 4: Run all analytics tests**

```bash
cd "trading bot" && python -m pytest tests/test_analytics.py -v
```
Expected: all pass

- [ ] **Step 5: Run the full test suite**

```bash
cd "trading bot" && python -m pytest tests/ -v
```
Expected: all 201+ tests pass, 0 failures

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add bot/analytics.py tests/test_analytics.py
git commit -m "feat: per-source P&L breakdown in weekly report"
```
