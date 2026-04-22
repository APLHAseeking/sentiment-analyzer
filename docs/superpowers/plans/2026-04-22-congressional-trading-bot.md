# Congressional Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily-run Python trading bot that detects US congressional stock purchases in sectors the member's committee oversees, scores them with Claude AI, and executes paper trades via Alpaca.

**Architecture:** Eight focused modules under `bot/` share a single SQLite database. An APScheduler-based scheduler runs the full pipeline each afternoon in Amsterdam time (targeting US pre-market). All scheduling uses `zoneinfo` for correct DST handling. Tests use pytest with in-memory SQLite and mocked external APIs.

**Tech Stack:** Python 3.11+, anthropic, alpaca-py, apscheduler, beautifulsoup4, requests, yfinance, exchange-calendars, python-dotenv, pytest, pytest-mock

---

## File Map

| File | Responsibility |
|------|----------------|
| `bot/__init__.py` | Package marker |
| `bot/config.py` | Load and validate all env vars from `.env` |
| `bot/db.py` | SQLite schema init + all CRUD helpers |
| `bot/scraper.py` | Scrape Capitol Trades for new disclosures |
| `bot/universe.py` | S&P 500 + Russell 1000 membership, weekly refresh |
| `bot/committee.py` | Committee→GICS map + politician committee lookup via ProPublica API |
| `bot/signal_engine.py` | Apply all three signal filters + lag decay check |
| `bot/ai_analyst.py` | Claude entry scoring + daily exit review |
| `bot/portfolio.py` | Position tracking, stop-loss enforcement, daily limits |
| `bot/broker.py` | Alpaca API wrapper (paper → IBKR swap point) |
| `bot/scheduler.py` | APScheduler daily pipeline, Amsterdam-aware |
| `run_bot.py` | Entry point: init DB, refresh universe, start scheduler |
| `tests/conftest.py` | Shared pytest fixtures (in-memory DB, mock broker) |
| `tests/test_db.py` | DB schema and CRUD |
| `tests/test_scraper.py` | Scraper HTML parsing |
| `tests/test_universe.py` | Universe membership check |
| `tests/test_committee.py` | Committee mapping and lookup |
| `tests/test_signal_engine.py` | Signal filtering logic |
| `tests/test_ai_analyst.py` | Claude prompt construction and JSON parsing |
| `tests/test_portfolio.py` | Portfolio state management and limits |
| `tests/test_broker.py` | Broker interface |
| `tests/test_integration.py` | End-to-end smoke test |
| `requirements.txt` | All dependencies |
| `.env.example` | Template for secrets |

---

### Task 1: Project setup

**Files:**
- Create: `bot/__init__.py`
- Create: `bot/config.py`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p bot tests
touch bot/__init__.py tests/__init__.py
```

- [ ] **Step 2: Create requirements.txt**

```
anthropic>=0.40.0
alpaca-py>=0.26.0
apscheduler>=3.10.4
beautifulsoup4>=4.12.3
requests>=2.31.0
yfinance>=0.2.40
exchange-calendars>=4.5.4
python-dotenv>=1.0.1
pytest>=8.1.0
pytest-mock>=3.14.0
```

- [ ] **Step 3: Create bot/config.py**

```python
import os
from dotenv import load_dotenv

load_dotenv()

def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
ALPACA_API_KEY: str = _require("ALPACA_API_KEY")
ALPACA_SECRET_KEY: str = _require("ALPACA_SECRET_KEY")
ALPACA_BASE_URL: str = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PROPUBLICA_API_KEY: str = _require("PROPUBLICA_API_KEY")
DB_PATH: str = os.environ.get("DB_PATH", "trading.db")
```

- [ ] **Step 4: Create .env.example**

```
ANTHROPIC_API_KEY=your-anthropic-key
ALPACA_API_KEY=your-alpaca-key
ALPACA_SECRET_KEY=your-alpaca-secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
PROPUBLICA_API_KEY=your-propublica-key
DB_PATH=trading.db
```

- [ ] **Step 5: Create tests/conftest.py**

```python
import pytest
import importlib

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Initialise a fresh in-memory SQLite DB for each test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import bot.db
    importlib.reload(bot.db)
    bot.db.init_db()
    return bot.db

@pytest.fixture
def mock_broker(mocker):
    broker = mocker.MagicMock()
    broker.get_cash.return_value = 100_000.0
    broker.get_positions.return_value = []
    return broker
```

- [ ] **Step 6: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: all packages install without error.

- [ ] **Step 7: Commit**

```bash
git add bot/__init__.py bot/config.py requirements.txt .env.example tests/__init__.py tests/conftest.py
git commit -m "feat: project scaffolding and config"
```

---

### Task 2: Database layer

**Files:**
- Create: `bot/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

