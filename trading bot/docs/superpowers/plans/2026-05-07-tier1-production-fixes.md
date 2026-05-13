# Tier 1 Production Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the bot for live operation by fixing four Tier 1 gaps: config/portfolio disconnect, unscheduled HMM refit, no earnings/FOMC event awareness, and stub-only alerts.

**Architecture:** Four independent tracks, each touching 2–4 files. Tasks 1–3 are sequential (all touch `main_loop.py`). Task 4 is fully independent. Each task produces working, tested, committed software on its own.

**Tech Stack:** Python 3.14, pytest, pytest-mock, yfinance, requests, exchange_calendars, Anthropic SDK, SQLite.

---

## File Map

| File | Task | Change |
|---|---|---|
| `bot/portfolio.py` | 1 | Accept optional `risk_cfg`; read all thresholds from it |
| `orchestration/main_loop.py` | 1, 2, 3 | Pass `risk_cfg` to Portfolio; add `_maybe_rolling_refit()`; add event-calendar gate |
| `system/config.py` | 2, 3, 4 | Add `refit_interval_days`; `event_exclusion_window_days`; `MonitoringConfig` |
| `utils/__init__.py` | 3 | New empty package |
| `utils/event_calendar.py` | 3 | FOMC date list, yfinance earnings fetch, `has_upcoming_event()` |
| `monitoring/alerts.py` | 4 | `AlertSender` ABC, `WebhookAlertSender`, `LogAlertSender`, lazy config dispatch |
| `run_bot.py` | 4 | Add `--test-alerts` CLI flag |
| `tests/test_portfolio.py` | 1 | Add 3 config-propagation tests |
| `tests/test_orchestrator.py` | 2 | Add 2 rolling-refit tests |
| `tests/test_event_calendar.py` | 3 | New file — 6 tests |
| `tests/test_alerts.py` | 4 | New file — 5 tests |

---

## Task 1: Config/Portfolio Disconnect

**Context:** `bot/portfolio.py` hardcodes `MAX_POSITIONS`, `MAX_POSITIONS_PER_DAY`, `MAX_POSITION_PCT`, `stop_loss_pct=15.0`, `take_profit_pct=25.0`. All five values also live in `system/config.py` → `RiskConfig`. Changes to `RiskConfig` silently do nothing for Portfolio. The fix: inject `RiskConfig` into `Portfolio.__init__` and read every threshold from it.

**Files:**
- Modify: `bot/portfolio.py`
- Modify: `orchestration/main_loop.py`
- Modify: `tests/test_portfolio.py`

- [ ] **Step 1: Write the failing tests**

Add to the bottom of `tests/test_portfolio.py`:

```python
from system.config import RiskConfig


def test_portfolio_reads_max_positions_from_config(mock_broker):
    risk_cfg = RiskConfig(max_positions=5)
    p = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = [
        {"ticker": f"T{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
        for i in range(5)
    ]
    assert p.can_open_new_position() is False


def test_portfolio_reads_stop_loss_from_config(mock_broker, db):
    # 6% drop — triggers 5% custom threshold, would NOT trigger the default 15%
    risk_cfg = RiskConfig(trailing_stop_pct=5.0)
    p = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 94.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "cfg-sl-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("cfg-sl-01", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    closed = p.enforce_stop_losses()   # no explicit pct — must read from injected config
    assert "AAPL" in closed


def test_portfolio_reads_take_profit_from_config(mock_broker, db):
    # 6% gain — triggers 5% custom threshold, would NOT trigger the default 25%
    risk_cfg = RiskConfig(take_profit_pct=5.0)
    p = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = [{
        "ticker": "TSLA", "qty": 5.0,
        "current_price": 106.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "cfg-tp-01", "politician": "J", "ticker": "TSLA",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("cfg-tp-01", "TSLA", 7, 4.0, "Good", [])
    db.insert_position("TSLA", 100.0, 5.0, 4.0, "2026-04-01", sid, "Test")
    reduced = p.enforce_take_profits()  # no explicit pct — must read from injected config
    assert "TSLA" in reduced
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_portfolio.py::test_portfolio_reads_max_positions_from_config tests/test_portfolio.py::test_portfolio_reads_stop_loss_from_config tests/test_portfolio.py::test_portfolio_reads_take_profit_from_config -v
```

