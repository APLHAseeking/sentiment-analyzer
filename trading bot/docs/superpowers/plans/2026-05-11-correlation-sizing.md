# Correlation-Aware Position Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scale down position sizes when a candidate is highly correlated with an existing holding, using a linear multiplier derived from 60-day rolling Pearson correlation.

**Architecture:** New `risk/correlation.py` with a `CorrelationFilter` class. Holdings returns are pre-fetched once per morning; candidate returns are fetched lazily with an in-session cache. The multiplier is applied after regime allocation and before the risk veto in both `_process_signal` and `_process_fundamental_candidate`.

**Tech Stack:** Python 3.14, pandas, yfinance, pytest, pytest-mock.

---

## File Map

| File | Task | Change |
|---|---|---|
| `system/config.py` | 1 | Add `CorrelationConfig` dataclass; add `correlation` field to `Settings` |
| `risk/correlation.py` | 1 | New — `CorrelationFilter` class |
| `tests/test_correlation.py` | 1 | New — 11 unit tests |
| `orchestration/main_loop.py` | 2 | Import, `__init__`, load/clear calls, apply multiplier in both signal paths |
| `tests/test_orchestrator.py` | 2 | 1 integration test |

---

## Task 1: CorrelationConfig + CorrelationFilter

**Files:**
- Modify: `system/config.py`
- Create: `risk/correlation.py`
- Create: `tests/test_correlation.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_correlation.py`:

```python
"""Unit tests for CorrelationFilter."""
import numpy as np
import pandas as pd
import pytest


def _make_filter(threshold=0.7, min_overlap=20, window_days=60):
    from system.config import Settings, CorrelationConfig
    cfg = Settings(correlation=CorrelationConfig(
        threshold=threshold,
        window_days=window_days,
        min_overlap_days=min_overlap,
    ))
    from risk.correlation import CorrelationFilter
    return CorrelationFilter(cfg)


def _idx(n=60):
    return pd.date_range("2026-01-01", periods=n, freq="B")


# ── _multiplier_from_rho: pure math, no mocking ───────────────────────

def test_multiplier_below_threshold_returns_one():
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(0.5) == pytest.approx(1.0)


def test_multiplier_at_threshold_returns_one():
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(0.7) == pytest.approx(1.0)


def test_multiplier_midpoint_returns_half():
    # ρ=0.85, threshold=0.7: (0.85-0.7)/(1.0-0.7) = 0.5 → mult = 0.5
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(0.85) == pytest.approx(0.5)


def test_multiplier_at_perfect_correlation_returns_zero():
    f = _make_filter(threshold=0.7)
    assert f._multiplier_from_rho(1.0) == pytest.approx(0.0)


# ── size_multiplier: injected data (no yfinance) ──────────────────────

def test_size_multiplier_returns_one_when_no_holdings():
    f = _make_filter()
    assert f.size_multiplier("AAPL") == pytest.approx(1.0)


def test_size_multiplier_returns_one_for_uncorrelated_candidate():
    f = _make_filter(threshold=0.7)
    idx = _idx(60)
    np.random.seed(0)
    hold = pd.Series(np.random.normal(0, 1, 60), index=idx)
    np.random.seed(1)
    cand = pd.Series(np.random.normal(0, 1, 60), index=idx)
    f._holdings_returns = {"SPY": hold}
    f._candidate_cache = {"AAPL": cand}
    mult = f.size_multiplier("AAPL")
    # Independent series → ρ ≈ 0 → mult = 1.0
    assert mult == pytest.approx(1.0)


def test_size_multiplier_scales_down_for_correlated_candidate():
    f = _make_filter(threshold=0.7)
    idx = _idx(60)
    np.random.seed(42)
    base = pd.Series(np.random.normal(0, 1, 60), index=idx)
    np.random.seed(7)
    noise = pd.Series(np.random.normal(0, 1, 60), index=idx)
    # Theoretical ρ ≈ 0.85 between base and candidate
    candidate = 0.85 * base + np.sqrt(1 - 0.85 ** 2) * noise
    f._holdings_returns = {"SPY": base}
    f._candidate_cache = {"AAPL": candidate}
    mult = f.size_multiplier("AAPL")
    # Allow variance from finite samples: somewhere in (0.2, 0.8)
    assert 0.2 <= mult <= 0.8


def test_size_multiplier_uses_max_correlation_across_holdings():
    f = _make_filter(threshold=0.7)
    idx = _idx(60)
    np.random.seed(42)
    base = pd.Series(np.random.normal(0, 1, 60), index=idx)
    np.random.seed(7)
    noise = pd.Series(np.random.normal(0, 1, 60), index=idx)
    # hold1: ρ ≈ 0.9 with base; hold2: uncorrelated
    hold1 = 0.9 * base + np.sqrt(1 - 0.9 ** 2) * noise
    np.random.seed(99)
    hold2 = pd.Series(np.random.normal(0, 1, 60), index=idx)
    f._holdings_returns = {"SPY": hold1, "QQQ": hold2}
    f._candidate_cache = {"AAPL": base}
    mult = f.size_multiplier("AAPL")
    # hold1 drives the max ρ → significant reduction
    assert mult < 0.6


def test_size_multiplier_skips_holdings_with_insufficient_overlap():
    f = _make_filter(threshold=0.7, min_overlap=30)
    # Holding and candidate date ranges don't overlap
    idx_hold = pd.date_range("2025-01-01", periods=60, freq="B")
    idx_cand = pd.date_range("2026-05-01", periods=60, freq="B")
    np.random.seed(0)
    f._holdings_returns = {
        "SPY": pd.Series(np.random.normal(0, 1, 60), index=idx_hold)
    }
    f._candidate_cache = {
        "AAPL": pd.Series(np.random.normal(0, 1, 60), index=idx_cand)
    }
    assert f.size_multiplier("AAPL") == pytest.approx(1.0)


def test_size_multiplier_returns_one_on_yfinance_failure(mocker):
    f = _make_filter()
    idx = _idx(60)
    np.random.seed(0)
    f._holdings_returns = {
        "SPY": pd.Series(np.random.normal(0, 1, 60), index=idx)
    }
    mocker.patch("risk.correlation.yf.download", side_effect=Exception("network error"))
    assert f.size_multiplier("AAPL") == pytest.approx(1.0)


# ── load_holdings_returns ─────────────────────────────────────────────

def test_load_holdings_returns_empty_list_is_noop():
    f = _make_filter()
    f.load_holdings_returns([])
    assert f._holdings_returns == {}


def test_load_holdings_returns_yfinance_failure_leaves_empty_cache(mocker):
    f = _make_filter()
    mocker.patch("risk.correlation.yf.download", side_effect=Exception("network"))
    f.load_holdings_returns(["AAPL"])
    assert f._holdings_returns == {}


# ── clear ─────────────────────────────────────────────────────────────

def test_clear_resets_both_caches():
    f = _make_filter()
    idx = _idx(30)
    np.random.seed(0)
    f._holdings_returns = {"SPY": pd.Series(np.random.normal(0, 1, 30), index=idx)}
    f._candidate_cache = {"AAPL": pd.Series(np.random.normal(0, 1, 30), index=idx)}
    f.clear()
    assert f._holdings_returns == {}
    assert f._candidate_cache == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_correlation.py -v
```