`tests/test_db.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_db.py -v
```
Expected: `ModuleNotFoundError` — `bot.db` doesn't exist yet.

- [ ] **Step 3: Implement bot/db.py**

```python
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
             str(risk_flags), datetime.utcnow().isoformat()),
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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_db.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/db.py tests/test_db.py
git commit -m "feat: database schema and CRUD helpers"
```

---

### Task 3: Capitol Trades scraper

**Files:**
- Create: `bot/scraper.py`
- Create: `tests/test_scraper.py`

- [ ] **Step 1: Write the failing test**

`tests/test_scraper.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_scraper.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/scraper.py**

```python
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from bot.db import get_existing_ids, insert_disclosures

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; congress-bot/1.0; research-only)"}
TRADES_URL = "https://capitoltrades.com/trades"

def _parse_trades_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.q-table tbody tr")
    trades = []
    for row in rows:
        cells = row.select("td")
        if len(cells) < 7:
            continue
        trade_id = row.get("data-id", "").strip()
        ticker = cells[2].get_text(strip=True)
        if not trade_id or not ticker:
            continue
        trades.append({
            "id": trade_id,
            "politician": cells[0].get_text(strip=True),
            "ticker": ticker,
            "transaction_type": cells[3].get_text(strip=True).lower(),
            "transaction_date": cells[4].get_text(strip=True),
            "disclosure_date": cells[5].get_text(strip=True),
            "amount_range": cells[6].get_text(strip=True),
            "scraped_at": datetime.utcnow().isoformat(),
        })
    return trades

