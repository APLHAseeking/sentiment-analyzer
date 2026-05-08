# Inverse ETF Hedging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the HMM regime is bear, deep-bear, or crash, the bot allocates to single-inverse ETFs instead of sitting idle in cash; all hedge positions exit immediately when the regime returns to neutral or better.

**Architecture:** A new `HedgeEngine` class owns all hedge logic. The orchestrator calls it as a black box. No DB schema changes — hedge positions use `signal_source="hedge"` on the existing `positions` table. Four sequential tasks: config + event types → HedgeEngine core → portfolio source filtering → orchestrator wiring.

**Tech Stack:** Python 3.14, yfinance, pytest, pytest-mock, SQLite, existing Anthropic/Alpaca infra.

---

## File Map

| File | Task | Change |
|---|---|---|
| `system/config.py` | 1 | Add `HedgeConfig` dataclass; add `enable_inverse_hedging` to `RiskConfig`; add `hedge: HedgeConfig` to `Settings` |
| `monitoring/logger.py` | 1 | Add `HEDGE_ENTRY`, `HEDGE_EXIT`, `HEDGE_STOP_LOSS` to `EventType` |
| `hedge/__init__.py` | 2 | New empty package marker |
| `hedge/hedge_engine.py` | 2 | `HedgeOrder` dataclass + `HedgeEngine` class |
| `tests/test_hedge_engine.py` | 2 | New — 10 unit tests |
| `bot/portfolio.py` | 3 | Add `source_include` / `source_exclude` params to `enforce_stop_losses`; add `source_exclude` to `enforce_take_profits` |
| `tests/test_portfolio.py` | 3 | Add 3 source-filter tests |
| `orchestration/main_loop.py` | 4 | Wire `HedgeEngine` into `__init__`; split stop-loss calls; add regime-gate + `_run_hedge_pass()` + `_run_hedge_exits()` |
| `tests/test_orchestrator.py` | 4 | Fix 2 broken existing tests; add 4 hedge routing tests |

---

## Task 1: Config + Event Types

**Files:**
- Modify: `system/config.py`
- Modify: `monitoring/logger.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:

```python
def test_hedge_config_defaults():
    from system.config import HedgeConfig
    cfg = HedgeConfig()
    assert "SH" in cfg.inverse_etf_universe
    assert "PSQ" in cfg.inverse_etf_universe
    assert cfg.max_single_position_pct == 15.0
    assert cfg.conflict_threshold_pct == 10.0
    assert cfg.stop_loss_pct == 10.0
    assert cfg.max_inverse_pct_by_regime["bear"] == 30.0
    assert cfg.max_inverse_pct_by_regime["crash"] == 50.0


def test_enable_inverse_hedging_default_true():
    from system.config import RiskConfig
    assert RiskConfig().enable_inverse_hedging is True


def test_settings_has_hedge_field():
    from system.config import settings
    assert hasattr(settings, "hedge")
    assert settings.hedge.stop_loss_pct == 10.0


def test_hedge_event_types_exist():
    from monitoring.logger import EventType
    assert EventType.HEDGE_ENTRY.value == "hedge_entry"
    assert EventType.HEDGE_EXIT.value == "hedge_exit"
    assert EventType.HEDGE_STOP_LOSS.value == "hedge_stop_loss"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_config.py::test_hedge_config_defaults tests/test_config.py::test_enable_inverse_hedging_default_true tests/test_config.py::test_settings_has_hedge_field tests/test_config.py::test_hedge_event_types_exist -v
```

Expected: 4 FAILED (ImportError / AttributeError)

- [ ] **Step 3: Add `HedgeConfig` to `system/config.py`**

Insert the following block immediately after the `AllocationConfig` dataclass and before the `# Risk management` comment:

```python
# ---------------------------------------------------------------------------
# Inverse ETF hedging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HedgeConfig:
    # ETF ticker → sectors it conflicts with (empty list = no sector filter)
    inverse_etf_universe: dict[str, list[str]] = field(default_factory=lambda: {
        "SH":  [],                                         # broad S&P 500
        "PSQ": ["Technology", "Communication Services"],   # Nasdaq
        "RWM": [],                                         # Russell 2000
        "SBB": [],                                         # short small-cap
        "EFZ": [],                                         # short MSCI EAFE
    })
    # Max total inverse allocation (% of NAV) by regime label
    max_inverse_pct_by_regime: dict[str, float] = field(default_factory=lambda: {
        "bear":      30.0,
        "crash":     50.0,
        "deep-bear": 50.0,
    })
    max_single_position_pct: float = 15.0   # per-ETF cap (% of NAV)
    conflict_threshold_pct: float = 10.0    # block ETF if portfolio > this % in its sectors
    stop_loss_pct: float = 10.0             # tighter than long 15%
```

- [ ] **Step 4: Add `enable_inverse_hedging` to `RiskConfig` in `system/config.py`**

Add one line at the end of `RiskConfig`, after `max_invested_pct`:

```python
    enable_inverse_hedging: bool = True     # set False to disable all inverse ETF hedging
```

- [ ] **Step 5: Add `hedge: HedgeConfig` to `Settings` in `system/config.py`**

In the `Settings` dataclass, add after the `allocation` field:

```python
    hedge: HedgeConfig = field(default_factory=HedgeConfig)
```

- [ ] **Step 6: Add three event types to `monitoring/logger.py`**

In the `EventType` enum, add after `SHUTDOWN`:

```python
    HEDGE_ENTRY = "hedge_entry"
    HEDGE_EXIT = "hedge_exit"
    HEDGE_STOP_LOSS = "hedge_stop_loss"
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_config.py::test_hedge_config_defaults tests/test_config.py::test_enable_inverse_hedging_default_true tests/test_config.py::test_settings_has_hedge_field tests/test_config.py::test_hedge_event_types_exist -v
```

Expected: 4 PASSED

- [ ] **Step 8: Run full suite for regressions**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 9: Commit**

```bash
cd "trading bot" && git add system/config.py monitoring/logger.py tests/test_config.py && git commit -m "$(cat <<'EOF'
feat: add HedgeConfig and hedge event types

Adds HedgeConfig (inverse ETF universe, per-regime allocation caps,
conflict threshold, stop-loss pct) to system/config.py. Adds
enable_inverse_hedging kill-switch to RiskConfig. Adds HEDGE_ENTRY,
HEDGE_EXIT, HEDGE_STOP_LOSS to EventType.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: HedgeEngine Core

**Files:**
- Create: `hedge/__init__.py`
- Create: `hedge/hedge_engine.py`
- Create: `tests/test_hedge_engine.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_hedge_engine.py`:

```python
"""Unit tests for HedgeEngine — compute_hedge_plan and get_exits_needed."""
import pytest
from regime.hmm_engine import RegimeState


def _regime(label: str, confidence: float = 0.85) -> RegimeState:
    return RegimeState(
        date="2026-05-08",
        regime_index=0,
        regime_label=label,
        confidence=confidence,
        is_stable=True,
        n_regimes=5,
        raw_posteriors=[0.85, 0.05, 0.05, 0.03, 0.02],
    )


def _engine(enable: bool = True):
    from system.config import Settings, RiskConfig
    cfg = Settings(risk=RiskConfig(enable_inverse_hedging=enable))
    from hedge.hedge_engine import HedgeEngine
    return HedgeEngine(cfg)


def test_is_hedge_regime_true_for_bear_crash_deep_bear():
    e = _engine()
    assert e.is_hedge_regime(_regime("bear")) is True
    assert e.is_hedge_regime(_regime("crash")) is True
    assert e.is_hedge_regime(_regime("deep-bear")) is True


def test_is_hedge_regime_false_for_neutral_bull():
    e = _engine()
    assert e.is_hedge_regime(_regime("neutral")) is False
    assert e.is_hedge_regime(_regime("bull")) is False
    assert e.is_hedge_regime(_regime("euphoria")) is False