Expected: all FAILED (ModuleNotFoundError: No module named `risk.correlation`)

- [ ] **Step 3: Add `CorrelationConfig` to `system/config.py`**

Insert the following block immediately after the `HedgeConfig` class and before the `# Risk management` comment:

```python
# ---------------------------------------------------------------------------
# Correlation-aware sizing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrelationConfig:
    threshold: float = 0.7        # ρ at or below this → no reduction
    window_days: int = 60         # daily return lookback period
    min_overlap_days: int = 20    # skip pair if fewer shared trading days
```

- [ ] **Step 4: Add `correlation` field to `Settings` in `system/config.py`**

In the `Settings` dataclass, add after `hedge: HedgeConfig`:

```python
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
```

- [ ] **Step 5: Create `risk/correlation.py`**

```python
"""Correlation-aware position sizing filter.

Reduces position sizes when a candidate is highly correlated with an
existing holding, using rolling Pearson correlation on daily returns.

Usage:
    At pipeline start:  corr_filter.load_holdings_returns(holding_tickers)
    Per candidate:      multiplier = corr_filter.size_multiplier(ticker)
    At pipeline end:    corr_filter.clear()
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


class CorrelationFilter:
    """Compute a position-size multiplier based on pairwise return correlation."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg.correlation
        self._holdings_returns: dict[str, pd.Series] = {}
        self._candidate_cache: dict[str, pd.Series] = {}

    def load_holdings_returns(self, tickers: list[str]) -> None:
        """Pre-fetch window_days returns for long holdings. Call once per morning."""
        self._holdings_returns = {}
        if not tickers:
            return
        try:
            raw = yf.download(tickers, period="3mo", auto_adjust=True, progress=False)
            if raw.empty:
                return
            if isinstance(raw.columns, pd.MultiIndex):
                close = raw["Close"]
            else:
                close = raw[["Close"]].rename(columns={"Close": tickers[0]})
            returns = close.pct_change().dropna()
            for ticker in tickers:
                if ticker in returns.columns:
                    series = returns[ticker].dropna()
                    if not series.empty:
                        self._holdings_returns[ticker] = series
        except Exception as exc:
            log.warning("load_holdings_returns failed: %s", exc)
            self._holdings_returns = {}

    def size_multiplier(self, candidate_ticker: str) -> float:
        """Return a multiplier ∈ [0.0, 1.0] to apply to candidate's position size.

        Returns 1.0 (no penalty) when holdings cache is empty, when max pairwise
        ρ is at or below threshold, or on any data error.
        """
        if not self._holdings_returns:
            return 1.0

        if candidate_ticker not in self._candidate_cache:
            try:
                raw = yf.download(
                    [candidate_ticker], period="3mo", auto_adjust=True, progress=False
                )
                if raw.empty:
                    return 1.0
                if isinstance(raw.columns, pd.MultiIndex):
                    close = raw["Close"][candidate_ticker]
                else:
                    close = raw["Close"]
                self._candidate_cache[candidate_ticker] = close.pct_change().dropna()
            except Exception as exc:
                log.warning(
                    "size_multiplier: could not fetch returns for %s: %s",
                    candidate_ticker, exc,
                )
                return 1.0

        cand_returns = self._candidate_cache[candidate_ticker]
        max_rho = 0.0
        valid = 0

        for hold_returns in self._holdings_returns.values():
            aligned_cand, aligned_hold = cand_returns.align(hold_returns, join="inner")
            if len(aligned_cand) < self._cfg.min_overlap_days:
                continue
            rho = float(aligned_cand.corr(aligned_hold))
            if pd.isna(rho):
                continue
            if rho > max_rho:
                max_rho = rho
            valid += 1

        if valid == 0:
            return 1.0

        return self._multiplier_from_rho(max_rho)

    def _multiplier_from_rho(self, rho: float) -> float:
        """Linear decay from 1.0 at threshold to 0.0 at perfect correlation."""
        if rho <= self._cfg.threshold:
            return 1.0
        return max(0.0, 1.0 - (rho - self._cfg.threshold) / (1.0 - self._cfg.threshold))

    def clear(self) -> None:
        """Reset returns caches. Call at end of morning pipeline."""
        self._holdings_returns = {}
        self._candidate_cache = {}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_correlation.py -v
```