def _fetch_page(page: int) -> str:
    resp = requests.get(
        TRADES_URL,
        params={"page": page, "pageSize": 100},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text

def run_scraper(max_pages: int = 3) -> list[dict]:
    """Fetch new disclosures from Capitol Trades and persist them. Returns new records."""
    existing = get_existing_ids()
    new_trades: list[dict] = []
    for page in range(1, max_pages + 1):
        html = _fetch_page(page)
        trades = _parse_trades_page(html)
        if not trades:
            break
        fresh = [t for t in trades if t["id"] not in existing]
        new_trades.extend(fresh)
        if len(fresh) < len(trades):
            break  # hit the already-seen boundary
    if new_trades:
        insert_disclosures(new_trades)
    return new_trades
```

**Note:** If `run_scraper()` returns 0 results when tested live, open `https://capitoltrades.com/trades` in a browser, right-click the trades table → Inspect, and check (1) the table's CSS class and (2) the column order. Update the selector `"table.q-table tbody tr"` and cell indices in `_parse_trades_page` accordingly. The logic stays the same.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_scraper.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/scraper.py tests/test_scraper.py
git commit -m "feat: Capitol Trades scraper"
```

---

### Task 4: Stock universe (S&P 500 + Russell 1000)

**Files:**
- Create: `bot/universe.py`
- Create: `tests/test_universe.py`

- [ ] **Step 1: Write the failing test**

`tests/test_universe.py`:
```python
import pandas as pd
from unittest.mock import patch
from bot.universe import is_in_universe, _build_universe

def test_is_in_universe_match():
    with patch("bot.universe._UNIVERSE", {"AAPL", "MSFT"}):
        assert is_in_universe("AAPL") is True

def test_is_in_universe_no_match():
    with patch("bot.universe._UNIVERSE", {"AAPL", "MSFT"}):
        assert is_in_universe("XYZ") is False

def test_is_in_universe_case_insensitive():
    with patch("bot.universe._UNIVERSE", {"AAPL"}):
        assert is_in_universe("aapl") is True

def test_build_universe_unions_sp500_and_russell(mocker):
    sp500_df = pd.DataFrame({"Symbol": ["AAPL", "MSFT"]})
    russell_df = pd.DataFrame({"Ticker": ["AAPL", "AMZN", "GOOG"]})
    mocker.patch("bot.universe._fetch_sp500", return_value=sp500_df)
    mocker.patch("bot.universe._fetch_russell1000", return_value=russell_df)
    result = _build_universe()
    assert result == {"AAPL", "MSFT", "AMZN", "GOOG"}
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_universe.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/universe.py**

```python
import io
import pandas as pd
import requests

_UNIVERSE: set[str] = set()

def _fetch_sp500() -> pd.DataFrame:
    tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    return tables[0][["Symbol"]]

def _fetch_russell1000() -> pd.DataFrame:
    url = (
        "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), skiprows=9)
    return df[["Ticker"]].dropna()

def _build_universe() -> set[str]:
    sp500 = set(_fetch_sp500()["Symbol"].str.strip().str.upper())
    russell = set(_fetch_russell1000()["Ticker"].str.strip().str.upper())
    return sp500 | russell

def refresh_universe() -> None:
    global _UNIVERSE
    _UNIVERSE = _build_universe()

def is_in_universe(ticker: str) -> bool:
    return ticker.upper() in _UNIVERSE
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_universe.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/universe.py tests/test_universe.py
git commit -m "feat: S&P 500 + Russell 1000 universe filter"
```

---

### Task 5: Committee mapping and politician lookup

**Files:**
- Create: `bot/committee.py`
- Create: `tests/test_committee.py`

- [ ] **Step 1: Write the failing test**

`tests/test_committee.py`:
```python
from unittest.mock import patch
from bot.committee import (
    COMMITTEE_SECTOR_MAP,
    get_committees_for_politician,
    sector_has_committee_overlap,
)

def test_map_has_entries():
    assert len(COMMITTEE_SECTOR_MAP) >= 10
    assert "Financial Services" in COMMITTEE_SECTOR_MAP["Senate Banking"]

def test_overlap_true():
    assert sector_has_committee_overlap("Financial Services", ["Senate Banking"]) is True

def test_overlap_false():
    assert sector_has_committee_overlap("Technology", ["Senate Agriculture"]) is False

def test_finance_committee_covers_all_sectors():
    assert sector_has_committee_overlap("Technology", ["Senate Finance"]) is True

def test_get_committees_calls_propublica(mocker):
    mocker.patch("bot.committee._search_propublica_member", return_value={
        "results": [{
            "roles": [{"committees": [{"name": "Senate Banking, Housing, and Urban Affairs"}]}]
        }]
    })
    committees = get_committees_for_politician("Jane Doe")
    assert any("Banking" in c for c in committees)

def test_get_committees_returns_empty_for_unknown(mocker):
    mocker.patch("bot.committee._search_propublica_member", return_value={"results": []})
    assert get_committees_for_politician("Nobody Known") == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_committee.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/committee.py**

```python
import requests
from functools import lru_cache
from bot.config import PROPUBLICA_API_KEY

COMMITTEE_SECTOR_MAP: dict[str, list[str]] = {
    "Senate Banking": ["Financial Services", "Real Estate"],
    "House Financial Services": ["Financial Services", "Real Estate"],
    "Senate Commerce": ["Consumer Cyclical", "Communication Services", "Technology"],
    "House Energy and Commerce": ["Energy", "Utilities", "Healthcare"],
    "Senate Armed Services": ["Industrials"],
    "House Armed Services": ["Industrials"],
    "Senate Agriculture": ["Consumer Defensive", "Basic Materials"],
    "House Agriculture": ["Consumer Defensive", "Basic Materials"],
    "Senate Finance": ["All"],
    "Senate HELP": ["Healthcare"],
    "Senate Environment": ["Utilities", "Energy", "Basic Materials"],
    "House Ways and Means": ["All"],
    "House Science": ["Technology"],
    "Senate Commerce Science": ["Technology", "Communication Services"],
}

_PROPUBLICA_BASE = "https://api.propublica.org/congress/v1"
_HEADERS = {"X-API-Key": PROPUBLICA_API_KEY}

def _search_propublica_member(name: str) -> dict:
    resp = requests.get(
        f"{_PROPUBLICA_BASE}/members/search.json",
        params={"query": name},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

@lru_cache(maxsize=512)
def get_committees_for_politician(name: str) -> list[str]:
    data = _search_propublica_member(name)
    results = data.get("results", [])
    if not results:
        return []
    committees: list[str] = []
    for role in results[0].get("roles", []):
        for c in role.get("committees", []):
            committees.append(c.get("name", ""))
    return committees

def _committee_covers_sector(committee_name: str, sector: str) -> bool:
    for key, sectors in COMMITTEE_SECTOR_MAP.items():
        if key.lower() in committee_name.lower():
            return "All" in sectors or sector in sectors
    return False

def sector_has_committee_overlap(sector: str, committees: list[str]) -> bool:
    return any(_committee_covers_sector(c, sector) for c in committees)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_committee.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/committee.py tests/test_committee.py
git commit -m "feat: committee jurisdiction mapping and ProPublica lookup"
```

---

### Task 6: Signal engine

**Files:**
- Create: `bot/signal_engine.py`
- Create: `tests/test_signal_engine.py`

- [ ] **Step 1: Write the failing test**

`tests/test_signal_engine.py`:
```python
from unittest.mock import patch
from bot.signal_engine import compute_lag_days, is_qualified_signal, filter_disclosures

def _disc(**kwargs):
    base = {
        "id": "x1", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-10", "disclosure_date": "2026-04-15",
        "amount_range": "$15,001 - $50,000",
    }
    return {**base, **kwargs}

def test_compute_lag_days():
    assert compute_lag_days("2026-04-01", "2026-04-10") == 9

def test_sale_disqualifies():
    assert is_qualified_signal(_disc(transaction_type="sale")) is False

def test_lag_over_45_disqualifies():
    disc = _disc(transaction_date="2026-01-01", disclosure_date="2026-04-22")
    assert is_qualified_signal(disc) is False

def test_not_in_universe_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=False):
        assert is_qualified_signal(disc) is False

def test_no_committees_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=[]):
        assert is_qualified_signal(disc) is False