Expected: 3 FAILED (TypeError: `Portfolio.__init__()` got unexpected keyword argument `risk_cfg`)

- [ ] **Step 3: Replace `bot/portfolio.py`**

```python
from datetime import date
import bot.db as db

# Kept for any external code that imports these constants directly.
MAX_POSITIONS = 20
MAX_POSITIONS_PER_DAY = 3
MAX_POSITION_PCT = 8.0


class Portfolio:
    def __init__(self, broker, risk_cfg=None):
        self.broker = broker
        if risk_cfg is None:
            from system.config import settings
            risk_cfg = settings.risk
        self._risk = risk_cfg
        self._opened_today = 0

    def get_cash(self) -> float:
        return self.broker.get_cash()

    def can_open_new_position(self) -> bool:
        if len(self.broker.get_positions()) >= self._risk.max_positions:
            return False
        if self._opened_today >= self._risk.max_positions_per_day:
            return False
        return True

    def reset_daily_counter(self) -> None:
        self._opened_today = 0

    def open_position(self, ticker: str, position_pct: float, signal_id: int | None,
                      rationale: str, entry_price: float,
                      signal_source: str = "congressional") -> None:
        position_pct = min(position_pct, self._risk.max_position_pct)
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

    def enforce_stop_losses(self, stop_loss_pct: float | None = None) -> list[str]:
        pct = stop_loss_pct if stop_loss_pct is not None else self._risk.trailing_stop_pct
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            current = pos["current_price"]
            meta = open_positions.get(ticker, {})
            peak = meta.get("peak_price") or pos["avg_entry_price"]

            db.update_position_peak(ticker, current)

            drop_from_peak = (peak - current) / peak * 100
            if drop_from_peak >= pct:
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

    def enforce_take_profits(self, take_profit_pct: float | None = None) -> list[str]:
        pct = take_profit_pct if take_profit_pct is not None else self._risk.take_profit_pct
        reduced = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            if ticker in reduced:
                continue
            entry = pos["avg_entry_price"]
            current = pos["current_price"]
            gain_pct = (current - entry) / entry * 100

            if gain_pct >= pct:
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

- [ ] **Step 4: Update `orchestration/main_loop.py` — Portfolio construction**

Find the exact line (in `initialize()`):
```python
        self._portfolio = Portfolio(broker=broker)
```
Replace with:
```python
        self._portfolio = Portfolio(broker=broker, risk_cfg=self._cfg.risk)
```

- [ ] **Step 5: Run the new tests to verify they pass**

```bash
cd "trading bot" && python -m pytest tests/test_portfolio.py -v
```

Expected: all green

- [ ] **Step 6: Run full suite for regressions**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
cd "trading bot" && git add bot/portfolio.py orchestration/main_loop.py tests/test_portfolio.py && git commit -m "$(cat <<'EOF'
fix: wire RiskConfig into Portfolio — eliminate hardcoded constants

Portfolio now reads max_positions, max_positions_per_day,
max_position_pct, trailing_stop_pct, take_profit_pct from the
injected RiskConfig (defaults to singleton settings.risk). Removes
silent mismatch where RiskConfig edits had no effect on Portfolio.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Schedule HMM Rolling Refit

**Context:** `HMMRegimeEngine.rolling_refit()` exists and works, but `RegimeAwareOrchestrator` never calls it. The model is fitted once at startup and stales indefinitely. Fix: add a `refit_interval_days` config field and a `_maybe_rolling_refit()` method that fires at the top of each morning pipeline when the interval has elapsed.

**Files:**
- Modify: `system/config.py`
- Modify: `orchestration/main_loop.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py` (keep all existing tests; add below them):

```python
from datetime import date, timedelta