Expected: 11 PASSED

- [ ] **Step 7: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 8: Commit**

```bash
cd "trading bot" && git add system/config.py risk/correlation.py tests/test_correlation.py && git commit -m "$(cat <<'EOF'
feat: add CorrelationFilter for correlation-aware position sizing

CorrelationFilter.size_multiplier() returns a linear multiplier in
[0, 1] based on max pairwise Pearson correlation with current holdings
over a 60-day rolling window. Holdings returns are pre-fetched in batch
once per morning; candidate returns are fetched lazily with caching.
Returns 1.0 (no penalty) on data errors or below the threshold.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Orchestrator Wiring + Integration Test

**Context:** Wire `CorrelationFilter` into the orchestrator. Four changes to `orchestration/main_loop.py`:
1. Import + `__init__`: instantiate `CorrelationFilter`
2. `run_morning_pipeline` start of `if not _at_capacity:` block: load holdings returns
3. `_process_signal` and `_process_fundamental_candidate`: apply multiplier after regime allocation
4. End of `run_morning_pipeline`: clear the cache

**Files:**
- Modify: `orchestration/main_loop.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing integration test**

Add to `tests/test_orchestrator.py`:

```python
def test_process_signal_applies_correlation_multiplier(mocker, orch):
    """correlation multiplier of 0.5 should halve the opened position size."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    orch._regime_state = None  # no regime → final_pct = AI position_pct directly

    mocker.patch("orchestration.main_loop.get_committees_for_politician",
                 return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=4.0,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=MagicMock(info={"regularMarketPrice": 100.0}))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=0.5)

    disc = {
        "id": "d1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] == pytest.approx(2.0)  # 4.0 * 0.5
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "trading bot" && python3 -m pytest tests/test_orchestrator.py::test_process_signal_applies_correlation_multiplier -v
```