def test_no_sector_overlap_disqualifies():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Agriculture"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Technology"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=False):
        assert is_qualified_signal(disc) is False

def test_qualified_purchase_passes():
    disc = _disc()
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        assert is_qualified_signal(disc) is True

def test_filter_disclosures():
    discs = [_disc(id="a"), _disc(id="b", transaction_type="sale")]
    with patch("bot.signal_engine.is_in_universe", return_value=True), \
         patch("bot.signal_engine.get_committees_for_politician", return_value=["Senate Banking"]), \
         patch("bot.signal_engine.get_sector_for_ticker", return_value="Financial Services"), \
         patch("bot.signal_engine.sector_has_committee_overlap", return_value=True):
        result = filter_disclosures(discs)
    assert len(result) == 1
    assert result[0]["id"] == "a"
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_signal_engine.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/signal_engine.py**

```python
from datetime import date
import yfinance as yf
from bot.universe import is_in_universe
from bot.committee import get_committees_for_politician, sector_has_committee_overlap

MAX_LAG_DAYS = 45

def compute_lag_days(transaction_date: str, disclosure_date: str) -> int:
    t = date.fromisoformat(transaction_date)
    d = date.fromisoformat(disclosure_date)
    return (d - t).days

def get_sector_for_ticker(ticker: str) -> str:
    return yf.Ticker(ticker).info.get("sector", "Unknown")

def is_qualified_signal(disclosure: dict) -> bool:
    if disclosure["transaction_type"] != "purchase":
        return False
    lag = compute_lag_days(disclosure["transaction_date"], disclosure["disclosure_date"])
    if lag > MAX_LAG_DAYS:
        return False
    if not is_in_universe(disclosure["ticker"]):
        return False
    committees = get_committees_for_politician(disclosure["politician"])
    if not committees:
        return False
    sector = get_sector_for_ticker(disclosure["ticker"])
    return sector_has_committee_overlap(sector, committees)

def filter_disclosures(disclosures: list[dict]) -> list[dict]:
    return [d for d in disclosures if is_qualified_signal(d)]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_signal_engine.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/signal_engine.py tests/test_signal_engine.py
git commit -m "feat: signal filtering engine with lag decay"
```

---

### Task 7: AI analyst (entry scoring and exit review)

**Files:**
- Create: `bot/ai_analyst.py`
- Create: `tests/test_ai_analyst.py`

- [ ] **Step 1: Write the failing test**

`tests/test_ai_analyst.py`:
```python
import json
from unittest.mock import MagicMock
from bot.ai_analyst import (
    EntryScore, ExitDecision,
    parse_entry_response, parse_exit_response,
    score_entry, review_exit,
)

def _mock_claude(mocker, text: str):
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

def test_parse_entry_buy():
    raw = json.dumps({"conviction": 7, "position_pct": 4.5,
                      "rationale": "Strong", "entry": "buy", "risk_flags": ["lag"]})
    s = parse_entry_response(raw)
    assert s.conviction == 7
    assert s.position_pct == 4.5
    assert s.entry == "buy"
    assert s.risk_flags == ["lag"]

def test_parse_entry_skip():
    raw = json.dumps({"conviction": 2, "position_pct": 0,
                      "rationale": "Weak", "entry": "skip", "risk_flags": []})
    assert parse_entry_response(raw).entry == "skip"

def test_score_entry_returns_entry_score(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy and Commerce"],
                         sector="Energy", lag_days=2, estimated_cost_pct=0.05)
    assert isinstance(result, EntryScore)
    assert result.conviction == 8

def test_parse_exit_hold():
    raw = json.dumps({"action": "hold", "rationale": "Momentum ok"})
    d = parse_exit_response(raw)
    assert d.action == "hold"

def test_review_exit_returns_exit_decision(mocker):
    payload = json.dumps({"action": "exit", "rationale": "Stop loss near"})
    _mock_claude(mocker, payload)
    result = review_exit("AAPL", 150.0, 125.0, 20, ["Bad news"])
    assert isinstance(result, ExitDecision)
    assert result.action == "exit"
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_ai_analyst.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/ai_analyst.py**

```python
import json
from dataclasses import dataclass
from anthropic import Anthropic
from bot.config import ANTHROPIC_API_KEY

