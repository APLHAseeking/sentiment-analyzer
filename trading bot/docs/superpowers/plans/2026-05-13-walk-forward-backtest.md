# Walk-Forward Backtesting Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing four-file backtesting layer with turnover/holding-period metrics, regime/confidence analysis, benchmark wiring into walk-forward output, fill-delay simulation, and per-window stress testing.

**Architecture:** Five independent tasks (simulation → metrics → analysis → stress_test → walk_forward). Tasks 1–4 touch one file each and are mutually independent. Task 5 wires them all into `walk_forward.py`. Existing code is extended, never replaced; all 348 existing tests must remain green.

**Tech Stack:** Python 3.14, NumPy, pandas, pytest, pytest-mock. No new third-party dependencies.

---

## File Map

| File | Task | Change |
|---|---|---|
| `backtesting/simulation.py` | 1 | Add `fill_delay_bars` param; add `total_volume_traded` to `SimState`; accumulate volume on every fill |
| `backtesting/metrics.py` | 2 | Add `turnover()`, `avg_holding_period()`; extend `compute_all()` signature |
| `backtesting/analysis.py` | 3 | NEW — `regime_performance()`, `confidence_bucket_performance()`, `exposure_by_regime()` |
| `backtesting/stress_test.py` | 4 | NEW — `StressScenario`, five factory functions, three price-transform functions, `apply_stress_scenario()`, `DEFAULT_STRESS_SCENARIOS` |
| `backtesting/walk_forward.py` | 5 | Add `stress_scenarios` param; extend `WalkForwardWindow`; call benchmarks, analysis, stress per window; extend `_aggregate()` |
| `tests/test_simulation.py` | 1, 5 | Add 3 simulation tests; add 3 walk-forward integration tests |
| `tests/test_metrics.py` | 2 | Add 6 metric tests |
| `tests/test_walk_forward_analysis.py` | 3 | NEW — 6 analysis tests |
| `tests/test_stress_test.py` | 4 | NEW — 8 stress-test tests |

---

## Task 1: Simulation — fill delay + volume tracking

**Context:** `simulate_portfolio` uses a single `slippage_bps` for all fills. `SimState` has no volume counter. This task adds `fill_delay_bars` (queue signals, execute N bars later) and `total_volume_traded` (sum of all fill values). When `fill_delay_bars=0` the behaviour is byte-for-byte identical to today.

**Files:**
- Modify: `backtesting/simulation.py`
- Modify: `tests/test_simulation.py`

- [ ] **Step 1: Write three failing tests**

Add at the bottom of `tests/test_simulation.py`:

```python
def test_total_volume_traded_positive_after_trade():
    price_data = {"AAPL": _price_series(100, 0.001, 50)}
    signals = [{"date": "2020-01-02", "ticker": "AAPL",
                "conviction": 7, "position_pct": 10.0, "regime_label": "bull"}]
    sim = simulate_portfolio(signals, price_data, initial_cash=100_000,
                             slippage_bps=0, commission_pct=0)
    # open + close → two fills → volume > 0
    assert sim.total_volume_traded > 0


def test_fill_delay_bars_defers_entry():
    dates = pd.date_range("2020-01-01", periods=20, freq="B")
    prices = pd.Series([100.0] * 20, index=dates)
    # Signal fires on dates[0]; with delay=2 the entry should land on dates[2]
    signals = [{"date": str(dates[0].date()), "ticker": "AAPL",
                "conviction": 7, "position_pct": 10.0, "regime_label": "bull"}]
    sim = simulate_portfolio(signals, {"AAPL": prices}, initial_cash=100_000,
                             fill_delay_bars=2, slippage_bps=0, commission_pct=0,
                             trailing_stop_pct=9999.0, take_profit_pct=9999.0)
    assert len(sim.trades) >= 1
    assert sim.trades[0].entry_date == str(dates[2].date())


def test_fill_delay_zero_unchanged_behaviour():
    price_data = {"AAPL": _price_series(100, 0.002, 50)}
    signals = [{"date": "2020-01-02", "ticker": "AAPL",
                "conviction": 7, "position_pct": 5.0, "regime_label": "bull"}]
    base = simulate_portfolio(signals, price_data, initial_cash=100_000,
                              fill_delay_bars=0, slippage_bps=0, commission_pct=0)
    new  = simulate_portfolio(signals, price_data, initial_cash=100_000,
                              slippage_bps=0, commission_pct=0)  # default fill_delay_bars=0
    # Equity curves must be identical
    assert base.equity_curve == new.equity_curve
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_simulation.py::test_total_volume_traded_positive_after_trade tests/test_simulation.py::test_fill_delay_bars_defers_entry tests/test_simulation.py::test_fill_delay_zero_unchanged_behaviour -v
```

Expected: 3 FAILED (`SimState` has no `total_volume_traded`; `simulate_portfolio` has no `fill_delay_bars`)

- [ ] **Step 3: Extend `SimState` and `simulate_portfolio` in `backtesting/simulation.py`**

**3a.** Add `total_volume_traded` to `SimState`:

Find:
```python
@dataclass
class SimState:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
```

Replace with:
```python
@dataclass
class SimState:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    total_volume_traded: float = 0.0
```

**3b.** Add `fill_delay_bars` parameter to `simulate_portfolio`. Find the function signature:

```python
def simulate_portfolio(
    signals: list[dict],        # list of {date, ticker, conviction, position_pct, regime_label}
    price_data: dict[str, pd.Series],  # ticker → daily close price series
    initial_cash: float = 100_000.0,
    slippage_bps: float = 10.0,
    commission_pct: float = 0.05,
    max_positions: int = 20,
    max_position_pct: float = 8.0,
    trailing_stop_pct: float = 15.0,
    take_profit_pct: float = 25.0,
) -> SimState:
```

Replace with:

```python
def simulate_portfolio(
    signals: list[dict],
    price_data: dict[str, pd.Series],
    initial_cash: float = 100_000.0,
    slippage_bps: float = 10.0,
    commission_pct: float = 0.05,
    max_positions: int = 20,
    max_position_pct: float = 8.0,
    trailing_stop_pct: float = 15.0,
    take_profit_pct: float = 25.0,
    fill_delay_bars: int = 0,
) -> SimState:
```

**3c.** Replace the signal-bucketing logic at the top of the function body. Find:

```python
    state = SimState(cash=initial_cash)
    signal_by_date: dict[str, list[dict]] = {}
    for sig in signals:
        signal_by_date.setdefault(sig["date"], []).append(sig)

    # Determine date range from price_data
    all_dates = sorted({str(d.date()) for series in price_data.values()
                        for d in series.index})
```

Replace with:

```python
    state = SimState(cash=initial_cash)

    # Determine date range from price_data
    all_dates = sorted({str(d.date()) for series in price_data.values()
                        for d in series.index})

    # Build pending execution queue (handles fill_delay_bars)
    date_to_idx: dict[str, int] = {d: i for i, d in enumerate(all_dates)}
    pending: dict[str, list[dict]] = {}
    for sig in signals:
        sig_date = sig["date"]
        idx = date_to_idx.get(sig_date)
        if idx is None:
            continue
        exec_idx = min(idx + fill_delay_bars, len(all_dates) - 1)
        pending.setdefault(all_dates[exec_idx], []).append(sig)
```

**3d.** Inside the main loop, replace `signal_by_date` with `pending`. Find:

```python
        day_signals = signal_by_date.get(day_str, [])
```

Replace with:

```python
        day_signals = pending.get(day_str, [])
```

**3e.** Accumulate volume on position open. In the "Open new positions" block, after:

```python
            shares = cost_basis / fill_price
            state.cash -= total_cost
            state.positions[ticker] = {
```

Add immediately before `state.positions[ticker] = {`:

```python
            state.total_volume_traded += shares * fill_price
```

**3f.** Accumulate volume on position close in `_close_position`. Find:

```python
    pos = state.positions.pop(ticker)
    fill_price = _apply_slippage(current_price, "sell", slippage_bps)
    gross = pos["shares"] * fill_price
    commission = _apply_commission(gross, commission_pct)
    proceeds = gross - commission
    state.cash += proceeds
```

Add `state.total_volume_traded += gross` after `gross = pos["shares"] * fill_price`:

```python
    pos = state.positions.pop(ticker)
    fill_price = _apply_slippage(current_price, "sell", slippage_bps)
    gross = pos["shares"] * fill_price
    state.total_volume_traded += gross
    commission = _apply_commission(gross, commission_pct)
    proceeds = gross - commission
    state.cash += proceeds
```

- [ ] **Step 4: Run the three new tests**

```bash
cd "trading bot" && python3 -m pytest tests/test_simulation.py::test_total_volume_traded_positive_after_trade tests/test_simulation.py::test_fill_delay_bars_defers_entry tests/test_simulation.py::test_fill_delay_zero_unchanged_behaviour -v
```

Expected: 3 PASSED

- [ ] **Step 5: Run the full simulation test file**

```bash
cd "trading bot" && python3 -m pytest tests/test_simulation.py -v
```

Expected: all green

- [ ] **Step 6: Run full suite for regressions**

```bash
cd "trading bot" && python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
cd "trading bot" && git add backtesting/simulation.py tests/test_simulation.py && git commit -m "$(cat <<'EOF'
feat: add fill_delay_bars and total_volume_traded to portfolio simulation

fill_delay_bars queues signals and executes them N bars later using that
bar's price (default 0 = immediate, backward-compatible). total_volume_traded
accumulates buy + sell fill values in SimState for downstream turnover calc.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Metrics — turnover and average holding period

**Context:** `compute_all()` in `backtesting/metrics.py` currently takes `(equity, trade_returns)`. This task adds two new standalone functions and two new keyword-only parameters to `compute_all`. All existing callers continue to work unchanged (both new params default to `None`).

**Files:**
- Modify: `backtesting/metrics.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Write six failing tests**

Add at the bottom of `tests/test_metrics.py`:

```python
from backtesting.metrics import turnover, avg_holding_period
from backtesting.simulation import SimTrade


def _make_trade(entry="2020-01-02", exit_="2020-01-22",
                pnl_pct=5.0, regime="bull", conviction=7):
    return SimTrade(
        ticker="AAPL",
        entry_date=entry,
        exit_date=exit_,
        entry_price=100.0,
        exit_price=100.0 * (1 + pnl_pct / 100),
        shares=10.0,
        pnl=pnl_pct * 10,
        pnl_pct=pnl_pct,
        regime_at_entry=regime,
        conviction=conviction,
        exit_reason="take_profit",
    )


def test_turnover_zero_when_no_volume():
    eq = _equity([100_000.0] * 10)
    assert turnover(0.0, eq) == pytest.approx(0.0)


def test_turnover_ratio():
    # avg NAV = 100_000; traded 50_000 → turnover = 0.5
    eq = _equity([100_000.0] * 10)
    assert turnover(50_000.0, eq) == pytest.approx(0.5, rel=1e-3)


def test_avg_holding_period_empty():
    assert avg_holding_period([]) == pytest.approx(0.0)


def test_avg_holding_period_single_trade():
    # entry 2020-01-02, exit 2020-01-12 → 10 calendar days
    t = _make_trade(entry="2020-01-02", exit_="2020-01-12")
    assert avg_holding_period([t]) == pytest.approx(10.0)


def test_compute_all_includes_turnover_when_volume_provided():
    eq = _equity([100_000.0, 101_000.0, 102_000.0])
    result = compute_all(eq, total_volume_traded=50_000.0)
    assert "turnover" in result
    assert result["turnover"] is not None


def test_compute_all_turnover_none_when_not_provided():
    eq = _equity([100_000.0, 101_000.0])
    result = compute_all(eq)
    assert result.get("turnover") is None
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_metrics.py::test_turnover_zero_when_no_volume tests/test_metrics.py::test_turnover_ratio tests/test_metrics.py::test_avg_holding_period_empty tests/test_metrics.py::test_avg_holding_period_single_trade tests/test_metrics.py::test_compute_all_includes_turnover_when_volume_provided tests/test_metrics.py::test_compute_all_turnover_none_when_not_provided -v
```

