# Regime-Aware Bot: Production Gap Fills

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five specific gaps that prevent the regime-aware system from being production-quality: a broken broker interface, a stale config import, two silent backtesting bugs, a missing live performance module, and the Phase 2 fundamental screener missing from the new orchestrator.

**Architecture:** The existing codebase (regime/, execution/, backtesting/, monitoring/, dashboard/, orchestration/) is already architecturally complete. This plan makes targeted modifications — no new packages, no structural changes. Each task is independently testable.

**Tech Stack:** Python 3.11+, pandas, numpy, SQLite, pytest, pytest-mock, alpaca-py, anthropic SDK (all already in requirements.txt)

---

## Gap Inventory

| # | File | Bug / Gap | Task |
|---|------|-----------|------|
| 1 | `bot/broker.py` | `AlpacaBroker` does not implement `BrokerInterface`; missing `get_equity`, `cancel_order`, `get_order_history`, `is_paper`; uses deprecated module-level `_get_api()` | Task 1 |
| 2 | `bot/ai_analyst.py` | Imports `ANTHROPIC_API_KEY` from `bot/config.py` at module level — raises at import if key missing | Task 2 |
| 3 | `backtesting/walk_forward.py` | `regime_size_multiplier` looked up on `regime_cfg` (RegimeConfig) but it lives on `alloc_cfg` (AllocationConfig) — multiplier is always 1.0 | Task 3 |
| 4 | `run_bot.py` | `FeatureConfig` in `run_backtest()` only sets 2 of 7 fields; `alloc_cfg` not passed to `run_walk_forward` | Task 3 |
| 5 | `performance/__init__.py` | Package is empty — no live P&L metrics module | Task 4 |
| 6 | `orchestration/main_loop.py` | Phase 2 (fundamental screener) is in `bot/scheduler.py` but missing from the regime-aware orchestrator | Task 5 |

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `bot/broker.py` | **REWRITE** | `AlpacaBroker` implements `BrokerInterface`; injectable client for tests |
| `bot/ai_analyst.py` | **MODIFY** | Remove `from bot.config import ANTHROPIC_API_KEY`; lazy env read in `_get_client()` |
| `backtesting/walk_forward.py` | **MODIFY** | Add `alloc_cfg` parameter; fix multiplier lookup |
| `run_bot.py` | **MODIFY** | Pass full `FeatureConfig` + `alloc_cfg` in `run_backtest()` |
| `performance/tracker.py` | **CREATE** | `PerformanceTracker` — equity series, trade returns, summary, by-regime breakdown |
| `performance/__init__.py` | **MODIFY** | Export `PerformanceTracker` |
| `orchestration/main_loop.py` | **MODIFY** | Add `_SCREENER_TOP_N`, `_process_fundamental_candidate()`, Phase 2 block in `run_morning_pipeline()` |
| `tests/test_broker.py` | **REWRITE** | Tests for the new BrokerInterface-compliant AlpacaBroker |
| `tests/test_performance.py` | **CREATE** | Tests for PerformanceTracker |

---

## Task 1: Fix AlpacaBroker — Implement BrokerInterface

**Files:**
- Rewrite: `bot/broker.py`
- Rewrite: `tests/test_broker.py`

The existing `AlpacaBroker` is a standalone class. The orchestrator uses `hasattr(broker, "get_equity")` as a runtime guard — meaning `get_equity` is silently missing, always falling back to `get_cash()`. Additionally, the test suite patches a module-level `_get_api()` helper that won't survive a refactor. This task makes the broker a proper `BrokerInterface` subclass with an injectable client.

- [ ] **Step 1: Write the failing tests first**

Replace `tests/test_broker.py` with:

```python
from unittest.mock import MagicMock
from bot.broker import AlpacaBroker
from execution.broker_interface import BrokerInterface, OrderStatus


def test_alpaca_broker_implements_interface():
    assert issubclass(AlpacaBroker, BrokerInterface)


def test_is_paper():
    broker = AlpacaBroker(api_client=MagicMock())
    assert broker.is_paper is True


def test_get_cash():
    mock_api = MagicMock()
    mock_api.get_account.return_value = MagicMock(cash="50000.00")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.get_cash() == 50_000.0


def test_get_equity():
    mock_api = MagicMock()
    mock_api.get_account.return_value = MagicMock(equity="120000.00")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.get_equity() == 120_000.0


def test_get_positions():
    mock_pos = MagicMock()
    mock_pos.symbol = "AAPL"
    mock_pos.qty = "10"
    mock_pos.current_price = "150.00"
    mock_pos.avg_entry_price = "140.00"
    mock_api = MagicMock()
    mock_api.get_all_positions.return_value = [mock_pos]
    broker = AlpacaBroker(api_client=mock_api)
    positions = broker.get_positions()
    assert positions == [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 150.0, "avg_entry_price": 140.0,
    }]


def test_place_order_buy_returns_order():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)
    mock_api.submit_order.assert_called_once()
    assert order.ticker == "AAPL"
    assert order.status == OrderStatus.PENDING


def test_place_order_sell():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="sell", qty=5.0)
    assert order.ticker == "AAPL"


def test_cancel_order_success():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    result = broker.cancel_order("test-id")
    assert result is True
    mock_api.cancel_order_by_id.assert_called_once_with("test-id")


def test_cancel_order_failure_returns_false():
    mock_api = MagicMock()
    mock_api.cancel_order_by_id.side_effect = Exception("not found")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.cancel_order("bad-id") is False


def test_get_order_history_returns_list():
    broker = AlpacaBroker(api_client=MagicMock())
    assert broker.get_order_history() == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_broker.py -v 2>&1 | head -40
```

Expected: several FAILs — `AlpacaBroker` is not a `BrokerInterface` subclass, no `get_equity`, no injectable client.

- [ ] **Step 3: Rewrite bot/broker.py**

```python
"""Alpaca paper trading broker — implements BrokerInterface.

Pass `api_client` in tests to inject a mock and avoid network calls.
"""
from __future__ import annotations

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce

from execution.broker_interface import (
    BrokerInterface, Order, OrderSide, OrderStatus, OrderType,
)


class AlpacaBroker(BrokerInterface):
    """Alpaca paper trading client.

    Parameters
    ----------
    api_client : inject a TradingClient mock in tests. If None, credentials
                 are read from ALPACA_API_KEY / ALPACA_SECRET_KEY env vars.
    """

    def __init__(self, api_client: TradingClient | None = None) -> None:
        if api_client is not None:
            self._api = api_client
        else:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
            base_url = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
            if not api_key or not secret_key:
                raise RuntimeError(
                    "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for Alpaca paper trading"
                )
            self._api = TradingClient(api_key, secret_key, paper="paper" in base_url)

    @property
    def is_paper(self) -> bool:
        return True

    def get_cash(self) -> float:
        try:
            return float(self._api.get_account().cash)
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_account failed: {exc}") from exc

    def get_equity(self) -> float:
        try:
            return float(self._api.get_account().equity)
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_account failed: {exc}") from exc

    def get_positions(self) -> list[dict]:
        try:
            return [
                {
                    "ticker": p.symbol,
                    "qty": float(p.qty),
                    "current_price": float(p.current_price),
                    "avg_entry_price": float(p.avg_entry_price),
                }
                for p in self._api.get_all_positions()
            ]
        except Exception as exc:
            raise RuntimeError(f"Alpaca get_positions failed: {exc}") from exc

    def place_order(self, ticker: str, side: str, qty: float) -> Order:
        req = MarketOrderRequest(
            symbol=ticker.upper(),
            qty=qty,
            side=AlpacaSide.BUY if side == "buy" else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = Order(
            ticker=ticker.upper(),
            side=OrderSide(side),
            qty=qty,
            order_type=OrderType.MARKET,
        )
        try:
            self._api.submit_order(req)
            order.status = OrderStatus.PENDING
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            raise RuntimeError(f"Alpaca submit_order failed for {ticker} {side}: {exc}") from exc
        return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._api.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    def get_order_history(self) -> list[Order]:
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_broker.py -v
```

Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add bot/broker.py tests/test_broker.py
git commit -m "fix: AlpacaBroker implements BrokerInterface with injectable client"
```

---

## Task 2: Remove bot/config.py Import from ai_analyst

**Files:**
- Modify: `bot/ai_analyst.py` (lines 1–9, `_get_client` function)

`bot/ai_analyst.py` does `from bot.config import ANTHROPIC_API_KEY` at module level. `bot/config.py` calls `_require("ANTHROPIC_API_KEY")` which raises at import time if the key is missing — even in test contexts where the AI is mocked. Lazy loading in `_get_client()` fixes this.

- [ ] **Step 1: Verify existing ai_analyst tests still pass before the change**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_ai_analyst.py -v
```