Expected: FAILED (AttributeError: `RegimeAwareOrchestrator` has no `_corr_filter`)

- [ ] **Step 3: Add `CorrelationFilter` import to `orchestration/main_loop.py`**

Add after the `from hedge.hedge_engine import HedgeEngine` import:

```python
from risk.correlation import CorrelationFilter
```

- [ ] **Step 4: Instantiate `CorrelationFilter` in `RegimeAwareOrchestrator.__init__`**

After `self._hedge_engine = HedgeEngine(self._cfg)`, add:

```python
        self._corr_filter = CorrelationFilter(self._cfg)
```

- [ ] **Step 5: Load holdings returns at start of `if not _at_capacity:` block in `run_morning_pipeline`**

Find the start of the `if not _at_capacity:` block:

```python
        if not _at_capacity:
            # --- Regime state as gate -----------------------------------
            if self._regime_state is None:
                log.warning("No regime state — processing signals without regime filter")

            sector_allocation: dict[str, float] = {}
```

Replace with:

```python
        if not _at_capacity:
            # --- Regime state as gate -----------------------------------
            if self._regime_state is None:
                log.warning("No regime state — processing signals without regime filter")

            # --- Correlation filter: pre-load holdings returns ----------
            _long_tickers = [
                pos["ticker"] for pos in get_open_positions()
                if pos.get("signal_source") != "hedge"
            ]
            self._corr_filter.load_holdings_returns(_long_tickers)
            # ------------------------------------------------------------

            sector_allocation: dict[str, float] = {}
```

- [ ] **Step 6: Clear correlation cache at end of `run_morning_pipeline`**

Find the very end of `run_morning_pipeline` (after Phase 3):

```python
        # ── Phase 3: inverse ETF hedge pass ─────────────────────────
        if is_hedge_now:
            self._run_hedge_pass()
        # ─────────────────────────────────────────────────────────────
```

Replace with:

```python
        # ── Phase 3: inverse ETF hedge pass ─────────────────────────
        if is_hedge_now:
            self._run_hedge_pass()
        # ─────────────────────────────────────────────────────────────

        self._corr_filter.clear()
```

- [ ] **Step 7: Apply correlation multiplier in `_process_signal`**

Find this exact block in `_process_signal`:

```python
        else:
            final_pct = base_pct

        # Risk manager veto
        entry_price_info = yf.Ticker(ticker).info
```

Replace with:

```python
        else:
            final_pct = base_pct

        # Correlation filter
        corr_mult = self._corr_filter.size_multiplier(ticker)
        final_pct *= corr_mult
        if final_pct < 0.1:
            emit_event(log, EventType.SIGNAL_REJECTED,
                       f"{ticker} reduced to zero by correlation filter (mult={corr_mult:.2f})")
            return

        # Risk manager veto
        entry_price_info = yf.Ticker(ticker).info
```

- [ ] **Step 8: Apply correlation multiplier in `_process_fundamental_candidate`**

Find this exact block in `_process_fundamental_candidate`:

```python
        else:
            final_pct = base_pct

        entry_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
```

Replace with:

```python
        else:
            final_pct = base_pct

        # Correlation filter
        corr_mult = self._corr_filter.size_multiplier(ticker)
        final_pct *= corr_mult
        if final_pct < 0.1:
            emit_event(
                log, EventType.SIGNAL_REJECTED,
                f"{ticker} ({signal_type}) reduced to zero by correlation filter "
                f"(mult={corr_mult:.2f})",
            )
            return False

        entry_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
```

- [ ] **Step 9: Run failing test to verify it now passes**

```bash
cd "trading bot" && python3 -m pytest tests/test_orchestrator.py::test_process_signal_applies_correlation_multiplier -v
```

Expected: PASSED

- [ ] **Step 10: Run full suite**

```bash
cd "trading bot" && python3 -m pytest tests/ 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 11: Commit**

```bash
cd "trading bot" && git add orchestration/main_loop.py tests/test_orchestrator.py && git commit -m "$(cat <<'EOF'
feat: wire CorrelationFilter into morning pipeline

Holdings returns pre-loaded once at start of if-not-at-capacity block.
Both _process_signal and _process_fundamental_candidate apply the
correlation multiplier after regime allocation and before the risk veto.
Cache cleared at end of run_morning_pipeline.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final Check

```bash
cd "trading bot" && python3 -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: all green.