def test_compute_hedge_plan_returns_empty_when_kill_switch_off():
    e = _engine(enable=False)
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    assert result == []


def test_compute_hedge_plan_returns_empty_when_not_hedge_regime():
    e = _engine()
    result = e.compute_hedge_plan(_regime("neutral"), [], {}, 100_000)
    assert result == []


def test_compute_hedge_plan_excludes_already_open_hedge_tickers():
    e = _engine()
    open_positions = [{"ticker": "SH", "signal_source": "hedge"}]
    result = e.compute_hedge_plan(_regime("bear"), open_positions, {}, 100_000)
    tickers = [o.ticker for o in result]
    assert "SH" not in tickers
    assert len(result) == 4  # 5 ETFs - 1 already open


def test_compute_hedge_plan_excludes_conflicting_etfs():
    e = _engine()
    # PSQ conflicts with Technology + Communication Services
    sector_alloc = {"Technology": 15.0}  # 15% > 10% threshold
    result = e.compute_hedge_plan(_regime("bear"), [], sector_alloc, 100_000)
    tickers = [o.ticker for o in result]
    assert "PSQ" not in tickers
    assert len(result) == 4  # SH, RWM, SBB, EFZ eligible


def test_compute_hedge_plan_equal_weights_eligible_etfs():
    e = _engine()
    # Bear regime: max_alloc=30%, 5 ETFs → alloc_per_etf = min(30/5, 15) = 6%
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    assert len(result) == 5
    for order in result:
        assert order.position_pct == pytest.approx(6.0)


def test_compute_hedge_plan_caps_each_position_at_max_single():
    from system.config import Settings, RiskConfig, HedgeConfig
    cfg = Settings(
        risk=RiskConfig(enable_inverse_hedging=True),
        hedge=HedgeConfig(
            inverse_etf_universe={"SH": [], "PSQ": []},
            max_inverse_pct_by_regime={"crash": 50.0},
            max_single_position_pct=15.0,
        ),
    )
    from hedge.hedge_engine import HedgeEngine
    e = HedgeEngine(cfg)
    # 2 ETFs, crash: alloc_per_etf = min(50/2, 15) = min(25, 15) = 15%
    result = e.compute_hedge_plan(_regime("crash"), [], {}, 100_000)
    assert len(result) == 2
    for order in result:
        assert order.position_pct == pytest.approx(15.0)


def test_compute_hedge_plan_total_allocation_does_not_exceed_regime_cap():
    from system.config import Settings, RiskConfig, HedgeConfig
    cfg = Settings(
        risk=RiskConfig(enable_inverse_hedging=True),
        hedge=HedgeConfig(
            inverse_etf_universe={"SH": [], "PSQ": []},
            max_inverse_pct_by_regime={"bear": 25.0},
            max_single_position_pct=20.0,
        ),
    )
    from hedge.hedge_engine import HedgeEngine
    e = HedgeEngine(cfg)
    # 2 ETFs, bear: alloc_per_etf = min(25/2, 20) = 12.5; total = 25%
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    total = sum(o.position_pct for o in result)
    assert total == pytest.approx(25.0)


def test_get_exits_needed_returns_hedge_tickers_only():
    e = _engine()
    open_positions = [
        {"ticker": "SH", "signal_source": "hedge"},
        {"ticker": "AAPL", "signal_source": "congressional"},
        {"ticker": "PSQ", "signal_source": "hedge"},
    ]
    result = e.get_exits_needed(open_positions)
    assert set(result) == {"SH", "PSQ"}