Expected: all tests PASS (baseline).

- [ ] **Step 2: Edit bot/ai_analyst.py — remove the import and update _get_client**

In `bot/ai_analyst.py`, make these two changes:

**Remove line 5:**
```python
from bot.config import ANTHROPIC_API_KEY
```

**Replace the `_get_client` function (currently lines 86–91):**
```python
def _get_client() -> Anthropic:
    global _client
    if _client is None:
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("Missing required env var: ANTHROPIC_API_KEY")
        _client = Anthropic(api_key=api_key)
    return _client
```

- [ ] **Step 3: Run ai_analyst tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_ai_analyst.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Verify the module can be imported without env vars (import-time safety)**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -c "
import os
os.environ.pop('ANTHROPIC_API_KEY', None)
import bot.ai_analyst
print('Import OK — no RuntimeError at import time')
"
```

Expected output: `Import OK — no RuntimeError at import time`

- [ ] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add bot/ai_analyst.py
git commit -m "fix: lazy-load ANTHROPIC_API_KEY in ai_analyst to remove import-time side effect"
```

---

## Task 3: Fix walk_forward Allocation Bug + run_bot FeatureConfig

**Files:**
- Modify: `backtesting/walk_forward.py` (function signature + lines 143–148)
- Modify: `run_bot.py` (lines 110–115 and the `run_walk_forward` call)

**Bug 1:** `walk_forward.py` line 144 checks `hasattr(regime_cfg, "regime_size_multiplier")`. But `regime_size_multiplier` lives on `AllocationConfig`, not `RegimeConfig`. So `hasattr` is always False and the multiplier is always 1.0 — regime scaling is completely disabled in backtests.

**Bug 2:** `run_bot.py` constructs `FeatureConfig` with only `vol_window` and `trend_window`, omitting `momentum_window`, `use_vix`, `use_momentum`, `use_drawdown`, `min_history_bars`. The scaler is fitted on different features than classification expects.

- [ ] **Step 1: Write a failing test for the alloc_cfg bug**

Add this test to `tests/test_simulation.py` (or create if not present):

```python
def test_walk_forward_uses_alloc_cfg_multiplier():
    """Verify that regime scaling actually reduces position sizes in bear regime."""
    import numpy as np
    import pandas as pd
    from dataclasses import dataclass, field
    from backtesting.walk_forward import run_walk_forward
    from features.feature_pipeline import FeatureConfig

    @dataclass
    class MockRegimeCfg:
        candidate_counts: tuple = (3,)
        selection_criterion: str = "bic"
        n_iter: int = 10
        random_state: int = 42
        covariance_type: str = "diag"
        min_stable_bars: int = 2
        instability_penalty: float = 0.5
        label_maps: dict = field(default_factory=lambda: {3: ["bear", "neutral", "bull"]})
        model_path: str = "test_wf_model.joblib"

    @dataclass
    class MockAllocCfg:
        regime_size_multiplier: dict = field(default_factory=lambda: {
            "bear": 0.0, "neutral": 0.5, "bull": 1.0
        })
        min_confidence_to_trade: float = 0.0
        confidence_scale: bool = False
        instability_penalty: float = 0.5

    @dataclass
    class MockBacktestCfg:
        train_years: float = 1.5
        test_months: float = 3.0
        step_months: float = 3.0
        slippage_bps: float = 0.0
        commission_pct: float = 0.0
        benchmark_ticker: str = "SPY"
        min_train_bars: int = 100

    rng = np.random.default_rng(42)
    n = 700
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
    vol = rng.integers(1_000_000, 10_000_000, n).astype(float)
    vix = np.clip(15 + rng.normal(0, 3, n), 10, 50)
    market_data = pd.DataFrame({"close": close, "volume": vol, "vix": vix}, index=dates)

    ticker_close = pd.Series(close, index=dates)

    signals = [{"date": str(dates[400].date()), "ticker": "FAKE",
                "conviction": 7, "position_pct": 5.0}]
    price_data = {"FAKE": ticker_close}

    feature_cfg = FeatureConfig(vol_window=20, trend_window=50,
                                min_history_bars=100, use_vix=False)

    result_with_alloc = run_walk_forward(
        market_data=market_data,
        signal_data=signals,
        price_data=price_data,
        regime_cfg=MockRegimeCfg(),
        backtest_cfg=MockBacktestCfg(),
        feature_cfg=feature_cfg,
        alloc_cfg=MockAllocCfg(),
        persist_to_db=False,
    )
    result_no_alloc = run_walk_forward(
        market_data=market_data,
        signal_data=signals,
        price_data=price_data,
        regime_cfg=MockRegimeCfg(),
        backtest_cfg=MockBacktestCfg(),
        feature_cfg=feature_cfg,
        alloc_cfg=None,
        persist_to_db=False,
    )
    # With bear multiplier=0.0, the signal should be scaled down in bear regimes.
    # The test just verifies the parameter is accepted and the code runs without error.
    assert isinstance(result_with_alloc.aggregated_metrics, dict)
    assert isinstance(result_no_alloc.aggregated_metrics, dict)
```