@pytest.fixture
def orch_fitted(mocker):
    """Orchestrator with a fitted engine for refit scheduling tests."""
    mocker.patch("orchestration.main_loop._NYSE.is_session", return_value=True)
    mocker.patch("orchestration.main_loop.get_regime_data", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.run_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])

    from system.config import settings
    o = RegimeAwareOrchestrator(settings)
    o._portfolio = MagicMock()
    o._risk = MagicMock()
    o._store = MagicMock()
    o._market_data = MagicMock()
    o._regime_state = None
    o._engine = MagicMock()
    o._engine.is_fitted = True
    o._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch.object(o, "_update_regime")    # prevent DB writes
    mocker.patch.object(o, "_update_dashboard") # prevent file writes
    return o


def test_rolling_refit_triggered_when_interval_exceeded(orch_fitted):
    orch_fitted._last_refit_date = date.today() - timedelta(days=31)
    orch_fitted.run_morning_pipeline()
    orch_fitted._engine.rolling_refit.assert_called_once()


def test_rolling_refit_not_triggered_when_recent(orch_fitted):
    orch_fitted._last_refit_date = date.today() - timedelta(days=5)
    orch_fitted.run_morning_pipeline()
    orch_fitted._engine.rolling_refit.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_orchestrator.py::test_rolling_refit_triggered_when_interval_exceeded tests/test_orchestrator.py::test_rolling_refit_not_triggered_when_recent -v
```

Expected: 2 FAILED (AttributeError: `RegimeAwareOrchestrator` has no `_last_refit_date`)

- [ ] **Step 3: Add `refit_interval_days` to `system/config.py`**

In `RegimeConfig`, add one field after `model_path`:
```python
    refit_interval_days: int = 30    # refit HMM every N days; 0 = disabled
```

- [ ] **Step 4: Add `_last_refit_date` to `RegimeAwareOrchestrator.__init__`**

In `orchestration/main_loop.py`, inside `__init__`, add this line after `self._market_data = None`:
```python
        self._last_refit_date: date | None = None
```

- [ ] **Step 5: Add `_maybe_rolling_refit()` method to `RegimeAwareOrchestrator`**

Insert this method in `orchestration/main_loop.py` immediately after `_fit_model()`:

```python
    def _maybe_rolling_refit(self) -> None:
        """Refit the HMM on recent market data if the refit interval has elapsed.

        Called at the top of each morning pipeline. A failed refit leaves the
        existing model in place and emits an alert — it never crashes the loop.
        """
        interval = self._cfg.regime.refit_interval_days
        if interval <= 0 or not self._engine.is_fitted:
            return
        today = date.today()
        if self._last_refit_date is not None:
            if (today - self._last_refit_date).days < interval:
                return
        prev_label = self._regime_state.regime_label if self._regime_state else "unknown"
        try:
            emit_event(log, EventType.MODEL_FIT,
                       f"Rolling refit triggered (last={self._last_refit_date}, "
                       f"interval={interval}d, prev_regime={prev_label})")
            self._engine.rolling_refit(
                self._market_data,
                feature_cfg=self._feature_cfg,
            )
            self._last_refit_date = today
            self._update_regime()
            new_label = self._regime_state.regime_label if self._regime_state else "unknown"
            emit_event(log, EventType.MODEL_FIT,
                       f"Rolling refit complete: {prev_label} → {new_label}")
        except Exception as exc:
            emit_event(log, EventType.MODEL_FIT_FAILED,
                       f"Rolling refit failed: {exc}", alert=True)
```

- [ ] **Step 6: Call `_maybe_rolling_refit()` at the top of `run_morning_pipeline()`**

In `run_morning_pipeline()`, after the market-closed guard and before `self._update_market_data()`:

Find:
```python
        self._update_market_data()
        self._update_regime()
```

Replace with:
```python
        self._maybe_rolling_refit()
        self._update_market_data()
        self._update_regime()
```

- [ ] **Step 7: Run failing tests to verify they now pass**

```bash
cd "trading bot" && python -m pytest tests/test_orchestrator.py -v
```

Expected: all green

- [ ] **Step 8: Run full suite for regressions**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 9: Commit**

```bash
cd "trading bot" && git add system/config.py orchestration/main_loop.py tests/test_orchestrator.py && git commit -m "$(cat <<'EOF'
feat: schedule periodic HMM rolling refit in morning pipeline

