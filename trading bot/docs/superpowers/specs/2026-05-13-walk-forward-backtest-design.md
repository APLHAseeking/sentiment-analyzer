# Walk-Forward Backtesting Engine — Design Spec

**Date:** 2026-05-13
**Status:** Approved

---

## Goal

Extend the existing four-file backtesting layer (`simulation.py`, `metrics.py`, `walk_forward.py`, `benchmarks.py`) with the missing pieces: regime/confidence analysis, stress testing integrated per walk-forward window, benchmark wiring, and the remaining metrics (turnover, holding period). No existing code is duplicated or removed.

---

## What Already Exists (Do Not Touch)

| Component | Location | Status |
|---|---|---|
| Portfolio simulation loop | `backtesting/simulation.py` | Keep; extend only |
| Slippage + commission | `simulation.py` | Complete |
| Trailing stop + take-profit | `simulation.py` | Complete |
| `SimTrade` with `regime_at_entry`, `conviction`, `entry_date`, `exit_date` | `simulation.py` | Complete |
| `equity_series()`, `trade_returns()` | `simulation.py` | Complete |
| All core metrics (Sharpe, Sortino, Calmar, max DD, win rate, profit factor, etc.) | `metrics.py` | Complete |
| `compute_all()` | `metrics.py` | Extend only |
| Buy-and-hold, trend-following, random allocation | `benchmarks.py` | Complete; unchanged |
| Rolling window builder | `walk_forward.py` | Complete |
| Per-window HMM retraining on training data only | `walk_forward.py` | Complete |
| Forward-only classification (frozen scaler) | `walk_forward.py` | Complete |
| DB persistence | `walk_forward.py` | Complete |

---

## Architecture

```
backtesting/
  simulation.py      ← add fill_delay_bars, total_volume_traded
  metrics.py         ← add turnover, avg_holding_period; extend compute_all
  analysis.py        ← NEW: regime breakdown, confidence buckets, exposure
  stress_test.py     ← NEW: StressScenario dataclass + 5 scenario factories + apply functions
  walk_forward.py    ← wire benchmarks, analysis, stress per window; extend aggregation
  benchmarks.py      ← unchanged
```

---

## Data Model Changes

### `SimState` (simulation.py)
Add one field:
```python
total_volume_traded: float = 0.0   # sum of all fill values (buy + sell)
```
Incremented by `shares × fill_price` on every open and close.

### `WalkForwardWindow` (walk_forward.py)
Add five fields alongside existing `metrics` and `regime_states`:
```python
benchmarks:           dict[str, float]   # "buy_and_hold"|"trend_following"|"random" → total_return_pct
regime_breakdown:     dict[str, dict]    # label → {n_trades, win_rate, avg_return_pct, profit_factor}
confidence_breakdown: dict[str, dict]    # "low"|"mid"|"high" → same shape
regime_exposure:      dict[str, float]   # label → fraction of test-period bars in that regime
stress_results:       dict[str, dict]    # scenario_name → full metrics dict
```

### `WalkForwardResult.aggregated_metrics` (walk_forward.py)
Gains additional aggregated keys:
- Per-benchmark excess return: `avg_excess_return_vs_bah`, `avg_excess_return_vs_trend`
- Per-stress-scenario avg Sharpe and avg max drawdown
- Aggregated regime exposure across windows

---

## New File: `backtesting/stress_test.py`

### `StressScenario` dataclass
```python
@dataclass(frozen=True)
class StressScenario:
    name: str
    # Price-level transforms (applied to test-period price_data before simulation)
    crash_pct: float | None = None           # fraction drop, e.g. 0.30
    crash_duration_days: int = 5             # bars over which drop occurs
    vol_multiplier: float | None = None      # amplify daily returns, e.g. 3.0
    vol_duration_days: int = 20
    drop_bar_fraction: float | None = None   # fraction of bars to remove, e.g. 0.05
    drop_bar_seed: int = 42
    # Simulation-level overrides
    slippage_multiplier: float = 1.0         # scale slippage_bps, e.g. 5.0
    fill_delay_bars: int = 0
```

### Factory functions (convenience constructors)
```python
sudden_crash(drop_pct=0.30, duration_days=5) -> StressScenario
high_vol_cluster(vol_mult=3.0, duration_days=20) -> StressScenario
slippage_spike(multiplier=5.0) -> StressScenario
delayed_fills(delay_bars=2) -> StressScenario
missing_data(drop_fraction=0.05, seed=42) -> StressScenario
```

### Price transform functions (internal, tested directly)
- `_apply_crash(price_data, drop_pct, start_idx, duration)` — log-linear decline from `start_idx` over `duration` bars; prices after the crash window are re-anchored (no artificial recovery)
- `_apply_vol_cluster(price_data, multiplier, start_idx, duration)` — amplify daily log-returns within window, keep first bar as anchor
- `_drop_random_bars(price_data, fraction, seed)` — remove random subset of bars from each series in the dict