Expected: 6 FAILED (ImportError: cannot import `turnover`, `avg_holding_period`)

- [ ] **Step 3: Add `turnover` and `avg_holding_period` to `backtesting/metrics.py`**

Add these two functions immediately before `compute_all`:

```python
def turnover(total_volume_traded: float, equity: pd.Series) -> float:
    """Total traded value divided by average NAV."""
    if equity.empty:
        return 0.0
    avg_nav = float(equity.mean())
    if avg_nav == 0:
        return 0.0
    return total_volume_traded / avg_nav


def avg_holding_period(trades: list) -> float:
    """Mean calendar days between entry_date and exit_date across all trades."""
    from datetime import date as _date
    if not trades:
        return 0.0
    days = []
    for t in trades:
        try:
            entry = _date.fromisoformat(t.entry_date)
            exit_ = _date.fromisoformat(t.exit_date)
            days.append((exit_ - entry).days)
        except (ValueError, AttributeError):
            pass
    return float(sum(days) / len(days)) if days else 0.0
```

- [ ] **Step 4: Extend `compute_all` signature and body**

Find:

```python
def compute_all(equity: pd.Series, trade_returns: pd.Series | None = None) -> dict:
    """Compute the full metrics suite."""
    tr = trade_returns if trade_returns is not None else pd.Series(dtype=float)
    return {
        "total_return_pct": round(total_return(equity) * 100, 2),
        "annualized_return_pct": round(annualized_return(equity) * 100, 2),
        "annualized_vol_pct": round(annualized_volatility(equity) * 100, 2),
        "sharpe": round(sharpe_ratio(equity), 3),
        "sortino": round(sortino_ratio(equity), 3),
        "calmar": round(calmar_ratio(equity), 3),
        "max_drawdown_pct": round(max_drawdown(equity) * 100, 2),
        "win_rate": round(win_rate(tr), 3),
        "profit_factor": round(profit_factor(tr), 3),
        "avg_trade_return_pct": round(avg_trade_return(tr) * 100, 3),
        "n_trades": len(tr),
    }
```

Replace with:

```python
def compute_all(
    equity: pd.Series,
    trade_returns: pd.Series | None = None,
    trades: list | None = None,
    total_volume_traded: float | None = None,
) -> dict:
    """Compute the full metrics suite."""
    tr = trade_returns if trade_returns is not None else pd.Series(dtype=float)
    result = {
        "total_return_pct": round(total_return(equity) * 100, 2),
        "annualized_return_pct": round(annualized_return(equity) * 100, 2),
        "annualized_vol_pct": round(annualized_volatility(equity) * 100, 2),
        "sharpe": round(sharpe_ratio(equity), 3),
        "sortino": round(sortino_ratio(equity), 3),
        "calmar": round(calmar_ratio(equity), 3),
        "max_drawdown_pct": round(max_drawdown(equity) * 100, 2),
        "win_rate": round(win_rate(tr), 3),
        "profit_factor": round(profit_factor(tr), 3),
        "avg_trade_return_pct": round(avg_trade_return(tr) * 100, 3),
        "n_trades": len(tr),
        "avg_holding_period_days": (
            round(avg_holding_period(trades), 1) if trades is not None else None
        ),
        "turnover": (
            round(turnover(total_volume_traded, equity), 4)
            if total_volume_traded is not None else None
        ),
    }
    return result
```

- [ ] **Step 5: Run the six new tests**

```bash
cd "trading bot" && python3 -m pytest tests/test_metrics.py -v
```

Expected: all green (including the 12 existing tests)

- [ ] **Step 6: Run full suite for regressions**

```bash
cd "trading bot" && python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
cd "trading bot" && git add backtesting/metrics.py tests/test_metrics.py && git commit -m "$(cat <<'EOF'
feat: add turnover and avg_holding_period metrics

turnover = total_volume_traded / avg_NAV.
avg_holding_period = mean calendar days entry→exit across trades.
compute_all() gains optional trades and total_volume_traded kwargs;
both default to None and are backward-compatible.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: New `backtesting/analysis.py`

**Context:** `SimTrade` already stores `regime_at_entry` and `conviction`. `RegimeState` already stores `regime_label`. This task adds three pure analysis functions that group and aggregate that existing data. No I/O, no DB, no side effects.

**Files:**
- Create: `backtesting/analysis.py`
- Create: `tests/test_walk_forward_analysis.py`

- [ ] **Step 1: Write six failing tests**

Create `tests/test_walk_forward_analysis.py`:

```python
"""Tests for backtesting.analysis — regime/confidence/exposure breakdown."""
from unittest.mock import MagicMock
import pytest
from backtesting.simulation import SimTrade


def _trade(regime="bull", conviction=7, pnl_pct=5.0,
           entry="2020-01-02", exit_="2020-01-12"):
    return SimTrade(
        ticker="AAPL",
        entry_date=entry,
        exit_date=exit_,
        entry_price=100.0,
        exit_price=100.0 * (1 + pnl_pct / 100),
        shares=10.0,
        pnl=pnl_pct * 10,
        pnl_pct=pnl_pct,
        regime_at_entry=regime,
        conviction=conviction,
        exit_reason="take_profit",
    )