Adds refit_interval_days (default 30) to RegimeConfig. Each morning
_maybe_rolling_refit() triggers rolling_refit() when the interval has
elapsed. Failed refits log a CRITICAL alert and leave the existing
model in place — they never crash the loop. Pre/post regime label
is logged for auditability.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Earnings / Event Calendar

**Context:** The bot can open a position the day before earnings or an FOMC announcement. Both are predictable events where the information advantage from congressional disclosures is overwhelmed by event risk. Fix: a small utility that checks whether a known event falls within a configurable window and gates both signal paths.

**Files:**
- Create: `utils/__init__.py`
- Create: `utils/event_calendar.py`
- Modify: `system/config.py`
- Modify: `orchestration/main_loop.py`
- Create: `tests/test_event_calendar.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_event_calendar.py`:

```python
"""Tests for earnings/FOMC event calendar exclusion window."""
from datetime import date
from unittest.mock import MagicMock
import pytest


def test_fomc_date_within_window_blocks(mocker):
    from utils.event_calendar import has_upcoming_event
    # May 7 2026 is a scheduled FOMC announcement; today = May 5 → 2 days away
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=MagicMock(calendar={}))
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 5, 5))
    assert result is True
    assert "FOMC" in reason


def test_fomc_date_outside_window_passes(mocker):
    from utils.event_calendar import has_upcoming_event
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=MagicMock(calendar={}))
    # April 1 — no FOMC within 2 days
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 1))
    assert result is False
    assert reason == ""


def test_earnings_within_window_blocks(mocker):
    from utils.event_calendar import has_upcoming_event
    mock_ticker = MagicMock()
    mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 10)]}
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=mock_ticker)
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 9))
    assert result is True
    assert "earnings" in reason.lower()


def test_earnings_outside_window_passes(mocker):
    from utils.event_calendar import has_upcoming_event
    mock_ticker = MagicMock()
    mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 20)]}
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=mock_ticker)
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 9))
    assert result is False


def test_yfinance_failure_does_not_raise(mocker):
    from utils.event_calendar import has_upcoming_event
    mocker.patch("utils.event_calendar.yf.Ticker", side_effect=Exception("network"))
    # yfinance failure skips earnings check silently; FOMC check still runs
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 1))
    assert isinstance(result, bool)


def test_process_signal_skips_on_upcoming_event(mocker):
    """_process_signal must return early without calling score_entry_with_debate."""
    from unittest.mock import MagicMock
    from orchestration.main_loop import RegimeAwareOrchestrator
    from system.config import settings

    mocker.patch("orchestration.main_loop.get_committees_for_politician",
                 return_value=["House Energy"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=5)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)  # avoids DB hit
    mocker.patch("orchestration.main_loop.has_upcoming_event",
                 return_value=(True, "FOMC 2026-05-07"))
    score_spy = mocker.patch("orchestration.main_loop.score_entry_with_debate")

    o = RegimeAwareOrchestrator(settings)
    o._broker = MagicMock()
    o._broker.get_cash.return_value = 100_000.0
    o._regime_state = None

    disc = {
        "id": "d1", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    o._process_signal(disc, {})
    score_spy.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_event_calendar.py -v
```

Expected: 6 FAILED (ModuleNotFoundError: No module named `utils`)

- [ ] **Step 3: Add `event_exclusion_window_days` to `system/config.py`**

In `UniverseConfig`, add after `min_trade_usd`:
```python
    event_exclusion_window_days: int = 2   # block new entries within N calendar days of earnings/FOMC
```

- [ ] **Step 4: Create `utils/__init__.py`**

Create an empty file at `utils/__init__.py` in the `trading bot/` directory.

- [ ] **Step 5: Create `utils/event_calendar.py`**