### `apply_stress_scenario(price_data, base_slippage_bps, scenario) -> tuple[dict, float, int]`
Returns `(stressed_price_data, stressed_slippage_bps, fill_delay_bars)` — the three things the simulation loop needs.

### Default stress suite
```python
DEFAULT_STRESS_SCENARIOS: list[StressScenario] = [
    sudden_crash(),
    high_vol_cluster(),
    slippage_spike(),
    delayed_fills(),
    missing_data(),
]
```

---

## `backtesting/simulation.py` Changes

### `simulate_portfolio()` additions
- Add `fill_delay_bars: int = 0` parameter. Signals are queued on receipt date and executed `fill_delay_bars` bars later (using the price on the execution date). A pending queue `dict[str, list[dict]]` maps execution date → signals.
- Increment `state.total_volume_traded` by `shares × fill_price` on every open and `shares × fill_price` on every close.

No other changes. Existing stop-loss, take-profit, and max-positions logic unchanged.

---

## `backtesting/metrics.py` Additions

### New standalone functions

```python
def turnover(total_volume_traded: float, equity: pd.Series) -> float:
    """Total traded value / average NAV."""

def avg_holding_period(trades: list) -> float:
    """Mean calendar days between entry_date and exit_date across all trades."""
```

### `compute_all()` signature change
```python
def compute_all(
    equity: pd.Series,
    trade_returns: pd.Series | None = None,
    trades: list | None = None,           # SimTrade list for holding period
    total_volume_traded: float | None = None,  # for turnover
) -> dict:
```
Adds `"turnover"` and `"avg_holding_period_days"` to the returned dict when data is available; `None` otherwise.

---

## New File: `backtesting/analysis.py`

Three pure functions, no I/O, no DB:

```python
def regime_performance(trades: list[SimTrade]) -> dict[str, dict]:
    """Group SimTrades by regime_at_entry.
    Returns {label: {n_trades, win_rate, avg_return_pct, profit_factor}}.
    Labels with zero trades are omitted."""

def confidence_bucket_performance(
    trades: list[SimTrade],
    low_max: int = 5,
    high_min: int = 8,
) -> dict[str, dict]:
    """Bucket trades by conviction score: low [1-5], mid [6-7], high [8-10].
    Returns {bucket: {n_trades, win_rate, avg_return_pct, profit_factor}}."""

def exposure_by_regime(regime_states: list[RegimeState]) -> dict[str, float]:
    """Fraction of test-period bars classified as each regime label.
    Returns {label: fraction} summing to 1.0."""
```

---

## `backtesting/walk_forward.py` Changes

### `run_walk_forward()` gains one new parameter
```python
stress_scenarios: list[StressScenario] | None = None
```
If `None`, `DEFAULT_STRESS_SCENARIOS` is used. Pass `[]` to skip all stress tests.

### Per-window additions (after base simulation completes)
1. **Benchmarks** — run `buy_and_hold`, `trend_following`, `random_allocation` on the test-period SPY prices; store total_return_pct for each
2. **Analysis** — call `regime_performance()`, `confidence_bucket_performance()`, `exposure_by_regime()` from `analysis.py`; store results in new window fields
3. **Stress runs** — for each scenario in `stress_scenarios`: call `apply_stress_scenario()` to get modified price_data and simulation params; run `simulate_portfolio()`; call `compute_all()`; store in `window.stress_results[scenario.name]`
4. **Extended metrics** — pass `sim.total_volume_traded` and `sim.trades` to `compute_all()` for turnover and holding period

### `_aggregate()` additions
- Benchmark comparison: for each benchmark, compute mean excess return vs. strategy across windows
- Stress aggregation: for each scenario, compute `avg_sharpe` and `avg_max_drawdown_pct` across windows
- Regime exposure: average exposure per regime across windows

---

## Testing Plan

| File | New tests |
|---|---|
| `tests/test_metrics.py` | `turnover()`, `avg_holding_period()`, extended `compute_all()` with trades |
| `tests/test_stress_test.py` | NEW — each factory, each price transform, `apply_stress_scenario()` |
| `tests/test_simulation.py` | `fill_delay_bars` delays fill; `total_volume_traded` accumulates correctly |
| `tests/test_walk_forward_analysis.py` | NEW — `regime_performance()`, `confidence_bucket_performance()`, `exposure_by_regime()` |
| `tests/test_simulation.py` (existing) | Extended — no regressions |

---

## What Is NOT Changing

- `benchmarks.py` — complete, untouched
- DB schema — `log_backtest_result` already accepts arbitrary metrics dict
- `run_bot.py` `--backtest` path — unchanged API; richer results flow through automatically
- All existing tests — must remain green