def test_regime_performance_groups_by_label():
    from backtesting.analysis import regime_performance
    trades = [_trade("bull", pnl_pct=5.0), _trade("bear", pnl_pct=-3.0)]
    result = regime_performance(trades)
    assert "bull" in result and "bear" in result
    assert result["bull"]["n_trades"] == 1
    assert result["bear"]["n_trades"] == 1


def test_regime_performance_win_rate():
    from backtesting.analysis import regime_performance
    trades = [_trade("bull", pnl_pct=5.0), _trade("bull", pnl_pct=-2.0)]
    result = regime_performance(trades)
    assert result["bull"]["win_rate"] == pytest.approx(0.5)


def test_regime_performance_empty():
    from backtesting.analysis import regime_performance
    assert regime_performance([]) == {}


def test_confidence_bucket_splits_correctly():
    from backtesting.analysis import confidence_bucket_performance
    trades = [_trade(conviction=3), _trade(conviction=6), _trade(conviction=9)]
    result = confidence_bucket_performance(trades)
    assert result["low"]["n_trades"] == 1
    assert result["mid"]["n_trades"] == 1
    assert result["high"]["n_trades"] == 1


def test_exposure_by_regime_sums_to_one():
    from backtesting.analysis import exposure_by_regime
    states = [MagicMock(regime_label="bull")] * 3 + [MagicMock(regime_label="bear")] * 1
    result = exposure_by_regime(states)
    assert abs(sum(result.values()) - 1.0) < 1e-6
    assert result["bull"] == pytest.approx(0.75)


def test_exposure_by_regime_empty_returns_empty():
    from backtesting.analysis import exposure_by_regime
    assert exposure_by_regime([]) == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_walk_forward_analysis.py -v
```

Expected: 6 FAILED (ModuleNotFoundError: `backtesting.analysis`)

- [ ] **Step 3: Create `backtesting/analysis.py`**

```python
"""Regime and confidence breakdown analysis for walk-forward results.

Three pure functions — no I/O, no DB. Each takes a list of SimTrade or
RegimeState objects (already attached to every WalkForwardWindow) and
returns a plain dict suitable for JSON serialisation and DB storage.
"""
from __future__ import annotations

from typing import Any


def _bucket_stats(trades: list) -> dict:
    """Shared helper: compute stats for a group of SimTrade objects."""
    if not trades:
        return {}
    returns = [t.pnl_pct / 100 for t in trades]
    wins = sum(1 for r in returns if r > 0)
    gains = sum(r for r in returns if r > 0)
    losses = sum(abs(r) for r in returns if r < 0)
    pf = (gains / losses) if losses > 0 else (float("inf") if gains > 0 else 0.0)
    return {
        "n_trades": len(trades),
        "win_rate": round(wins / len(trades), 3),
        "avg_return_pct": round(sum(returns) / len(returns) * 100, 3),
        "profit_factor": round(pf, 3),
    }


def regime_performance(trades: list) -> dict[str, dict]:
    """Group SimTrades by regime_at_entry.

    Returns {label: {n_trades, win_rate, avg_return_pct, profit_factor}}.
    Labels with zero trades are omitted.
    """
    by_regime: dict[str, list] = {}
    for t in trades:
        by_regime.setdefault(t.regime_at_entry, []).append(t)
    return {label: _bucket_stats(group) for label, group in by_regime.items()}


def confidence_bucket_performance(
    trades: list,
    low_max: int = 5,
    high_min: int = 8,
) -> dict[str, dict]:
    """Bucket trades by conviction score.

    low  = conviction in [1, low_max]
    mid  = conviction in (low_max, high_min)
    high = conviction in [high_min, 10]

    Returns {bucket: {n_trades, win_rate, avg_return_pct, profit_factor}}.
    Empty buckets are omitted.
    """
    buckets: dict[str, list] = {"low": [], "mid": [], "high": []}
    for t in trades:
        if t.conviction <= low_max:
            buckets["low"].append(t)
        elif t.conviction >= high_min:
            buckets["high"].append(t)
        else:
            buckets["mid"].append(t)
    return {k: _bucket_stats(v) for k, v in buckets.items() if v}


def exposure_by_regime(regime_states: list[Any]) -> dict[str, float]:
    """Fraction of test-period bars classified as each regime label.

    Returns {label: fraction} summing to 1.0. Returns {} for empty input.
    """
    if not regime_states:
        return {}
    counts: dict[str, int] = {}
    for s in regime_states:
        counts[s.regime_label] = counts.get(s.regime_label, 0) + 1
    total = len(regime_states)
    return {label: round(count / total, 4) for label, count in counts.items()}
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_walk_forward_analysis.py -v
```

Expected: 6 PASSED

- [ ] **Step 5: Run full suite for regressions**

```bash
cd "trading bot" && python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add backtesting/analysis.py tests/test_walk_forward_analysis.py && git commit -m "$(cat <<'EOF'
feat: add analysis.py — regime/confidence/exposure breakdown

regime_performance() groups SimTrades by regime_at_entry.
confidence_bucket_performance() buckets by conviction (low≤5, mid 6-7, high≥8).
exposure_by_regime() computes fraction of test-period bars per regime label.
All functions are pure — no I/O.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: New `backtesting/stress_test.py`

**Context:** Stress scenarios apply deterministic transforms to price data and/or override simulation parameters. The module is standalone — no imports from other new files. `apply_stress_scenario` is the single interface that `walk_forward.py` calls.

**Files:**
- Create: `backtesting/stress_test.py`
- Create: `tests/test_stress_test.py`

- [ ] **Step 1: Write eight failing tests**

Create `tests/test_stress_test.py`:

```python
"""Tests for backtesting.stress_test scenario generation and transforms."""
import numpy as np
import pandas as pd
import pytest


def _price_series(n=50, start=100.0, daily_ret=0.001):
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    prices = [start * (1 + daily_ret) ** i for i in range(n)]
    return pd.Series(prices, index=dates, name="SPY")


def test_sudden_crash_factory_fields():
    from backtesting.stress_test import sudden_crash
    s = sudden_crash(drop_pct=0.20, duration_days=3)
    assert s.name == "sudden_crash"
    assert s.crash_pct == pytest.approx(0.20)
    assert s.crash_duration_days == 3


def test_crash_reduces_tail_prices():
    from backtesting.stress_test import _apply_crash
    series = _price_series(30)
    result = _apply_crash({"SPY": series}, drop_pct=0.30, start_idx=5, duration=5)
    # Tail prices should be ~70 % of where they would have been
    assert result["SPY"].iloc[-1] < series.iloc[-1] * 0.90


def test_vol_cluster_increases_return_variance():
    from backtesting.stress_test import _apply_vol_cluster
    rng = np.random.default_rng(0)
    n = 80
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    prices = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=dates)
    result = _apply_vol_cluster({"SPY": prices}, multiplier=3.0, start_idx=20, duration=30)
    original_std = prices.pct_change().iloc[20:50].std()
    stressed_std = result["SPY"].pct_change().iloc[20:50].std()
    assert stressed_std > original_std * 1.5


def test_drop_random_bars_reduces_length():
    from backtesting.stress_test import _drop_random_bars
    series = _price_series(100)
    result = _drop_random_bars({"SPY": series}, fraction=0.10, seed=42)
    assert len(result["SPY"]) < 100
    assert len(result["SPY"]) >= 85


def test_apply_stress_scenario_slippage_multiplier():
    from backtesting.stress_test import apply_stress_scenario, slippage_spike
    _, stressed_slip, _ = apply_stress_scenario({}, 10.0, slippage_spike(multiplier=3.0))
    assert stressed_slip == pytest.approx(30.0)


def test_apply_stress_scenario_fill_delay():
    from backtesting.stress_test import apply_stress_scenario, delayed_fills
    _, _, delay = apply_stress_scenario({}, 10.0, delayed_fills(delay_bars=2))
    assert delay == 2


def test_apply_stress_scenario_crash_modifies_prices():
    from backtesting.stress_test import apply_stress_scenario, sudden_crash
    series = _price_series(40)
    stressed_prices, _, _ = apply_stress_scenario(
        {"SPY": series}, 10.0, sudden_crash(drop_pct=0.30, duration_days=5)
    )
    assert stressed_prices["SPY"].iloc[-1] < series.iloc[-1] * 0.90


def test_default_stress_scenarios_has_five_distinct_names():
    from backtesting.stress_test import DEFAULT_STRESS_SCENARIOS
    assert len(DEFAULT_STRESS_SCENARIOS) == 5
    names = {s.name for s in DEFAULT_STRESS_SCENARIOS}
    assert names == {"sudden_crash", "high_vol_cluster", "slippage_spike",
                     "delayed_fills", "missing_data"}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_stress_test.py -v
```

Expected: 8 FAILED (ModuleNotFoundError: `backtesting.stress_test`)

- [ ] **Step 3: Create `backtesting/stress_test.py`**