```python
"""Earnings and FOMC event exclusion window.

Prevents the bot from opening positions when a known market-moving
event falls within a configurable window of calendar days.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import yfinance as yf

log = logging.getLogger(__name__)

# Official FOMC announcement dates (second day of each two-day meeting).
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
# Update this list in January each year when the Fed publishes the new schedule.
_FOMC_DATES_2026: list[date] = [
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 5, 7),
    date(2026, 6, 18),
    date(2026, 7, 29),
    date(2026, 9, 17),
    date(2026, 10, 29),
    date(2026, 12, 10),
]

_FOMC_DATES: list[date] = _FOMC_DATES_2026  # extend here for future years


def _get_next_earnings(ticker: str) -> date | None:
    """Return the next scheduled earnings date for ticker via yfinance, or None."""
    try:
        cal: Any = yf.Ticker(ticker).calendar
        if not cal:
            return None
        dates = cal.get("Earnings Date", [])
        if not dates:
            return None
        raw = dates[0]
        if hasattr(raw, "date"):
            return raw.date()
        if isinstance(raw, date):
            return raw
        return None
    except Exception as exc:
        log.debug("Could not fetch earnings date for %s: %s", ticker, exc)
        return None


def has_upcoming_event(
    ticker: str,
    window_days: int = 2,
    today: date | None = None,
) -> tuple[bool, str]:
    """Return (True, reason) if an earnings or FOMC event is within window_days.

    Parameters
    ----------
    ticker        : ticker to check for upcoming earnings
    window_days   : calendar days; event on today through today+window_days is a block
    today         : override today's date (for testing)

    Returns
    -------
    (True, "FOMC 2026-05-07")         — FOMC within window
    (True, "earnings 2026-05-09")     — earnings within window
    (False, "")                        — no upcoming event
    """
    _today = today or date.today()

    for fomc_date in _FOMC_DATES:
        days_until = (fomc_date - _today).days
        if 0 <= days_until <= window_days:
            return True, f"FOMC {fomc_date.isoformat()}"

    earnings_date = _get_next_earnings(ticker)
    if earnings_date is not None:
        days_until = (earnings_date - _today).days
        if 0 <= days_until <= window_days:
            return True, f"earnings {earnings_date.isoformat()}"

    return False, ""
```

- [ ] **Step 6: Wire event calendar into `orchestration/main_loop.py`**

**6a.** Add the import at the top of the imports block (after the existing local imports):
```python
from utils.event_calendar import has_upcoming_event
```

**6b.** In `_process_signal()`, insert the event check BEFORE `research = gather_research(ticker)` (saves the research API call when blocked). The insertion point is after `cluster_count = get_cluster_count(...)`:

Find:
```python
        research = gather_research(ticker)

        # AI entry scoring (unchanged from existing bot)
        score: EntryScore = score_entry_with_debate(
```

Replace with:
```python
        # Skip before expensive research call if an event is imminent
        has_event, event_reason = has_upcoming_event(
            ticker, window_days=self._cfg.universe.event_exclusion_window_days
        )
        if has_event:
            log.info("Skipping %s: upcoming event — %s", ticker, event_reason)
            return

        research = gather_research(ticker)

        # AI entry scoring (unchanged from existing bot)
        score: EntryScore = score_entry_with_debate(
```

**6c.** In `_process_fundamental_candidate()`, add the same check after `sector = get_sector_for_ticker(ticker)` and before `score: EntryScore = score_entry_with_debate(`:

Find:
```python
        sector = get_sector_for_ticker(ticker)

        score: EntryScore = score_entry_with_debate(
```

Replace with:
```python
        sector = get_sector_for_ticker(ticker)

        has_event, event_reason = has_upcoming_event(
            ticker, window_days=self._cfg.universe.event_exclusion_window_days
        )
        if has_event:
            log.info("Skipping %s (%s): upcoming event — %s",
                     ticker, signal_type, event_reason)
            return False

        score: EntryScore = score_entry_with_debate(
```

- [ ] **Step 7: Run failing tests to verify they now pass**

```bash
cd "trading bot" && python -m pytest tests/test_event_calendar.py -v
```

Expected: all green

- [ ] **Step 8: Run full suite for regressions**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 9: Commit**