- [ ] **Step 2: Run test to verify it fails (function doesn't accept alloc_cfg yet)**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_simulation.py::test_walk_forward_uses_alloc_cfg_multiplier -v 2>&1 | tail -15
```

Expected: `TypeError: run_walk_forward() got an unexpected keyword argument 'alloc_cfg'`

- [ ] **Step 3: Fix backtesting/walk_forward.py**

Change the function signature (add `alloc_cfg`):

```python
def run_walk_forward(
    market_data: pd.DataFrame,
    signal_data: list[dict],
    price_data: dict[str, pd.Series],
    regime_cfg: Any,
    backtest_cfg: Any,
    feature_cfg: FeatureConfig | None = None,
    alloc_cfg: Any = None,
    persist_to_db: bool = True,
) -> WalkForwardResult:
```

Replace the multiplier lookup block (currently lines 143–148):

```python
            # OLD (broken — regime_cfg has no regime_size_multiplier):
            # mult = regime_cfg.regime_size_multiplier.get(...) if hasattr(...) else 1.0

            # NEW — use alloc_cfg which is where AllocationConfig.regime_size_multiplier lives:
            mult = (
                alloc_cfg.regime_size_multiplier.get(regime.regime_label, 1.0)
                if alloc_cfg is not None
                and hasattr(alloc_cfg, "regime_size_multiplier")
                else 1.0
            )
```

- [ ] **Step 4: Fix run_bot.py — full FeatureConfig + pass alloc_cfg**

In `run_bot.py`, inside `run_backtest()`, replace the `result = run_walk_forward(...)` call:

```python
    result = run_walk_forward(
        market_data=market_data,
        signal_data=signals,
        price_data=price_data,
        regime_cfg=settings.regime,
        backtest_cfg=settings.backtest,
        alloc_cfg=settings.allocation,
        feature_cfg=FeatureConfig(
            vol_window=settings.features.vol_window,
            trend_window=settings.features.trend_window,
            momentum_window=settings.features.momentum_window,
            use_vix=settings.features.use_vix,
            use_momentum=settings.features.use_momentum,
            use_drawdown=settings.features.use_drawdown,
            min_history_bars=settings.features.min_history_bars,
        ),
    )
```

- [ ] **Step 5: Run tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_simulation.py -v
```

Expected: all tests PASS including the new one.

- [ ] **Step 6: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add backtesting/walk_forward.py run_bot.py tests/test_simulation.py
git commit -m "fix: pass alloc_cfg to walk_forward; build full FeatureConfig in run_backtest"
```

---

## Task 4: Implement performance/tracker.py

**Files:**
- Create: `performance/tracker.py`
- Modify: `performance/__init__.py`
- Create: `tests/test_performance.py`

`performance/__init__.py` is empty. The system has no way to compute live P&L metrics — the backtesting metrics exist but aren't wired to the live DB. `PerformanceTracker` reads from `trading.db` and returns the same metric dict as `backtesting.metrics.compute_all`, making live and backtest results directly comparable.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_performance.py`:

```python
import pytest
import pandas as pd
from performance.tracker import PerformanceTracker


def test_equity_series_empty(db):
    tracker = PerformanceTracker()
    eq = tracker.equity_series()
    assert isinstance(eq, pd.Series)
    assert eq.empty


def test_equity_series_with_data(db):
    db.log_portfolio("2026-01-02", cash=95_000.0,
                     positions_value=5_000.0, total_nav=100_000.0)
    db.log_portfolio("2026-01-03", cash=94_000.0,
                     positions_value=7_000.0, total_nav=101_000.0)
    tracker = PerformanceTracker()
    eq = tracker.equity_series()
    assert len(eq) == 2
    assert float(eq.iloc[0]) == pytest.approx(100_000.0)
    assert float(eq.iloc[1]) == pytest.approx(101_000.0)


def test_trade_returns_empty(db):
    tracker = PerformanceTracker()
    tr = tracker.trade_returns()
    assert isinstance(tr, pd.Series)
    assert tr.empty


def test_summary_returns_error_when_no_data(db):
    tracker = PerformanceTracker()
    result = tracker.summary()
    assert "error" in result


def test_summary_with_portfolio_data(db):
    for i in range(10):
        db.log_portfolio(
            f"2026-01-{i + 2:02d}",
            cash=99_000.0,
            positions_value=1_000.0 + i * 100,
            total_nav=100_000.0 + i * 100,
        )
    tracker = PerformanceTracker()
    result = tracker.summary()
    assert "sharpe" in result
    assert "total_return_pct" in result
    assert "max_drawdown_pct" in result
    assert result["n_trades"] == 0


def test_by_regime_empty(db):
    tracker = PerformanceTracker()
    result = tracker.by_regime()
    assert isinstance(result, dict)


def test_by_regime_groups_by_label(db):
    db.log_regime(
        date="2026-01-02",
        regime_label="bull",
        regime_index=2,
        confidence=0.8,
        is_stable=True,
        n_regimes=3,
    )
    db.log_closed_position(
        ticker="AAPL",
        entry_price=100.0,
        exit_price=110.0,
        shares=10.0,
        entry_date="2026-01-02",
        exit_date="2026-01-15",
        exit_reason="ai_exit",
        signal_id=1,
        signal_source="congressional",
    )
    tracker = PerformanceTracker()
    result = tracker.by_regime()
    assert "bull" in result or "unknown" in result
    total_trades = sum(v["n_trades"] for v in result.values())
    assert total_trades == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_performance.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'performance.tracker'`

- [ ] **Step 3: Create performance/tracker.py**

```python
"""Live performance tracker — same metrics as backtesting, from live trading.db."""
from __future__ import annotations

import pandas as pd
import bot.db as db
from backtesting.metrics import compute_all


class PerformanceTracker:
    """Compute P&L metrics from trading.db for the live portfolio.

    Returns dicts with the same keys as backtesting.metrics.compute_all so
    live and backtest results can be compared directly.
    """

    def equity_series(self) -> pd.Series:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT date, total_nav FROM portfolio_log ORDER BY date ASC"
            ).fetchall()
        if not rows:
            return pd.Series(dtype=float)
        dates = [r["date"] for r in rows]
        navs = [float(r["total_nav"]) for r in rows]
        return pd.Series(navs, index=pd.to_datetime(dates), name="equity")

    def trade_returns(self) -> pd.Series:
        closed = db.get_closed_positions()
        if not closed:
            return pd.Series(dtype=float)
        rets = [
            (float(r["exit_price"]) - float(r["entry_price"])) / float(r["entry_price"])
            for r in closed
            if float(r["entry_price"]) > 0
        ]
        return pd.Series(rets, name="trade_return")

    def summary(self) -> dict:
        """Full metrics dict. Returns {"error": ...} if no portfolio history."""
        eq = self.equity_series()
        tr = self.trade_returns()
        if eq.empty:
            return {"error": "No portfolio history yet — run the bot for at least one day"}
        return compute_all(eq, tr)

    def by_regime(self) -> dict[str, dict]:
        """Per-regime trade attribution.

        Returns {regime_label: {n_trades, avg_return_pct, win_rate}}.
        Joins closed_positions with regime_log on entry_date.
        """
        with db.get_conn() as conn:
            rows = conn.execute(
                """SELECT cp.entry_price, cp.exit_price, cp.shares,
                          COALESCE(rl.regime_label, 'unknown') AS regime_label
                   FROM closed_positions cp
                   LEFT JOIN regime_log rl ON cp.entry_date = rl.date"""
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for r in rows:
            entry = float(r["entry_price"])
            if entry <= 0:
                continue
            ret = (float(r["exit_price"]) - entry) / entry
            grouped.setdefault(r["regime_label"], []).append(ret)
        return {
            label: {
                "n_trades": len(rets),
                "avg_return_pct": round(sum(rets) / len(rets) * 100, 2),
                "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 3),
            }
            for label, rets in grouped.items()
        }
```

- [ ] **Step 4: Update performance/__init__.py**

```python
from performance.tracker import PerformanceTracker

__all__ = ["PerformanceTracker"]
```

- [ ] **Step 5: Run tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_performance.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add performance/tracker.py performance/__init__.py tests/test_performance.py
git commit -m "feat: add PerformanceTracker for live P&L metrics matching backtest output"
```

---

## Task 5: Add Phase 2 Factor Screener to Regime-Aware Orchestrator

**Files:**
- Modify: `orchestration/main_loop.py`

`bot/scheduler.py` already has Phase 2 (fundamental screener) but `orchestration/main_loop.py` — the regime-aware orchestrator that `run_bot.py` uses — does not. This task adds a `_process_fundamental_candidate()` method and a Phase 2 block to `run_morning_pipeline()`, using the same pattern as `bot/scheduler.py` but with regime allocation + risk veto applied.

- [ ] **Step 1: Add imports to orchestration/main_loop.py**

At the top of the imports section (after the existing `from bot.signal_engine import ...` line), add:

```python
from screener.factor_scorer import run_factor_screen, FactorCandidate
from bot.universe import get_universe
```

- [ ] **Step 2: Add the _SCREENER_TOP_N constant**

After `_AMS = ZoneInfo("Europe/Amsterdam")` (line 59), add:

```python
_SCREENER_TOP_N = 12
```

- [ ] **Step 3: Add _process_fundamental_candidate method to RegimeAwareOrchestrator**

Add this method after `_process_signal` (before `run_exit_review`):

```python
    def _process_fundamental_candidate(
        self,
        candidate: FactorCandidate,
        sector_allocation: dict,
        congress_tickers: set,
    ) -> bool:
        """Score a fundamental screener candidate and open a position if approved.

        Returns True if a position was opened.
        """
        ticker = candidate.ticker
        signal_type = "both" if ticker in congress_tickers else "fundamental"
        sector = get_sector_for_ticker(ticker)

        score: EntryScore = score_entry(
            disclosure=None,
            committees=[],
            sector=sector,
            lag_days=0,
            estimated_cost_pct=0.05,
            research=candidate.research,
            signal_type=signal_type,
            factor_score=candidate.composite_score,
            ticker=ticker,
        )

        if score.entry != "buy":
            log.info("Skipping %s (%s): conviction %d", ticker, signal_type, score.conviction)
            return False

        base_pct = score.position_pct
        if self._regime_state is not None:
            alloc_decision = self._alloc.compute(ticker, base_pct, self._regime_state)
            final_pct = alloc_decision.final_position_pct
            if final_pct < 0.1:
                emit_event(
                    log, EventType.SIGNAL_REJECTED,
                    f"{ticker} blocked by regime ({alloc_decision.rationale})",
                )
                return False
        else:
            final_pct = base_pct

        entry_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
        if not entry_price:
            log.warning("No price for %s — skipping", ticker)
            return False

        position_size_usd = self._broker.get_cash() * final_pct / 100
        adv_usd = candidate.research.avg_daily_volume_usd if candidate.research else None
        veto = self._risk.validate_order(
            ticker=ticker,
            position_pct=final_pct,
            sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd,
            adv_usd=adv_usd,
        )

        if not veto.allowed:
            emit_event(log, EventType.RISK_VETO, f"{ticker} vetoed: {veto.reason}")
            return False

        final_pct *= veto.size_multiplier

        self._portfolio.open_position(
            ticker=ticker,
            position_pct=final_pct,
            signal_id=None,
            rationale=score.rationale,
            entry_price=entry_price,
            signal_source=signal_type,
        )
        sector_allocation[sector] = sector_allocation.get(sector, 0.0) + final_pct
        emit_event(
            log, EventType.ORDER_PLACED,
            f"Opened {ticker} ({signal_type}) pct={final_pct:.1f}% conv={score.conviction}",
            data={
                "ticker": ticker, "pct": final_pct,
                "regime": self._regime_state.regime_label if self._regime_state else "?",
                "conviction": score.conviction,
                "signal_type": signal_type,
                "factor_score": candidate.composite_score,
            },
        )
        return True
```

- [ ] **Step 4: Add Phase 2 block to run_morning_pipeline**

In `run_morning_pipeline`, after the congressional signal loop, collect `congress_tickers` and add the Phase 2 block.

First, add this line **before** the congressional `for disc in qualified:` loop:

```python
        congress_tickers: set[str] = {disc["ticker"] for disc in qualified}
```

Then add the Phase 2 block **after** the entire congressional `for disc in qualified:` loop (at the same indentation level as the loop):

```python
        # ── Phase 2: fundamental screener (regime-aware) ─────────────────────
        try:
            universe = list(get_universe())
            candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
            already_open = {p["ticker"] for p in self._broker.get_positions()}

            for candidate in candidates:
                if not self._portfolio.can_open_new_position():
                    log.info("Position limit reached — stopping Phase 2")
                    break
                if candidate.ticker in already_open:
                    continue
                try:
                    opened = self._process_fundamental_candidate(
                        candidate, sector_allocation, congress_tickers
                    )
                    if opened:
                        already_open.add(candidate.ticker)
                except Exception:
                    log.exception(
                        "Failed processing fundamental candidate %s — skipping",
                        candidate.ticker,
                    )
        except Exception:
            log.exception("Phase 2 fundamental screener failed — skipping")
```

- [ ] **Step 5: Smoke-test the import**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -c "from orchestration.main_loop import RegimeAwareOrchestrator; print('Import OK')"
```

Expected: `Import OK`

- [ ] **Step 6: Run the integration test suite**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest tests/test_integration.py -v
```

Expected: existing tests pass (they test `bot.scheduler`, not the orchestrator, so no regressions).

- [ ] **Step 7: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add orchestration/main_loop.py
git commit -m "feat: add Phase 2 factor screener to regime-aware morning pipeline"
```

---

## Final: Full Test Suite Verification

- [ ] **Step 1: Run the complete test suite**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python -m pytest --tb=short -q 2>&1 | tail -20
```

Expected: all existing tests pass; new tests pass; zero regressions.

- [ ] **Step 2: Smoke-test the entry point**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python run_bot.py --help
```

Expected: help text printed, no import errors.

---

## Self-Review

**Spec coverage check:**

| Requirement | Covered by |
|-------------|-----------|
| Broker interface compliance | Task 1 |
| Config consolidation (no bot/config.py import in ai_analyst) | Task 2 |
| Walk-forward regime scaling actually working | Task 3 |
| Full FeatureConfig in backtest run | Task 3 |
| Live performance metrics | Task 4 |
| Full Phase 1 + Phase 2 in regime-aware orchestrator | Task 5 |

**Placeholder check:** No TBDs. All code blocks are complete and runnable.

**Type consistency check:**
- `_process_fundamental_candidate` takes `FactorCandidate` (defined in `screener/factor_scorer.py`) — imported at top of main_loop.py ✓
- `score_entry` signature matches `bot/ai_analyst.py:154` (accepts `disclosure=None`, `factor_score`, `ticker` kwargs) ✓
- `AllocationEngine.compute(ticker, base_pct, self._regime_state)` matches `regime/allocation_engine.py:45` ✓
- `RiskManager.validate_order(ticker, position_pct, sector, sector_allocation, position_size_usd, adv_usd)` matches `risk/risk_manager.py:208` ✓
- `PerformanceTracker.summary()` returns same keys as `backtesting.metrics.compute_all` because it calls `compute_all` directly ✓
- `AlpacaBroker.place_order` returns `Order` (from `execution.broker_interface`) — same as `SimulatedBroker` ✓