```python
"""Stress scenario generation for walk-forward backtesting.

Each scenario is a frozen dataclass. Factory functions create common presets.
apply_stress_scenario() is the single interface used by walk_forward.py —
it returns modified price data and simulation parameter overrides.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StressScenario:
    name: str
    # Price-level transforms
    crash_pct: float | None = None
    crash_duration_days: int = 5
    vol_multiplier: float | None = None
    vol_duration_days: int = 20
    drop_bar_fraction: float | None = None
    drop_bar_seed: int = 42
    # Simulation-level overrides
    slippage_multiplier: float = 1.0
    fill_delay_bars: int = 0


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def sudden_crash(drop_pct: float = 0.30, duration_days: int = 5) -> StressScenario:
    return StressScenario(name="sudden_crash", crash_pct=drop_pct,
                          crash_duration_days=duration_days)


def high_vol_cluster(vol_mult: float = 3.0, duration_days: int = 20) -> StressScenario:
    return StressScenario(name="high_vol_cluster", vol_multiplier=vol_mult,
                          vol_duration_days=duration_days)


def slippage_spike(multiplier: float = 5.0) -> StressScenario:
    return StressScenario(name="slippage_spike", slippage_multiplier=multiplier)


def delayed_fills(delay_bars: int = 2) -> StressScenario:
    return StressScenario(name="delayed_fills", fill_delay_bars=delay_bars)


def missing_data(drop_fraction: float = 0.05, seed: int = 42) -> StressScenario:
    return StressScenario(name="missing_data", drop_bar_fraction=drop_fraction,
                          drop_bar_seed=seed)


DEFAULT_STRESS_SCENARIOS: list[StressScenario] = [
    sudden_crash(),
    high_vol_cluster(),
    slippage_spike(),
    delayed_fills(),
    missing_data(),
]


# ---------------------------------------------------------------------------
# Price transform functions
# ---------------------------------------------------------------------------

def _apply_crash(
    price_data: dict[str, pd.Series],
    drop_pct: float,
    start_idx: int,
    duration: int,
) -> dict[str, pd.Series]:
    """Log-linear decline of drop_pct over duration bars starting at start_idx.

    Prices after the crash window stay at the new (lower) level — no recovery.
    """
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty:
            result[ticker] = series.copy()
            continue
        prices = series.to_numpy(dtype=float).copy()
        n = len(prices)
        end_idx = min(start_idx + duration, n)
        n_bars = end_idx - start_idx
        if n_bars > 0:
            # Each bar within the window is multiplied by (1-drop_pct)^(k/n_bars)
            for k in range(n_bars):
                prices[start_idx + k] *= (1 - drop_pct) ** ((k + 1) / n_bars)
            # Everything after the window stays at the fully-crashed level
            prices[end_idx:] *= (1 - drop_pct)
        result[ticker] = pd.Series(prices, index=series.index, name=series.name)
    return result


def _apply_vol_cluster(
    price_data: dict[str, pd.Series],
    multiplier: float,
    start_idx: int,
    duration: int,
) -> dict[str, pd.Series]:
    """Amplify daily log-returns by multiplier within [start_idx, start_idx+duration)."""
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty or len(series) < 2:
            result[ticker] = series.copy()
            continue
        prices = series.to_numpy(dtype=float).copy()
        n = len(prices)
        end_idx = min(start_idx + duration, n)
        for i in range(max(1, start_idx), end_idx):
            prev = prices[i - 1]
            if prev <= 0:
                continue
            log_ret = np.log(prices[i] / prev)
            prices[i] = prev * np.exp(log_ret * multiplier)
        result[ticker] = pd.Series(prices, index=series.index, name=series.name)
    return result


def _drop_random_bars(
    price_data: dict[str, pd.Series],
    fraction: float,
    seed: int,
) -> dict[str, pd.Series]:
    """Remove a random fraction of bars from every series (simulates missing data)."""
    rng = np.random.default_rng(seed)
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty:
            result[ticker] = series.copy()
            continue
        n = len(series)
        n_drop = max(0, int(n * fraction))
        if n_drop == 0:
            result[ticker] = series.copy()
            continue
        drop_idx = rng.choice(n, size=n_drop, replace=False)
        mask = np.ones(n, dtype=bool)
        mask[drop_idx] = False
        result[ticker] = series.iloc[mask].copy()
    return result


# ---------------------------------------------------------------------------
# Main interface
# ---------------------------------------------------------------------------

def apply_stress_scenario(
    price_data: dict[str, pd.Series],
    base_slippage_bps: float,
    scenario: StressScenario,
) -> tuple[dict[str, pd.Series], float, int]:
    """Apply a scenario to price data and return modified simulation inputs.

    Returns
    -------
    (stressed_price_data, stressed_slippage_bps, fill_delay_bars)
    """
    stressed: dict[str, pd.Series] = {t: s.copy() for t, s in price_data.items()}

    # Crash and vol cluster start a quarter of the way into the test window
    n_bars = max((len(s) for s in stressed.values()), default=0)
    start_idx = max(1, n_bars // 4)

    if scenario.crash_pct is not None:
        stressed = _apply_crash(stressed, scenario.crash_pct,
                                start_idx, scenario.crash_duration_days)
    if scenario.vol_multiplier is not None:
        stressed = _apply_vol_cluster(stressed, scenario.vol_multiplier,
                                      start_idx, scenario.vol_duration_days)
    if scenario.drop_bar_fraction is not None:
        stressed = _drop_random_bars(stressed, scenario.drop_bar_fraction,
                                     scenario.drop_bar_seed)

    return stressed, base_slippage_bps * scenario.slippage_multiplier, scenario.fill_delay_bars
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd "trading bot" && python3 -m pytest tests/test_stress_test.py -v
```

Expected: 8 PASSED

- [ ] **Step 5: Run full suite for regressions**

```bash
cd "trading bot" && python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all green

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add backtesting/stress_test.py tests/test_stress_test.py && git commit -m "$(cat <<'EOF'
feat: add stress_test.py with five scenario types

StressScenario frozen dataclass + factory functions: sudden_crash,
high_vol_cluster, slippage_spike, delayed_fills, missing_data.
apply_stress_scenario() returns modified price_data + slippage_bps +
fill_delay_bars for drop-in use in walk_forward loop.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire everything into `walk_forward.py`

**Context:** `run_walk_forward` needs four additions after the existing base simulation: (1) benchmark equity curves, (2) regime/confidence analysis, (3) stress runs, (4) extended `compute_all` call with volume and trades. `WalkForwardWindow` gains five new fields. `_aggregate()` gains benchmark comparison, stress aggregation, and regime exposure averaging.

**Files:**
- Modify: `backtesting/walk_forward.py`
- Modify: `tests/test_simulation.py`

- [ ] **Step 1: Write three failing tests**

Add at the bottom of `tests/test_simulation.py`:

```python
def _make_wf_result(stress_scenarios=None):
    """Run a minimal walk-forward and return the result."""
    import numpy as np
    import pandas as pd
    from dataclasses import dataclass, field
    from backtesting.walk_forward import run_walk_forward
    from features.feature_pipeline import FeatureConfig

    @dataclass
    class _RCfg:
        candidate_counts: tuple = (3,)
        selection_criterion: str = "bic"
        n_iter: int = 10
        random_state: int = 42
        covariance_type: str = "diag"
        min_stable_bars: int = 2
        instability_penalty: float = 0.5
        label_maps: dict = field(default_factory=lambda: {3: ["bear", "neutral", "bull"]})
        model_path: str = "test_wf_stress.joblib"

    @dataclass
    class _BCfg:
        train_years: int = 1
        test_months: int = 3
        step_months: int = 3
        slippage_bps: float = 0.0
        commission_pct: float = 0.0
        benchmark_ticker: str = "SPY"
        min_train_bars: int = 100

    rng = np.random.default_rng(7)
    n = 700
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
    market_data = pd.DataFrame({
        "close": close,
        "volume": rng.integers(1_000_000, 10_000_000, n).astype(float),
        "vix": np.clip(15 + rng.normal(0, 3, n), 10, 50),
    }, index=dates)

    return run_walk_forward(
        market_data=market_data,
        signal_data=[],
        price_data={},
        regime_cfg=_RCfg(),
        backtest_cfg=_BCfg(),
        feature_cfg=FeatureConfig(vol_window=20, trend_window=50,
                                   min_history_bars=100, use_vix=False),
        persist_to_db=False,
        stress_scenarios=stress_scenarios,
    )