```bash
cd "trading bot" && git add utils/__init__.py utils/event_calendar.py system/config.py orchestration/main_loop.py tests/test_event_calendar.py && git commit -m "$(cat <<'EOF'
feat: add earnings/FOMC event calendar to signal pipeline

New utils/event_calendar.py checks yfinance earnings dates and a
static 2026 FOMC schedule. Both _process_signal and
_process_fundamental_candidate skip entries when an event falls
within event_exclusion_window_days (default 2) calendar days. The
check fires before research gathering to avoid unnecessary API calls.
yfinance failures are silent — the earnings check is skipped, FOMC
check always runs.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Real Alert Sender

**Context:** `monitoring/alerts.py` calls `requests.post` when `ALERT_WEBHOOK_URL` is set but has no abstraction — it is a single function with inline HTTP logic. There is no way to inject a test double, no interface for alternative channels, and config lives in an env-var at module load time. Fix: add an `AlertSender` ABC, implement `WebhookAlertSender` and `LogAlertSender`, move config to `MonitoringConfig`, and add a `--test-alerts` flag to `run_bot.py`.

**Files:**
- Modify: `system/config.py`
- Modify: `monitoring/alerts.py`
- Modify: `run_bot.py`
- Create: `tests/test_alerts.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alerts.py`:

```python
"""Tests for the AlertSender abstraction and fire_alert() dispatch."""
import json
import logging
import pytest
from unittest.mock import MagicMock


def test_webhook_sender_posts_correct_payload(mocker):
    from monitoring.alerts import WebhookAlertSender
    mock_post = mocker.patch("monitoring.alerts.requests.post")
    sender = WebhookAlertSender(url="https://hooks.example.com/test")
    sender.send("circuit_breaker", "Daily loss exceeded", {"loss_pct": 4.1})
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]
    assert call_kwargs["url"] == "https://hooks.example.com/test"
    payload = call_kwargs["json"]
    assert payload["event"] == "circuit_breaker"
    assert "Daily loss exceeded" in payload["text"]


def test_webhook_sender_handles_network_error_silently(mocker):
    from monitoring.alerts import WebhookAlertSender
    import requests as req
    mocker.patch("monitoring.alerts.requests.post",
                 side_effect=req.exceptions.Timeout)
    sender = WebhookAlertSender(url="https://hooks.example.com/test")
    sender.send("lockout_created", "Lockout triggered", {})  # must not raise


def test_log_sender_emits_warning(caplog):
    from monitoring.alerts import LogAlertSender
    sender = LogAlertSender()
    with caplog.at_level(logging.WARNING, logger="monitoring.alerts"):
        sender.send("circuit_breaker", "Test warning", {"x": 1})
    assert any("circuit_breaker" in r.message for r in caplog.records)


def test_fire_alert_delegates_to_sender(mocker):
    from monitoring import alerts
    mock_sender = MagicMock()
    mocker.patch("monitoring.alerts._get_sender", return_value=mock_sender)
    alerts.fire_alert("startup", "Hello", {"k": "v"})
    mock_sender.send.assert_called_once_with("startup", "Hello", {"k": "v"})


def test_fire_alert_uses_log_sender_when_no_url_configured(mocker):
    from monitoring import alerts
    # Reset cache so _build_sender is called fresh
    alerts._sender_cache[0] = None
    mocker.patch("monitoring.alerts._build_sender",
                 return_value=alerts.LogAlertSender())
    post_spy = mocker.patch("monitoring.alerts.requests.post")
    alerts.fire_alert("startup", "No webhook", {})
    post_spy.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python -m pytest tests/test_alerts.py -v
```

Expected: 5 FAILED (ImportError: cannot import name `WebhookAlertSender`)

- [ ] **Step 3: Add `MonitoringConfig` to `system/config.py`**

**3a.** Add this dataclass BEFORE `DashboardConfig`:
```python
@dataclass(frozen=True)
class MonitoringConfig:
    alert_webhook_url: str = field(
        default_factory=lambda: _env("ALERT_WEBHOOK_URL", "")
    )