_ENTRY_SYSTEM = """You are a quantitative analyst evaluating congressional stock trade signals.
Respond with ONLY valid JSON matching this exact schema:
{"conviction": <int 1-10>, "position_pct": <float>, "rationale": <str>, "entry": <"buy"|"skip">, "risk_flags": [<str>]}

Rules:
- conviction 1-4: entry="skip", position_pct=0
- conviction 5-6: position_pct 1.0-2.0
- conviction 7-8: position_pct 3.0-5.0
- conviction 9-10: position_pct 6.0-8.0
- Only set entry="buy" if expected return exceeds estimated_cost_pct by at least 2x
- Penalise conviction -2 if lag_days is 15-30
- Penalise conviction -3 and cap position_pct at 2.0 if lag_days is 31-45
- Raise conviction for larger transaction sizes or multiple members buying same stock"""

_EXIT_SYSTEM = """You are a quantitative analyst reviewing an open stock position.
Respond with ONLY valid JSON: {"action": <"hold"|"exit"|"reduce">, "rationale": <str>}
- exit: sell entire position at next open
- reduce: sell 50% at next open
- hold: keep position
Consider P&L, days held, recent news, and market conditions."""

def _get_client() -> Anthropic:
    return Anthropic(api_key=ANTHROPIC_API_KEY)

@dataclass
class EntryScore:
    conviction: int
    position_pct: float
    rationale: str
    entry: str
    risk_flags: list[str]

@dataclass
class ExitDecision:
    action: str
    rationale: str

def parse_entry_response(text: str) -> EntryScore:
    data = json.loads(text)
    return EntryScore(
        conviction=int(data["conviction"]),
        position_pct=float(data["position_pct"]),
        rationale=data["rationale"],
        entry=data["entry"],
        risk_flags=data.get("risk_flags", []),
    )

def parse_exit_response(text: str) -> ExitDecision:
    data = json.loads(text)
    return ExitDecision(action=data["action"], rationale=data["rationale"])

def score_entry(disclosure: dict, committees: list[str], sector: str,
                lag_days: int, estimated_cost_pct: float) -> EntryScore:
    prompt = (
        f"Politician: {disclosure['politician']}\n"
        f"Ticker: {disclosure['ticker']} | Sector: {sector}\n"
        f"Transaction date: {disclosure['transaction_date']} | "
        f"Disclosure date: {disclosure['disclosure_date']}\n"
        f"Lag days: {lag_days}\n"
        f"Amount range: {disclosure['amount_range']}\n"
        f"Committees held: {', '.join(committees)}\n"
        f"Estimated round-trip cost: {estimated_cost_pct:.2f}% of position\n"
        f"Score this signal."
    )
    client = _get_client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{"type": "text", "text": _ENTRY_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_entry_response(resp.content[0].text)

def review_exit(ticker: str, entry_price: float, current_price: float,
                days_held: int, recent_headlines: list[str]) -> ExitDecision:
    pnl_pct = (current_price - entry_price) / entry_price * 100
    headlines_text = "\n".join(f"- {h}" for h in recent_headlines[:5]) or "None"
    prompt = (
        f"Ticker: {ticker}\n"
        f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
        f"P&L: {pnl_pct:+.1f}%\n"
        f"Days held: {days_held}\n"
        f"Recent headlines:\n{headlines_text}\n"
        f"Hold, reduce, or exit?"
    )
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

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_ai_analyst.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/ai_analyst.py tests/test_ai_analyst.py
git commit -m "feat: Claude entry scoring and exit review"
```

---

### Task 8: Portfolio manager

**Files:**
- Create: `bot/portfolio.py`
- Create: `tests/test_portfolio.py`

- [ ] **Step 1: Write the failing test**

`tests/test_portfolio.py`:
```python
import pytest
from bot.portfolio import Portfolio

MAX_POSITIONS = 20

@pytest.fixture
def portfolio(db, mock_broker):
    return Portfolio(broker=mock_broker)

def test_initial_cash(portfolio, mock_broker):
    assert portfolio.get_cash() == 100_000.0

def test_can_open_when_under_limit(portfolio, mock_broker):
    mock_broker.get_positions.return_value = []
    assert portfolio.can_open_new_position() is True

def test_cannot_open_at_max_positions(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [
        {"ticker": f"T{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
        for i in range(MAX_POSITIONS)
    ]
    assert portfolio.can_open_new_position() is False

def test_cannot_open_after_daily_limit(portfolio, mock_broker):
    portfolio._opened_today = 3
    assert portfolio.can_open_new_position() is False

def test_open_position_places_order(portfolio, mock_broker):
    portfolio.open_position("AAPL", position_pct=5.0, signal_id=1,
                            rationale="Test", entry_price=150.0)
    mock_broker.place_order.assert_called_once()
    kwargs = mock_broker.place_order.call_args[1]
    assert kwargs["ticker"] == "AAPL"
    assert kwargs["side"] == "buy"

def test_open_position_caps_at_max_pct(portfolio, mock_broker):
    portfolio.open_position("AAPL", position_pct=15.0, signal_id=1,
                            rationale="Test", entry_price=100.0)
    kwargs = mock_broker.place_order.call_args[1]
    expected_shares = 100_000.0 * (8.0 / 100) / 100.0
    assert kwargs["qty"] == pytest.approx(expected_shares)

def test_stop_loss_triggers(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 33.0,
        "current_price": 100.0, "avg_entry_price": 120.0,
    }]
    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    assert "AAPL" in closed
    mock_broker.place_order.assert_called_with(ticker="AAPL", side="sell", qty=33.0)

def test_stop_loss_does_not_trigger_within_threshold(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 110.0, "avg_entry_price": 120.0,
    }]
    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    assert closed == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_portfolio.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/portfolio.py**

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

    def open_position(self, ticker: str, position_pct: float, signal_id: int,
                      rationale: str, entry_price: float) -> None:
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
        )
        self._opened_today += 1

    def close_position(self, ticker: str, shares: float) -> None:
        self.broker.place_order(ticker=ticker, side="sell", qty=shares)
        db.delete_position(ticker)

    def reduce_position(self, ticker: str, shares: float) -> None:
        self.broker.place_order(ticker=ticker, side="sell", qty=shares / 2)

    def enforce_stop_losses(self, stop_loss_pct: float = 15.0) -> list[str]:
        closed = []
        for pos in self.broker.get_positions():
            loss_pct = (pos["avg_entry_price"] - pos["current_price"]) / pos["avg_entry_price"] * 100
            if loss_pct >= stop_loss_pct:
                self.broker.place_order(ticker=pos["ticker"], side="sell", qty=pos["qty"])
                db.delete_position(pos["ticker"])
                closed.append(pos["ticker"])
        return closed

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
pytest tests/test_portfolio.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/portfolio.py tests/test_portfolio.py
git commit -m "feat: portfolio manager with stop-loss and position limits"
```

---

### Task 9: Alpaca broker wrapper

**Files:**
- Create: `bot/broker.py`
- Create: `tests/test_broker.py`

- [ ] **Step 1: Write the failing test**

`tests/test_broker.py`:
```python
from unittest.mock import MagicMock
from bot.broker import AlpacaBroker