def test_walk_forward_window_has_benchmarks():
    result = _make_wf_result(stress_scenarios=[])
    assert result.windows, "Expected at least one window"
    w = result.windows[0]
    assert hasattr(w, "benchmarks")
    assert isinstance(w.benchmarks, dict)
    # buy_and_hold key present (market_data has close prices)
    assert "buy_and_hold" in w.benchmarks


def test_walk_forward_window_has_regime_exposure():
    result = _make_wf_result(stress_scenarios=[])
    w = result.windows[0]
    assert hasattr(w, "regime_exposure")
    assert isinstance(w.regime_exposure, dict)
    # Exposure fractions should sum to ~1 if there are regime states
    if w.regime_exposure:
        assert abs(sum(w.regime_exposure.values()) - 1.0) < 0.01


def test_walk_forward_empty_stress_list_produces_no_stress_results():
    result = _make_wf_result(stress_scenarios=[])
    w = result.windows[0]
    assert hasattr(w, "stress_results")
    assert w.stress_results == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "trading bot" && python3 -m pytest tests/test_simulation.py::test_walk_forward_window_has_benchmarks tests/test_simulation.py::test_walk_forward_window_has_regime_exposure tests/test_simulation.py::test_walk_forward_empty_stress_list_produces_no_stress_results -v
```

Expected: 3 FAILED (`WalkForwardWindow` has no `benchmarks`, `regime_exposure`, `stress_results`)

- [ ] **Step 3: Extend `WalkForwardWindow` dataclass**

Find:

```python
@dataclass
class WalkForwardWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_regimes: int
    metrics: dict = field(default_factory=dict)
    regime_states: list[RegimeState] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
```

Replace with:

```python
@dataclass
class WalkForwardWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_regimes: int
    metrics: dict = field(default_factory=dict)
    regime_states: list[RegimeState] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    benchmarks: dict = field(default_factory=dict)
    regime_breakdown: dict = field(default_factory=dict)
    confidence_breakdown: dict = field(default_factory=dict)
    regime_exposure: dict = field(default_factory=dict)
    stress_results: dict = field(default_factory=dict)