```

**3b.** Add to `Settings` (after `dashboard`):
```python
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
```

- [ ] **Step 4: Replace `monitoring/alerts.py`**

```python
"""Alert sender abstraction.

WebhookAlertSender — POST JSON to a Slack- or Discord-compatible webhook URL.
LogAlertSender     — fallback: emit a WARNING to the standard log.

The active sender is built lazily on first use from settings.monitoring.alert_webhook_url
(or the ALERT_WEBHOOK_URL environment variable). Set the env var or config field to enable
real webhook delivery; leave it empty to keep log-only alerts.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, UTC
from typing import Any

import requests

log = logging.getLogger(__name__)

_HIGH_PRIORITY = {
    "lockout_created",
    "circuit_breaker",
    "model_fit_failed",
    "drawdown_threshold",
}

# Mutable single-element list so tests can reset and patch it easily.
_sender_cache: list[AlertSender | None] = [None]


class AlertSender(ABC):
    @abstractmethod
    def send(self, event: str, message: str, data: dict[str, Any]) -> None: ...


class WebhookAlertSender(AlertSender):
    """POST a JSON payload compatible with Slack/Discord incoming webhooks."""

    def __init__(self, url: str, timeout: int = 5) -> None:
        self._url = url
        self._timeout = timeout

    def send(self, event: str, message: str, data: dict[str, Any]) -> None:
        payload = {
            "text": f"[{event.upper()}] {message}",
            "event": event,
            "ts": datetime.now(UTC).isoformat(),
            "data": data,
        }
        try:
            requests.post(url=self._url, json=payload, timeout=self._timeout)
        except Exception as exc:
            log.warning("Alert webhook delivery failed (%s): %s", event, exc)


class LogAlertSender(AlertSender):
    """Fallback sender — writes to the standard log as WARNING."""

    def send(self, event: str, message: str, data: dict[str, Any]) -> None:
        log.warning("[ALERT] %s | %s | %s", event, message, json.dumps(data))


def _build_sender() -> AlertSender:
    try:
        from system.config import settings
        url = settings.monitoring.alert_webhook_url
    except Exception:
        url = ""
    return WebhookAlertSender(url) if url else LogAlertSender()


def _get_sender() -> AlertSender:
    if _sender_cache[0] is None:
        _sender_cache[0] = _build_sender()
    return _sender_cache[0]


def fire_alert(event: str, message: str, data: dict[str, Any]) -> None:
    """Route an alert to the configured sender (webhook or log)."""
    level = logging.WARNING if event in _HIGH_PRIORITY else logging.INFO
    log.log(level, "[ALERT] %s | %s", event, message)
    _get_sender().send(event, message, data)
```

- [ ] **Step 5: Add `--test-alerts` flag to `run_bot.py`**

**5a.** Add argument after `--backtest` in `main()`:
```python
    parser.add_argument("--test-alerts", action="store_true",
                        help="Fire a test alert to verify the configured sender, then exit")
```

**5b.** Add handler before `if args.backtest:` in `main()`:
```python
    if args.test_alerts:
        from monitoring.alerts import fire_alert
        fire_alert(
            "startup",
            "Test alert — trading bot alert pipeline is configured correctly",
            {"test": True},
        )
        print("Test alert fired. Check your webhook or log output.")
        return
```

- [ ] **Step 6: Run failing tests to verify they now pass**

```bash
cd "trading bot" && python -m pytest tests/test_alerts.py -v
```

Expected: all green

- [ ] **Step 7: Run full suite for regressions**

```bash
cd "trading bot" && python -m pytest tests/ -v
```

Expected: all green

- [ ] **Step 8: Commit**

```bash
cd "trading bot" && git add monitoring/alerts.py system/config.py run_bot.py tests/test_alerts.py && git commit -m "$(cat <<'EOF'
feat: replace alert stub with real AlertSender abstraction

Adds AlertSender ABC with WebhookAlertSender (Slack/Discord-compatible
POST) and LogAlertSender fallback. Active sender is chosen lazily from
settings.monitoring.alert_webhook_url (or ALERT_WEBHOOK_URL env var).
Adds MonitoringConfig to Settings. run_bot.py --test-alerts fires a
startup test event and exits, enabling quick config verification.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final Check

- [ ] Full suite one last time:

```bash
cd "trading bot" && python -m pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: all tests green.

- [ ] Verify `--test-alerts` works end-to-end (log output at minimum):

```bash
cd "trading bot" && python run_bot.py --test-alerts
```

Expected output: `Test alert fired. Check your webhook or log output.`

To test a real webhook, set `ALERT_WEBHOOK_URL` first:
```bash
cd "trading bot" && ALERT_WEBHOOK_URL="https://hooks.slack.com/..." python run_bot.py --test-alerts
```