def test_get_cash(mocker):
    mock_api = MagicMock()
    mock_api.get_account.return_value = MagicMock(cash="50000.00")
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    assert broker.get_cash() == 50_000.0

def test_get_positions(mocker):
    mock_pos = MagicMock()
    mock_pos.symbol = "AAPL"
    mock_pos.qty = "10"
    mock_pos.current_price = "150.00"
    mock_pos.avg_entry_price = "140.00"
    mock_api = MagicMock()
    mock_api.get_all_positions.return_value = [mock_pos]
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    positions = broker.get_positions()
    assert positions == [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 150.0, "avg_entry_price": 140.0,
    }]

def test_place_order_buy(mocker):
    mock_api = MagicMock()
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    broker.place_order(ticker="AAPL", side="buy", qty=10.0)
    mock_api.submit_order.assert_called_once()
    order = mock_api.submit_order.call_args[0][0]
    assert order.symbol == "AAPL"
    assert float(order.qty) == 10.0

def test_place_order_sell(mocker):
    mock_api = MagicMock()
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    broker.place_order(ticker="AAPL", side="sell", qty=5.0)
    order = mock_api.submit_order.call_args[0][0]
    from alpaca.trading.enums import OrderSide
    assert order.side == OrderSide.SELL
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_broker.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/broker.py**

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from bot.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

def _get_api() -> TradingClient:
    paper = "paper" in ALPACA_BASE_URL
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)

class AlpacaBroker:
    def __init__(self):
        self._api = _get_api()

    def get_cash(self) -> float:
        return float(self._api.get_account().cash)

    def get_positions(self) -> list[dict]:
        return [
            {
                "ticker": p.symbol,
                "qty": float(p.qty),
                "current_price": float(p.current_price),
                "avg_entry_price": float(p.avg_entry_price),
            }
            for p in self._api.get_all_positions()
        ]

    def place_order(self, ticker: str, side: str, qty: float) -> None:
        order = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        self._api.submit_order(order)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_broker.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/broker.py tests/test_broker.py
git commit -m "feat: Alpaca broker wrapper"
```

---

### Task 10: Scheduler

**Files:**
- Create: `bot/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