```

- [ ] **Step 4: Add new imports at the top of `walk_forward.py`**

Find the existing imports block. Add these five lines after `from backtesting.simulation import (`:

```python
from backtesting.analysis import (
    regime_performance, confidence_bucket_performance, exposure_by_regime,
)
from backtesting.benchmarks import buy_and_hold, trend_following, random_allocation
from backtesting.metrics import total_return as _total_return
from backtesting.stress_test import DEFAULT_STRESS_SCENARIOS, apply_stress_scenario
```

The full existing imports block (for reference — do not change existing lines):

```python
import bot.db as db
from backtesting.metrics import compute_all
from backtesting.simulation import (
    equity_series, simulate_portfolio, trade_returns
)
from features.feature_pipeline import FeatureConfig
from regime.hmm_engine import HMMRegimeEngine, RegimeState
```

Add the four new imports below `from regime.hmm_engine import HMMRegimeEngine, RegimeState`.

- [ ] **Step 5: Add `stress_scenarios` parameter to `run_walk_forward`**

Find:

```python
def run_walk_forward(
    market_data: pd.DataFrame,       # full history of market bars (for regime fitting)
    signal_data: list[dict],          # list of {date, ticker, conviction, position_pct}
    price_data: dict[str, pd.Series], # ticker → daily close prices (for simulation)
    regime_cfg: Any,                  # system.config.RegimeConfig
    backtest_cfg: Any,                # system.config.BacktestConfig
    feature_cfg: FeatureConfig | None = None,
    alloc_cfg: Any = None,
    persist_to_db: bool = True,
) -> WalkForwardResult:
```

Replace with:

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
    stress_scenarios: list | None = None,
) -> WalkForwardResult:
```

- [ ] **Step 6: Replace the per-window simulation block**

Find the section from after `# --- Simulate portfolio ---` through to the end of the `all_results.append(window_result)` block. Replace the entire block from `# --- Simulate portfolio ---` to `all_results.append(window_result)` (inclusive) with:

```python
        # --- Simulate portfolio ---
        test_price_data = {
            ticker: series.loc[
                (series.index >= pd.Timestamp(test_start)) &
                (series.index <= pd.Timestamp(test_end))
            ]
            for ticker, series in price_data.items()
        }
        sim = simulate_portfolio(
            signals=enriched_signals,
            price_data=test_price_data,
            initial_cash=100_000.0,
            slippage_bps=backtest_cfg.slippage_bps,
            commission_pct=backtest_cfg.commission_pct,
        )

        eq = equity_series(sim)
        tr = trade_returns(sim)
        metrics = compute_all(
            eq, tr,
            trades=sim.trades,
            total_volume_traded=sim.total_volume_traded,
        )
        metrics["n_regimes"] = engine.n_regimes

        # --- Benchmarks ---
        benchmarks: dict = {}
        try:
            spy_close = market_data["close"].loc[
                (market_data.index >= pd.Timestamp(test_start)) &
                (market_data.index <= pd.Timestamp(test_end))
            ]
            if not spy_close.empty:
                bah_eq = buy_and_hold(spy_close)
                tf_eq  = trend_following(spy_close)
                rand_eq = random_allocation(
                    spy_close,
                    n_signals=max(len(enriched_signals), 1),
                )
                benchmarks = {
                    "buy_and_hold":    round(_total_return(bah_eq) * 100, 2),
                    "trend_following": round(_total_return(tf_eq)  * 100, 2),
                    "random":          round(_total_return(rand_eq) * 100, 2),
                }
        except Exception as exc:
            log.warning("Benchmark computation failed for window %d: %s", i + 1, exc)

        # --- Analysis ---
        regime_breakdown    = regime_performance(sim.trades)
        confidence_breakdown = confidence_bucket_performance(sim.trades)
        regime_exposure     = exposure_by_regime(test_states)

        # --- Stress tests ---
        scenarios = (
            stress_scenarios
            if stress_scenarios is not None
            else DEFAULT_STRESS_SCENARIOS
        )
        stress_results: dict = {}
        for scenario in scenarios:
            try:
                s_prices, s_slip, s_delay = apply_stress_scenario(
                    test_price_data, backtest_cfg.slippage_bps, scenario
                )
                s_sim = simulate_portfolio(
                    signals=enriched_signals,
                    price_data=s_prices,
                    initial_cash=100_000.0,
                    slippage_bps=s_slip,
                    commission_pct=backtest_cfg.commission_pct,
                    fill_delay_bars=s_delay,
                )
                s_eq = equity_series(s_sim)
                s_tr = trade_returns(s_sim)
                stress_results[scenario.name] = compute_all(
                    s_eq, s_tr,
                    trades=s_sim.trades,
                    total_volume_traded=s_sim.total_volume_traded,
                )
            except Exception as exc:
                log.warning("Stress scenario '%s' failed (window %d): %s",
                            scenario.name, i + 1, exc)
                stress_results[scenario.name] = {}

        window_result = WalkForwardWindow(
            train_start=str(train_start.date()),
            train_end=str(train_end.date()),
            test_start=test_start,
            test_end=test_end,
            n_regimes=engine.n_regimes,
            metrics=metrics,
            regime_states=test_states,
            benchmarks=benchmarks,
            regime_breakdown=regime_breakdown,
            confidence_breakdown=confidence_breakdown,
            regime_exposure=regime_exposure,
            stress_results=stress_results,
        )
        all_results.append(window_result)
```

- [ ] **Step 7: Replace `_aggregate()`**

Find and replace the entire `_aggregate` function:

```python
def _aggregate(windows: list[WalkForwardWindow]) -> dict:
    if not windows:
        return {}
    metrics_list = [w.metrics for w in windows]
    keys = ["sharpe", "sortino", "max_drawdown_pct", "total_return_pct", "win_rate"]
    result: dict = {}
    for k in keys:
        vals = [m.get(k, 0.0) for m in metrics_list if k in m]
        if vals:
            result[f"avg_{k}"]  = round(float(sum(vals) / len(vals)), 3)
            result[f"min_{k}"]  = round(min(vals), 3)
            result[f"max_{k}"]  = round(max(vals), 3)
    result["n_windows"] = len(windows)

    # Benchmark excess return (strategy total_return minus benchmark total_return per window)
    strat_returns = [m.get("total_return_pct", 0.0) for m in metrics_list]
    for bench in ("buy_and_hold", "trend_following", "random"):
        bench_vals = [w.benchmarks.get(bench, 0.0) for w in windows]
        if bench_vals:
            excess = [s - b for s, b in zip(strat_returns, bench_vals)]
            result[f"avg_excess_return_vs_{bench}_pct"] = round(
                sum(excess) / len(excess), 3
            )

    # Stress scenario aggregation
    scenario_names: set[str] = set()
    for w in windows:
        scenario_names.update(w.stress_results.keys())
    for sname in sorted(scenario_names):
        sharpes = [w.stress_results[sname].get("sharpe", 0.0)
                   for w in windows if sname in w.stress_results]
        mdds = [w.stress_results[sname].get("max_drawdown_pct", 0.0)
                for w in windows if sname in w.stress_results]
        if sharpes:
            result[f"stress_{sname}_avg_sharpe"] = round(
                sum(sharpes) / len(sharpes), 3
            )
        if mdds:
            result[f"stress_{sname}_avg_max_dd_pct"] = round(
                sum(mdds) / len(mdds), 3
            )

    # Regime exposure averaging
    regime_labels: set[str] = set()
    for w in windows:
        regime_labels.update(w.regime_exposure.keys())
    for label in sorted(regime_labels):
        exposures = [w.regime_exposure.get(label, 0.0) for w in windows]
        result[f"avg_exposure_{label}"] = round(
            sum(exposures) / len(exposures), 4
        )

    return result
```

- [ ] **Step 8: Run the three new walk-forward tests**

```bash
cd "trading bot" && python3 -m pytest tests/test_simulation.py::test_walk_forward_window_has_benchmarks tests/test_simulation.py::test_walk_forward_window_has_regime_exposure tests/test_simulation.py::test_walk_forward_empty_stress_list_produces_no_stress_results -v
```

Expected: 3 PASSED

- [ ] **Step 9: Run the full simulation test file**

```bash
cd "trading bot" && python3 -m pytest tests/test_simulation.py -v
```

Expected: all green

- [ ] **Step 10: Run the entire test suite**

```bash
cd "trading bot" && python3 -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: all green — no regressions

- [ ] **Step 11: Commit**

```bash
cd "trading bot" && git add backtesting/walk_forward.py tests/test_simulation.py && git commit -m "$(cat <<'EOF'
feat: wire benchmarks, analysis, and stress tests into walk-forward engine

WalkForwardWindow gains benchmarks, regime_breakdown, confidence_breakdown,
regime_exposure, stress_results fields. run_walk_forward accepts optional
stress_scenarios list (defaults to DEFAULT_STRESS_SCENARIOS; pass [] to skip).
_aggregate() computes benchmark excess return, per-scenario stress metrics,
and averaged regime exposure across windows.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final Verification

- [ ] **Run the complete test suite one last time**

```bash
cd "trading bot" && python3 -m pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: all tests green. The count should be 348 (existing) + 3 + 6 + 6 + 8 + 3 = **374 tests**.

- [ ] **Push**

```bash
cd "trading bot" && git push
```