def test_get_exits_needed_returns_empty_when_no_hedges():
    e = _engine()
    open_positions = [{"ticker": "AAPL", "signal_source": "congressional"}]
    result = e.get_exits_needed(open_positions)
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_hedge_engine.py -v
```

Expected: all FAILED (ModuleNotFoundError: No module named `hedge`)

- [ ] **Step 3: Create `hedge/__init__.py`**

Create an empty file at `hedge/__init__.py` in the `trading bot/` root (same level as `bot/`, `orchestration/`, etc.).

- [ ] **Step 4: Create `hedge/hedge_engine.py`**

```python
"""Inverse ETF hedge engine.

Given the current regime state and open portfolio positions, computes
which inverse ETF orders to open and which to close. The orchestrator
calls this as a black box — no coupling to the signal pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from regime.hmm_engine import RegimeState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HedgeOrder:
    ticker: str
    position_pct: float   # % of NAV to allocate
    rationale: str


class HedgeEngine:
    """Compute hedge entry and exit decisions from regime state."""

    def __init__(self, cfg: Any) -> None:
        self._risk_cfg = cfg.risk
        self._hedge_cfg = cfg.hedge

    def is_hedge_regime(self, regime_state: RegimeState) -> bool:
        """True if the regime label is bear, crash, or deep-bear."""
        return regime_state.regime_label in self._hedge_cfg.max_inverse_pct_by_regime

    def compute_hedge_plan(
        self,
        regime_state: RegimeState,
        open_positions_meta: list[dict],
        sector_allocation: dict[str, float],
        nav: float,
    ) -> list[HedgeOrder]:
        """Return orders for inverse ETF positions to open.

        Already-open hedges and ETFs that conflict with current long
        sector exposure are excluded. Equal-weights eligible ETFs up to
        the regime's allocation cap and the per-ETF size cap.
        """
        if not self._risk_cfg.enable_inverse_hedging:
            return []
        if not self.is_hedge_regime(regime_state):
            return []

        max_alloc = self._hedge_cfg.max_inverse_pct_by_regime[regime_state.regime_label]
        already_open = {
            p["ticker"] for p in open_positions_meta
            if p.get("signal_source") == "hedge"
        }

        eligible: list[str] = []
        for etf, conflict_sectors in self._hedge_cfg.inverse_etf_universe.items():
            if etf in already_open:
                continue
            conflicted = any(
                sector_allocation.get(s, 0.0) > self._hedge_cfg.conflict_threshold_pct
                for s in conflict_sectors
            )
            if conflicted:
                continue
            eligible.append(etf)

        if not eligible:
            log.info("HedgeEngine: no eligible ETFs (all open or conflicted)")
            return []

        alloc_per_etf = min(
            max_alloc / len(eligible),
            self._hedge_cfg.max_single_position_pct,
        )

        return [
            HedgeOrder(
                ticker=etf,
                position_pct=alloc_per_etf,
                rationale=(
                    f"Regime hedge: {etf} in {regime_state.regime_label} "
                    f"(conf={regime_state.confidence:.2f}), alloc={alloc_per_etf:.1f}%"
                ),
            )
            for etf in eligible
        ]

    def get_exits_needed(self, open_positions_meta: list[dict]) -> list[str]:
        """Return tickers of all open hedge positions (to close on regime exit)."""
        return [
            p["ticker"] for p in open_positions_meta
            if p.get("signal_source") == "hedge"
        ]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_hedge_engine.py -v
```

Expected: 10 PASSED

- [ ] **Step 6: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
cd "trading bot" && git add hedge/__init__.py hedge/hedge_engine.py tests/test_hedge_engine.py && git commit -m "$(cat <<'EOF'
feat: add HedgeEngine for inverse ETF hedging logic

HedgeEngine.compute_hedge_plan() selects eligible inverse ETFs for the
current bear/crash regime, excludes already-open positions and
sector-conflicting ETFs, and equal-weights the remainder up to the
regime allocation cap and per-ETF size cap. get_exits_needed() returns
all open hedge positions for immediate close on regime transition.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Portfolio Source Filtering

**Context:** `enforce_stop_losses` and `enforce_take_profits` currently process all positions with the same threshold. Hedge positions need a tighter stop-loss (10%) and should never take profit (they exit on regime change). Two new optional parameters allow the caller to restrict processing by `signal_source`.

**Files:**
- Modify: `bot/portfolio.py`
- Modify: `tests/test_portfolio.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_portfolio.py`:

```python
def test_enforce_stop_losses_source_include_processes_only_matching(mock_broker, db):
    from system.config import RiskConfig
    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=5.0))
    # Both positions drop 6% — triggers 5% custom threshold
    mock_broker.get_positions.return_value = [
        {"ticker": "SH",   "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
        {"ticker": "AAPL", "qty": 5.0,  "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "sf-aapl-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sf-aapl-01", "AAPL", 7, 4.0, "Good", [])
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge")
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional")
    # source_include="hedge" → only SH processed
    closed = p.enforce_stop_losses(source_include="hedge")
    assert "SH" in closed
    assert "AAPL" not in closed


def test_enforce_stop_losses_source_exclude_skips_matching(mock_broker, db):
    from system.config import RiskConfig
    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=5.0))
    mock_broker.get_positions.return_value = [
        {"ticker": "SH",   "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
        {"ticker": "AAPL", "qty": 5.0,  "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "sf-aapl-02", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sf-aapl-02", "AAPL", 7, 4.0, "Good", [])
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge")
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional")
    # source_exclude="hedge" → SH skipped, AAPL processed
    closed = p.enforce_stop_losses(source_exclude="hedge")
    assert "SH" not in closed
    assert "AAPL" in closed


def test_enforce_stop_losses_raises_when_both_filters_set(mock_broker):
    p = Portfolio(broker=mock_broker)
    with pytest.raises(ValueError, match="mutually exclusive"):
        p.enforce_stop_losses(source_include="hedge", source_exclude="congressional")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_portfolio.py::test_enforce_stop_losses_source_include_processes_only_matching tests/test_portfolio.py::test_enforce_stop_losses_source_exclude_skips_matching tests/test_portfolio.py::test_enforce_stop_losses_raises_when_both_filters_set -v
```

Expected: 3 FAILED (TypeError: unexpected keyword argument)

- [ ] **Step 3: Update `enforce_stop_losses` in `bot/portfolio.py`**

Replace the existing `enforce_stop_losses` method with:

```python
    def enforce_stop_losses(
        self,
        stop_loss_pct: float | None = None,
        source_include: str | None = None,
        source_exclude: str | None = None,
    ) -> list[str]:
        if source_include is not None and source_exclude is not None:
            raise ValueError("source_include and source_exclude are mutually exclusive")
        pct = stop_loss_pct if stop_loss_pct is not None else self._risk.trailing_stop_pct
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            current = pos["current_price"]
            meta = open_positions.get(ticker, {})
            source = meta.get("signal_source", "congressional")

            if source_include is not None and source != source_include:
                continue
            if source_exclude is not None and source == source_exclude:
                continue

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
```

- [ ] **Step 4: Add `source_exclude` to `enforce_take_profits` in `bot/portfolio.py`**

Replace the existing `enforce_take_profits` method with:

```python
    def enforce_take_profits(
        self,
        take_profit_pct: float | None = None,
        source_exclude: str | None = None,
    ) -> list[str]:
        pct = take_profit_pct if take_profit_pct is not None else self._risk.take_profit_pct
        reduced = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            if ticker in reduced:
                continue
            meta = open_positions.get(ticker, {})
            source = meta.get("signal_source", "congressional")

            if source_exclude is not None and source == source_exclude:
                continue

            entry = pos["avg_entry_price"]
            current = pos["current_price"]
            gain_pct = (current - entry) / entry * 100
            if gain_pct >= pct:
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
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_portfolio.py -v
```

Expected: all green

- [ ] **Step 6: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
cd "trading bot" && git add bot/portfolio.py tests/test_portfolio.py && git commit -m "$(cat <<'EOF'
feat: add source filtering to enforce_stop_losses and enforce_take_profits

source_include/source_exclude params allow the orchestrator to apply
different stop-loss thresholds to hedge (10%) vs long (15%) positions
without duplicating the enforcement logic.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Orchestrator Wiring

**Context:** Wire `HedgeEngine` into the morning pipeline. The changes are:
1. Instantiate `HedgeEngine` in `__init__`
2. Split the existing `enforce_stop_losses()` / `enforce_take_profits()` calls to exclude hedges, then enforce hedge stop-loss separately
3. After `_update_regime()`, compute `is_hedge_now` and call `_run_hedge_exits()` when not in hedge regime
4. At the end of the signal-processing block, call `_run_hedge_pass()` when in hedge regime
5. Fix two existing orchestrator tests whose `assert_called_once()` assertions break when stop-loss is called twice

**Files:**
- Modify: `orchestration/main_loop.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_orchestrator.py` (keep all existing tests):

```python
from regime.hmm_engine import RegimeState as _RegimeState


def _bear_regime() -> _RegimeState:
    return _RegimeState(
        date="2026-05-08", regime_index=1, regime_label="bear",
        confidence=0.85, is_stable=True, n_regimes=5,
        raw_posteriors=[0.05, 0.85, 0.05, 0.03, 0.02],
    )


def _neutral_regime() -> _RegimeState:
    return _RegimeState(
        date="2026-05-08", regime_index=2, regime_label="neutral",
        confidence=0.85, is_stable=True, n_regimes=5,
        raw_posteriors=[0.05, 0.05, 0.85, 0.03, 0.02],
    )


def test_hedge_pass_called_when_regime_is_bear(mocker, orch_fitted):
    orch_fitted._regime_state = _bear_regime()
    hedge_pass_spy = mocker.patch.object(orch_fitted, "_run_hedge_pass")
    mocker.patch.object(orch_fitted, "_run_hedge_exits")
    orch_fitted.run_morning_pipeline()
    hedge_pass_spy.assert_called_once()


def test_hedge_pass_not_called_when_regime_is_neutral(mocker, orch_fitted):
    orch_fitted._regime_state = _neutral_regime()
    hedge_pass_spy = mocker.patch.object(orch_fitted, "_run_hedge_pass")
    mocker.patch.object(orch_fitted, "_run_hedge_exits")
    orch_fitted.run_morning_pipeline()
    hedge_pass_spy.assert_not_called()


def test_hedge_exits_called_when_regime_is_not_hedge(mocker, orch_fitted):
    orch_fitted._regime_state = _neutral_regime()
    exits_spy = mocker.patch.object(orch_fitted, "_run_hedge_exits")
    mocker.patch.object(orch_fitted, "_run_hedge_pass")
    orch_fitted.run_morning_pipeline()
    exits_spy.assert_called_once()


def test_hedge_exits_not_called_when_regime_is_bear(mocker, orch_fitted):
    orch_fitted._regime_state = _bear_regime()
    exits_spy = mocker.patch.object(orch_fitted, "_run_hedge_exits")
    mocker.patch.object(orch_fitted, "_run_hedge_pass")
    orch_fitted.run_morning_pipeline()
    exits_spy.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_orchestrator.py::test_hedge_pass_called_when_regime_is_bear tests/test_orchestrator.py::test_hedge_pass_not_called_when_regime_is_neutral tests/test_orchestrator.py::test_hedge_exits_called_when_regime_is_not_hedge tests/test_orchestrator.py::test_hedge_exits_not_called_when_regime_is_bear -v
```

Expected: 4 FAILED (AttributeError: `_run_hedge_pass` / `_run_hedge_exits` not found)

- [ ] **Step 3: Add `HedgeEngine` import to `orchestration/main_loop.py`**

Add to the imports block (after the other local imports):

```python
from hedge.hedge_engine import HedgeEngine
```

- [ ] **Step 4: Add `_hedge_engine` to `RegimeAwareOrchestrator.__init__`**

In `__init__`, after `self._last_refit_date: date | None = None`, add:

```python
        # Hedge engine
        self._hedge_engine = HedgeEngine(self._cfg)
```

- [ ] **Step 5: Update `run_morning_pipeline` — split stop-loss/take-profit calls**

Find these two lines:

```python
        self._portfolio.enforce_stop_losses()
        self._portfolio.enforce_take_profits()
```

Replace with:

```python
        # Long positions: existing thresholds, skip hedge positions
        self._portfolio.enforce_stop_losses(source_exclude="hedge")
        self._portfolio.enforce_take_profits(source_exclude="hedge")
        # Hedge positions: tighter stop-loss (10%), no take-profit
        if self._cfg.risk.enable_inverse_hedging:
            self._portfolio.enforce_stop_losses(
                stop_loss_pct=self._cfg.hedge.stop_loss_pct,
                source_include="hedge",
            )
```

- [ ] **Step 6: Update `run_morning_pipeline` — add regime-transition check after `_update_regime()`**

Find:

```python
        self._update_regime()
        self._update_dashboard()

        log.info("Morning pipeline started")
```

Replace with:

```python
        self._update_regime()
        self._update_dashboard()

        # ── Hedge regime gate ────────────────────────────────────────────
        is_hedge_now = (
            self._regime_state is not None
            and self._hedge_engine.is_hedge_regime(self._regime_state)
        )
        if not is_hedge_now:
            self._run_hedge_exits()
        # ─────────────────────────────────────────────────────────────────

        log.info("Morning pipeline started")
```

- [ ] **Step 7: Update `run_morning_pipeline` — add hedge entry pass at end of `if not _at_capacity:` block**

Find the end of the `if not _at_capacity:` block — the last statement is the Phase 2 try/except block. After it (still inside `if not _at_capacity:`), add:

```python
            # ── Phase 3: inverse ETF hedge pass ─────────────────────────
            if is_hedge_now:
                self._run_hedge_pass()
            # ─────────────────────────────────────────────────────────────
```

The exact insertion point is after:

```python
            except Exception:
                log.exception("Phase 2 fundamental screener failed — skipping")
```

- [ ] **Step 8: Add `_run_hedge_pass()` method to `RegimeAwareOrchestrator`**

Add after `_process_fundamental_candidate()`:

```python
    # ------------------------------------------------------------------
    # Hedge entry / exit
    # ------------------------------------------------------------------

    def _run_hedge_pass(self) -> None:
        """Open inverse ETF positions for the current hedge regime."""
        try:
            positions = self._broker.get_positions()
            cash = self._broker.get_cash()
            nav = cash + sum(p["qty"] * p["current_price"] for p in positions) if positions else cash

            # Sector allocation from long positions only (exclude hedges)
            open_positions_meta = get_open_positions()
            sector_allocation: dict[str, float] = {}
            if positions and nav > 0:
                for pos in positions:
                    meta = next(
                        (m for m in open_positions_meta if m["ticker"] == pos["ticker"]), {}
                    )
                    if meta.get("signal_source") == "hedge":
                        continue
                    sector = get_sector_for_ticker(pos["ticker"])
                    pv = pos["qty"] * pos["current_price"]
                    sector_allocation[sector] = sector_allocation.get(sector, 0.0) + pv / nav * 100

            orders = self._hedge_engine.compute_hedge_plan(
                self._regime_state,
                open_positions_meta,
                sector_allocation,
                nav,
            )
            if not orders:
                log.info("Hedge pass: no eligible ETFs for regime %s",
                         self._regime_state.regime_label if self._regime_state else "?")
                return

            for order in orders:
                try:
                    entry_price = yf.Ticker(order.ticker).info.get("regularMarketPrice", 0)
                    if not entry_price:
                        log.warning("No price for hedge ETF %s — skipping", order.ticker)
                        continue
                    self._portfolio.open_position(
                        ticker=order.ticker,
                        position_pct=order.position_pct,
                        signal_id=None,
                        rationale=order.rationale,
                        entry_price=entry_price,
                        signal_source="hedge",
                    )
                    emit_event(
                        log, EventType.HEDGE_ENTRY,
                        f"Opened hedge {order.ticker} pct={order.position_pct:.1f}%",
                        data={
                            "ticker": order.ticker,
                            "position_pct": order.position_pct,
                            "regime_label": self._regime_state.regime_label if self._regime_state else "?",
                            "regime_confidence": self._regime_state.confidence if self._regime_state else 0.0,
                            "rationale": order.rationale,
                        },
                        alert=True,
                    )
                except Exception:
                    log.exception("Failed to open hedge position %s", order.ticker)
        except Exception as exc:
            log.warning("Hedge pass failed: %s", exc)

    def _run_hedge_exits(self) -> None:
        """Close all open hedge positions (called when regime leaves bear/crash)."""
        try:
            open_positions_meta = get_open_positions()
            tickers = self._hedge_engine.get_exits_needed(open_positions_meta)
            if not tickers:
                return
            current_label = self._regime_state.regime_label if self._regime_state else "unknown"
            for ticker in tickers:
                try:
                    pos_meta = next(
                        (p for p in open_positions_meta if p["ticker"] == ticker), None
                    )
                    if pos_meta is None:
                        continue
                    exit_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
                    if not exit_price:
                        log.warning("No price for hedge exit %s — skipping", ticker)
                        continue
                    self._portfolio.close_position(
                        ticker=ticker,
                        shares=pos_meta["shares"],
                        exit_price=exit_price,
                        exit_reason="regime_transition",
                        signal_id=None,
                        entry_price=pos_meta["entry_price"],
                        entry_date=pos_meta["entry_date"],
                        signal_source="hedge",
                    )
                    emit_event(
                        log, EventType.HEDGE_EXIT,
                        f"Closed hedge {ticker}: regime → {current_label}",
                        data={
                            "ticker": ticker,
                            "exit_reason": "regime_transition",
                            "exit_regime": current_label,
                        },
                        alert=True,
                    )
                except Exception:
                    log.exception("Failed to close hedge position %s", ticker)
        except Exception as exc:
            log.warning("Hedge exit pass failed: %s", exc)
```

- [ ] **Step 9: Fix two broken existing orchestrator tests**

In `tests/test_orchestrator.py`, the two tests below use `assert_called_once()` on `enforce_stop_losses`. After the Task 4 split, it is called twice (once for longs, once for hedges). Update both:

Find:

```python
    orch._portfolio.enforce_stop_losses.assert_called_once()
    orch._portfolio.enforce_take_profits.assert_called_once()
```

Replace with:

```python
    assert orch._portfolio.enforce_stop_losses.call_count >= 1
    assert orch._portfolio.enforce_take_profits.call_count >= 1
```

And find (in `test_pipeline_skips_entries_when_at_capacity`):

```python
    orch._portfolio.enforce_stop_losses.assert_called_once()
```

Replace with:

```python
    assert orch._portfolio.enforce_stop_losses.call_count >= 1
```

- [ ] **Step 10: Run all orchestrator tests to verify they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_orchestrator.py -v
```

Expected: all green (6 pre-existing + 4 new = 10 total)

- [ ] **Step 11: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 12: Commit**

```bash
cd "trading bot" && git add orchestration/main_loop.py tests/test_orchestrator.py && git commit -m "$(cat <<'EOF'
feat: wire HedgeEngine into morning pipeline

- Instantiates HedgeEngine in __init__
- Splits enforce_stop_losses into long (15%) and hedge (10%) calls
- Calls _run_hedge_exits() every morning when not in a hedge regime
  (no-op when no hedge positions open; handles stale positions on restart)
- Calls _run_hedge_pass() in Phase 3 when regime is bear/crash
- Both hedge methods are individually try/except wrapped so failures
  never crash the morning pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final Check

- [ ] Full suite one last time:

```bash
cd "trading bot" && python3 -m pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: all tests green.