`tests/test_scheduler.py`:
```python
from unittest.mock import MagicMock, patch
from bot.scheduler import run_morning_pipeline, run_exit_review, run_eod_snapshot
from bot.ai_analyst import EntryScore, ExitDecision

def _make_portfolio(mocker):
    p = MagicMock()
    p.can_open_new_position.return_value = True
    p.get_cash.return_value = 100_000.0
    return p

def test_morning_skips_on_non_trading_day(mocker):
    mocker.patch("bot.scheduler._is_trading_day", return_value=False)
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_not_called()

def test_morning_no_signals(mocker):
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_not_called()

def test_morning_opens_on_buy_signal(mocker, db):
    disc = {
        "id": "x1", "politician": "Jane Doe", "ticker": "XOM",
        "transaction_type": "purchase",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["House Energy and Commerce"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Energy")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=[]
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 100.0}
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_called_once()

def test_exit_review_closes_on_exit(mocker, db):
    db.insert_disclosures([{
        "id": "pos1", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }])
    sid = db.insert_signal("pos1", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 150.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 155.0}
    mocker.patch("bot.scheduler.yf.Ticker").return_value.news = []
    mocker.patch("bot.scheduler.review_exit", return_value=ExitDecision("exit", "Take profit"))
    portfolio = _make_portfolio(mocker)
    run_exit_review(portfolio)
    portfolio.close_position.assert_called_once_with("AAPL", 10.0)

def test_eod_snapshot(mocker, db):
    portfolio = _make_portfolio(mocker)
    portfolio.broker = MagicMock()
    portfolio.broker.get_cash.return_value = 95_000.0
    portfolio.broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 155.0, "avg_entry_price": 150.0}
    ]
    from bot.portfolio import Portfolio
    real_portfolio = Portfolio(broker=portfolio.broker)
    run_eod_snapshot(real_portfolio)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM portfolio_log").fetchall()
    assert len(rows) == 1
    import pytest
    assert rows[0]["total_nav"] == pytest.approx(95_000.0 + 1_550.0)
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_scheduler.py -v
```
Expected: `ImportError`.

- [ ] **Step 3: Implement bot/scheduler.py**

```python
import logging
from datetime import date
from zoneinfo import ZoneInfo

import yfinance as yf
import exchange_calendars as xcals
from apscheduler.schedulers.blocking import BlockingScheduler

from bot.scraper import run_scraper
from bot.signal_engine import filter_disclosures, get_sector_for_ticker, compute_lag_days
from bot.committee import get_committees_for_politician
from bot.ai_analyst import score_entry, review_exit, EntryScore
from bot.db import get_open_positions, insert_signal
from bot.universe import refresh_universe
from bot.portfolio import Portfolio

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_ESTIMATED_COST_PCT = 0.05

def _is_trading_day() -> bool:
    return _NYSE.is_session(date.today().isoformat())

def run_morning_pipeline(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        log.info("Market closed — skipping morning pipeline")
        return
    log.info("Morning pipeline started")
    portfolio.reset_daily_counter()
    portfolio.enforce_stop_losses()
    new_disclosures = run_scraper()
    qualified = filter_disclosures(new_disclosures)
    log.info(f"Disclosures: {len(new_disclosures)} new, {len(qualified)} qualified")
    for disc in qualified:
        if not portfolio.can_open_new_position():
            log.info("Daily or total position limit reached — stopping")
            break
        committees = get_committees_for_politician(disc["politician"])
        sector = get_sector_for_ticker(disc["ticker"])
        lag = compute_lag_days(disc["transaction_date"], disc["disclosure_date"])
        score: EntryScore = score_entry(
            disc, committees=committees, sector=sector,
            lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
        )
        if score.entry != "buy":
            log.info(f"Skipping {disc['ticker']}: conviction {score.conviction}")
            continue
        signal_id = insert_signal(
            disc["id"], disc["ticker"], score.conviction,
            score.position_pct, score.rationale, score.risk_flags,
        )
        entry_price = yf.Ticker(disc["ticker"]).info.get("regularMarketPrice", 0)
        if not entry_price:
            log.warning(f"No price for {disc['ticker']} — skipping")
            continue
        portfolio.open_position(
            ticker=disc["ticker"], position_pct=score.position_pct,
            signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
        )
        log.info(f"Opened {disc['ticker']} conviction={score.conviction}")

def run_exit_review(portfolio: Portfolio) -> None:
    if not _is_trading_day():
        return
    log.info("Exit review started")
    for pos in get_open_positions():
        info = yf.Ticker(pos["ticker"]).info
        current_price = info.get("regularMarketPrice", pos["entry_price"])
        days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
        headlines = [h.get("title", "") for h in yf.Ticker(pos["ticker"]).news[:5]]
        decision = review_exit(
            pos["ticker"], pos["entry_price"], current_price, days_held, headlines
        )
        if decision.action == "exit":
            portfolio.close_position(pos["ticker"], pos["shares"])
            log.info(f"Closed {pos['ticker']}: {decision.rationale}")
        elif decision.action == "reduce":
            portfolio.reduce_position(pos["ticker"], pos["shares"])
            log.info(f"Reduced {pos['ticker']}: {decision.rationale}")

def run_eod_snapshot(portfolio: Portfolio) -> None:
    portfolio.log_snapshot()
    log.info("EOD snapshot logged")

def start(portfolio: Portfolio) -> None:
    scheduler = BlockingScheduler(timezone=_AMS)
    # Refresh stock universe every Monday at 07:00 Amsterdam
    scheduler.add_job(refresh_universe, "cron", day_of_week="mon", hour=7, minute=0)
    # Morning pipeline at 14:00 Amsterdam (08:00 ET)
    scheduler.add_job(lambda: run_morning_pipeline(portfolio), "cron", hour=14, minute=0)
    # Exit review at 15:00 Amsterdam (09:00 ET)
    scheduler.add_job(lambda: run_exit_review(portfolio), "cron", hour=15, minute=0)
    # EOD snapshot at 22:30 Amsterdam (16:30 ET)
    scheduler.add_job(lambda: run_eod_snapshot(portfolio), "cron", hour=22, minute=30)
    log.info("Scheduler started — running in Amsterdam time (Europe/Amsterdam)")
    scheduler.start()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_scheduler.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/scheduler.py tests/test_scheduler.py
git commit -m "feat: APScheduler pipeline with Amsterdam timezone"
```

---

### Task 11: Entry point and integration smoke test

**Files:**
- Create: `run_bot.py`
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_integration.py`:
```python
import pytest
from unittest.mock import MagicMock, patch

def test_full_pipeline_no_signals(mocker, db):
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mock_broker = MagicMock()
    mock_broker.get_cash.return_value = 100_000.0
    mock_broker.get_positions.return_value = []
    from bot.portfolio import Portfolio
    from bot.scheduler import run_morning_pipeline
    portfolio = Portfolio(broker=mock_broker)
    run_morning_pipeline(portfolio)
    mock_broker.place_order.assert_not_called()

def test_eod_snapshot_writes_to_db(mocker, db):
    mock_broker = MagicMock()
    mock_broker.get_cash.return_value = 90_000.0
    mock_broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 160.0, "avg_entry_price": 150.0}
    ]
    from bot.portfolio import Portfolio
    from bot.scheduler import run_eod_snapshot
    portfolio = Portfolio(broker=mock_broker)
    run_eod_snapshot(portfolio)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM portfolio_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["total_nav"] == pytest.approx(90_000.0 + 1_600.0)

def test_non_trading_day_skips_pipeline(mocker, db):
    mocker.patch("bot.scheduler._is_trading_day", return_value=False)
    mock_broker = MagicMock()
    mock_broker.get_cash.return_value = 100_000.0
    mock_broker.get_positions.return_value = []
    from bot.portfolio import Portfolio
    from bot.scheduler import run_morning_pipeline
    portfolio = Portfolio(broker=mock_broker)
    run_morning_pipeline(portfolio)
    mock_broker.place_order.assert_not_called()
```

- [ ] **Step 2: Run the full test suite**

```bash
pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 3: Create run_bot.py**

```python
import logging
from bot.db import init_db
from bot.universe import refresh_universe
from bot.broker import AlpacaBroker
from bot.portfolio import Portfolio
from bot.scheduler import start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

if __name__ == "__main__":
    init_db()
    refresh_universe()
    broker = AlpacaBroker()
    portfolio = Portfolio(broker=broker)
    start(portfolio)
```

- [ ] **Step 4: Manual smoke test with paper trading**

```bash
# Copy and fill in your keys
cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY, PROPUBLICA_API_KEY

# Run the bot
python run_bot.py
```

Expected output:
```
2026-04-22 14:00:00 INFO Scheduler started — running in Amsterdam time (Europe/Amsterdam)
2026-04-22 14:00:00 INFO Morning pipeline started
2026-04-22 14:00:01 INFO Disclosures: N new, M qualified
```

If Capitol Trades scraper returns 0 results: open `https://capitoltrades.com/trades` in a browser, inspect the trades table HTML, and update the selector in `bot/scraper.py:_parse_trades_page`.

- [ ] **Step 5: Commit**

```bash
git add run_bot.py tests/test_integration.py
git commit -m "feat: entry point and integration smoke test"
```

---

## API Keys You Need Before Running

| Key | Where to get it | Cost |
|-----|----------------|------|
| `ANTHROPIC_API_KEY` | Already in your existing app | Pay per use |
| `ALPACA_API_KEY` + `SECRET` | alpaca.markets → Paper Trading | Free |
| `PROPUBLICA_API_KEY` | projects.propublica.org/data-store | Free |
