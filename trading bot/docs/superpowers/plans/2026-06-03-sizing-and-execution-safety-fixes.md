# Sizing & Execution-Safety Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a critical position-sizing units bug (positions are ~100× too small), align backtest sizing with live, and close several live-paper execution-safety gaps (stop-order lifecycle, reject-safe exits, invested-cap enforcement, regime feature padding).

**Architecture:** Surgical fixes to existing modules. One shared, pure sizing helper (`risk/position_sizing.py`) is used by the live orchestrator and both backtest runners so they size identically. Execution-safety fixes live in `bot/portfolio.py` and `risk/risk_manager.py`. Each task is test-first (TDD) and independently committable.

**Tech Stack:** Python 3.11+, pytest, numpy, pandas, SQLite. Tests run **offline** (no network) — yfinance/Alpaca/scrapers are mocked. Run all commands from **inside `trading bot/`**.

---

## Background (read first)

This plan implements the fixes from the 2026-06-03 code review. The findings, in severity order:

1. **🔴 Sizing units bug** — `vol_target_size_pct` divides two percentages and returns the dimensionless ratio as a percent, so a normal 2%-ATR stock gets a **0.25% position** instead of a few percent. The book deploys ~5% of capital and sits ~95% in cash; every risk cap (8% position, 30% sector, 80% invested) is dead. Tests currently enshrine the wrong value.
2. **🔴 Backtest ≠ live sizing** — walk-forward and PIT backtests use a flat ~5% size and never call `vol_target_size_pct`, so backtested performance reflects a portfolio the live bot will never hold.
3. **🟠 Alpaca stop-order lifecycle** — resting GTC stops are never cancelled on trail-up or on close, so stale/duplicate stops accumulate and can fire after a position is flat (opening a short). `SimulatedBroker` is immune; the real Alpaca path (default `run_bot.py`) is not.
4. **🟠 Reject-unsafe exits** — `close_position`/`reduce_position` book realized PnL and mutate the DB even when the sell order is REJECTED, creating orphans and corrupting PnL.
5. **🟡 `max_invested_pct` enforced once per pipeline**, not per entry — exposure can overshoot the 80% cap within one pipeline.
6. **🟡 Resting-stop trailing ignores the source filter** — the hedge-stop pass also tightens long positions' resting stops (10% instead of 15%).
7. **🟡 `update_single` zero-pads missing regime features before scaling** — a dropped feature becomes a large spurious z-score.
8. **🟢 Hygiene** — backtest same-bar fills (look-ahead), inaccurate reconcile docstring, dead import, stale comment, stale CLAUDE.md gotchas.

**Calibration note for Task A1:** fixing the units makes positions ~100× larger, so `per_trade_risk_pct` must drop from `0.5` to a value that targets sane gross exposure. This plan uses **`0.15`** as the new default (a 2%-ATR name → 7.5%, a 3%-ATR name → 5%, low-vol names cap at 8%; ~15 names ≈ 75% deployed). This is a risk knob — after Task A1, confirm median position size and total deployed % look right before trusting any backtest (verification step included).

**Where to run:** branch `sizing-execution-fixes` off `main` (which matches this plan exactly). A dedicated worktree is set up at `.worktrees/sizing-execution-fixes/` — run `pytest` from inside its `trading bot/` and keep the full suite green after every task. Do **not** use the older `trading-bot-fixes` worktree; it is a stale, divergent branch missing the current code.

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `risk/position_sizing.py` | Modify + extend | Fix units in `vol_target_size_pct`; add pure helpers `atr_pct_from_ohlc`, `vol_pct_from_close` |
| `system/config.py` | Modify | Retune `SizingConfig.per_trade_risk_pct` default |
| `orchestration/main_loop.py` | Modify | Use ATR helper (DRY); pass `current_invested_pct` to risk veto |
| `backtesting/run_strategy_backtest.py` | Modify | Size signals via `vol_target_size_pct`; next-bar fills |
| `backtesting/walk_forward.py` | Modify | Size signals via `vol_target_size_pct`; next-bar fills |
| `risk/risk_manager.py` | Modify | Enforce `max_invested_pct` in `validate_order`; remove dead import |
| `bot/portfolio.py` | Modify | Reject-safe close/reduce; stop-order cancel lifecycle; source-filtered trailing; docstring fix |
| `regime/hmm_engine.py` | Modify | Neutral (mean) feature padding via testable helper |
| `screener/factor_scorer.py` | Modify | Fix stale comment |
| `CLAUDE.md` | Modify | Refresh sizing/stops gotchas |
| `tests/test_position_sizing.py` | Rewrite assertions | Correct sizing expectations + helper tests |
| `tests/test_portfolio.py` | Extend | Reject-safe + stop-lifecycle + source-filter tests |
| `tests/test_risk_manager.py` | Extend | Invested-cap veto tests |
| `tests/test_regime.py` | Extend | Feature-padding helper test |
| `tests/test_simulation.py` or `tests/test_walk_forward_analysis.py` | Extend | Backtest-sizing test |

---

# PHASE A — Sizing correctness (Critical)

### Task A1: Fix the position-sizing units bug + retune default

**Files:**
- Modify: `risk/position_sizing.py:18-53`
- Modify: `system/config.py:171-182`
- Test: `tests/test_position_sizing.py:6-58` (rewrite the `TestVolTargetSizePct` class)

- [ ] **Step 1: Rewrite the failing tests to encode CORRECT sizing**

Replace the entire `class TestVolTargetSizePct:` block (lines 6-58) in `tests/test_position_sizing.py` with:

```python
class TestVolTargetSizePct:
    """vol_target_size_pct(atr_pct, per_trade_risk_pct, max_position_pct).

    Position % of NAV = per_trade_risk_pct / atr_pct * 100, clamped to max.
    Both inputs are percentages (e.g. atr_pct=2.0 means 2%).
    """

    def test_two_pct_atr_is_a_real_position_not_microscopic(self):
        # Regression for the units bug: 0.15 / 2.0 * 100 = 7.5% (NOT 0.075%)
        assert vol_target_size_pct(2.0, 0.15, 8.0) == pytest.approx(7.5)

    def test_three_pct_atr_below_ceiling(self):
        # 0.15 / 3.0 * 100 = 5.0%
        assert vol_target_size_pct(3.0, 0.15, 8.0) == pytest.approx(5.0)

    def test_higher_atr_gives_smaller_size(self):
        low_vol = vol_target_size_pct(3.0, 0.15, 8.0)   # 5.0%
        high_vol = vol_target_size_pct(6.0, 0.15, 8.0)  # 2.5%
        assert low_vol > high_vol

    def test_low_vol_name_caps_at_ceiling(self):
        # 0.15 / 1.0 * 100 = 15% → capped at 8.0
        assert vol_target_size_pct(1.0, 0.15, 8.0) == pytest.approx(8.0)

    def test_respects_custom_ceiling(self):
        # 0.15 / 1.0 * 100 = 15% → capped at 5.0
        assert vol_target_size_pct(1.0, 0.15, 5.0) == pytest.approx(5.0)

    def test_zero_atr_uses_fallback_not_crash(self):
        # atr<=0 → fallback 1.0 → 0.05 / 1.0 * 100 = 5.0% (no ZeroDivisionError)
        assert vol_target_size_pct(0.0, 0.05, 8.0) == pytest.approx(5.0)

    def test_negative_atr_uses_fallback(self):
        assert vol_target_size_pct(-1.0, 0.05, 8.0) == pytest.approx(5.0)

    def test_result_is_always_non_negative(self):
        for atr in [0.01, 0.5, 1.0, 5.0, 10.0]:
            assert vol_target_size_pct(atr, 0.15, 8.0) >= 0.0

    def test_higher_risk_budget_gives_bigger_size(self):
        # 0.30 / 6.0 * 100 = 5.0% vs 0.15 / 6.0 * 100 = 2.5%
        assert vol_target_size_pct(6.0, 0.30, 8.0) == pytest.approx(5.0)
        assert vol_target_size_pct(6.0, 0.15, 8.0) == pytest.approx(2.5)

    def test_very_high_atr_gives_small_but_nonzero_size(self):
        # 0.15 / 20.0 * 100 = 0.75%
        assert vol_target_size_pct(20.0, 0.15, 8.0) == pytest.approx(0.75)
```

- [ ] **Step 2: Run the tests to verify they FAIL**

Run: `pytest tests/test_position_sizing.py::TestVolTargetSizePct -v`
Expected: FAIL — current implementation returns `per_trade_risk_pct/atr_pct` (e.g. `0.075`), so `test_two_pct_atr_is_a_real_position_not_microscopic` fails (`0.075 != 7.5`).

- [ ] **Step 3: Fix the units in `vol_target_size_pct`**

In `risk/position_sizing.py`, replace the function body (lines 18-53) with:

```python
def vol_target_size_pct(
    atr_pct: float,
    per_trade_risk_pct: float,
    max_position_pct: float,
) -> float:
    """Return position size as % of NAV using volatility targeting.

    Both ``atr_pct`` and ``per_trade_risk_pct`` are percentages. Risking
    ``per_trade_risk_pct`` of NAV with a stop ≈ 1 ATR away implies a position of
    ``per_trade_risk_pct / atr_pct`` of NAV — a *fraction*, so we multiply by 100
    to return a percentage. Smaller size for more volatile names.

    Parameters
    ----------
    atr_pct:
        14-bar ATR as a percentage of price (e.g. 2.0 means 2%). Must be
        positive; zero/negative falls back to 1.0 to avoid division by zero.
    per_trade_risk_pct:
        % of NAV risked per trade (e.g. 0.15).
    max_position_pct:
        Hard ceiling on position size as % of NAV (e.g. 8.0).

    Returns
    -------
    float : position size in [0, max_position_pct].

    Examples
    --------
    >>> vol_target_size_pct(2.0, 0.15, 8.0)   # 0.15/2.0*100 = 7.5%
    7.5
    >>> vol_target_size_pct(1.0, 0.15, 8.0)   # 0.15/1.0*100 = 15% → capped
    8.0
    >>> vol_target_size_pct(20.0, 0.15, 8.0)  # 0.15/20.0*100 = 0.75%
    0.75
    """
    if atr_pct <= 0:
        atr_pct = 1.0  # safe fallback
    raw = per_trade_risk_pct / atr_pct * 100.0
    return float(min(max(raw, 0.0), max_position_pct))
```

- [ ] **Step 4: Retune the config default**

In `system/config.py`, change `SizingConfig.per_trade_risk_pct` (line 181) from:

```python
    per_trade_risk_pct: float = 0.5          # risk budget per trade as % of NAV
```

to:

```python
    # Risk budget per trade as % of NAV. With the corrected vol-target formula a
    # 2%-ATR name → 7.5%, a 3%-ATR name → 5%, low-vol names cap at max_position_pct.
    # Tune this to your target gross exposure; keep it ≤ max_position_pct (validated).
    per_trade_risk_pct: float = 0.15
```

- [ ] **Step 5: Run the tests to verify they PASS**

Run: `pytest tests/test_position_sizing.py -v`
Expected: PASS (all `TestVolTargetSizePct` + unchanged `TestApplyConvictionTilt`).

- [ ] **Step 6: Verify the full suite is still green**

Run: `pytest -q`
Expected: PASS (no regressions). If any orchestrator/integration test asserted the old micro-sizes, update the asserted number to the corrected value (do not revert the fix).

- [ ] **Step 7: Commit**

```bash
git add risk/position_sizing.py system/config.py tests/test_position_sizing.py
git commit -m "fix: correct vol_target_size_pct units (was ~100x undersizing); retune per_trade_risk_pct

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task A2: Extract ATR helper and de-duplicate the live sizing path

The orchestrator computes ATR% inline in two near-identical blocks (`_process_signal` and `_process_fundamental_candidate`). Extract a pure, tested helper and reuse it.

**Files:**
- Modify: `risk/position_sizing.py` (add `atr_pct_from_ohlc`)
- Modify: `orchestration/main_loop.py:58` (import), `:580-595`, `:726-742` (use helper)
- Test: `tests/test_position_sizing.py` (add `TestAtrPctFromOhlc`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_position_sizing.py`:

```python
import numpy as np
from risk.position_sizing import atr_pct_from_ohlc


class TestAtrPctFromOhlc:
    def test_constant_range_gives_expected_atr_pct(self):
        # Each bar has a true range of 2.0 around a ~100 close → ATR=2, atr_pct≈2%
        n = 20
        close = np.full(n, 100.0)
        high = close + 1.0
        low = close - 1.0
        result = atr_pct_from_ohlc(high, low, close, window=14)
        assert result == pytest.approx(2.0, abs=0.1)

    def test_insufficient_history_returns_fallback(self):
        close = np.array([100.0, 101.0, 102.0])  # < window+1
        assert atr_pct_from_ohlc(close + 1, close - 1, close, window=14) == pytest.approx(1.0)

    def test_zero_last_price_returns_fallback(self):
        n = 20
        close = np.concatenate([np.full(n - 1, 100.0), [0.0]])
        high = close + 1.0
        low = close - 1.0
        assert atr_pct_from_ohlc(high, low, close, window=14, fallback=1.0) == pytest.approx(1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_position_sizing.py::TestAtrPctFromOhlc -v`
Expected: FAIL with `ImportError: cannot import name 'atr_pct_from_ohlc'`.

- [ ] **Step 3: Implement the helper**

At the top of `risk/position_sizing.py`, under `from __future__ import annotations`, add:

```python
import numpy as np
```

Then append to `risk/position_sizing.py`:

```python
def atr_pct_from_ohlc(
    high,
    low,
    close,
    window: int = 14,
    fallback: float = 1.0,
) -> float:
    """Average True Range as a percentage of the latest close.

    Parameters
    ----------
    high, low, close : array-likes of equal length (oldest → newest).
    window : ATR lookback in bars.
    fallback : returned when there is insufficient history or a non-positive
        last price (keeps sizing conservative rather than crashing).
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if len(close) < window + 1:
        return fallback
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])),
    )
    atr = float(tr[-window:].mean())
    last = close[-1]
    return atr / last * 100.0 if last > 0 else fallback
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_position_sizing.py::TestAtrPctFromOhlc -v`
Expected: PASS.

- [ ] **Step 5: Use the helper in the orchestrator (DRY)**

In `orchestration/main_loop.py` line 58, change the import to:

```python
from risk.position_sizing import vol_target_size_pct, apply_conviction_tilt, atr_pct_from_ohlc
```

Replace the ATR block in `_process_signal` (lines 580-595) with:

```python
        # ATR-based deterministic position sizing
        try:
            hist = _t.history(period="30d")
            atr_pct = atr_pct_from_ohlc(
                hist["High"].values, hist["Low"].values, hist["Close"].values,
                window=self._cfg.sizing.atr_window,
            )
        except Exception:
            atr_pct = 1.0
```

Replace the identical ATR block in `_process_fundamental_candidate` (lines 726-742) with the same six lines.

- [ ] **Step 6: Verify the suite is green**

Run: `pytest -q`
Expected: PASS. Orchestrator tests mock `yf.Ticker(...).history(...)`; if a test relied on the old inline code path it still works because the helper produces the same ATR% for the same OHLC.

- [ ] **Step 7: Commit**

```bash
git add risk/position_sizing.py orchestration/main_loop.py tests/test_position_sizing.py
git commit -m "refactor: extract atr_pct_from_ohlc helper; de-duplicate live ATR sizing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task A3: Make the backtests size like live

Both backtest runners use a flat `position_pct` (~5%). Route them through `vol_target_size_pct` using a close-only volatility proxy (PIT/backtest price data is close-only), so backtested deployment matches the live vol-target book.

**Files:**
- Modify: `risk/position_sizing.py` (add `vol_pct_from_close`)
- Modify: `backtesting/run_strategy_backtest.py:70-160`
- Modify: `backtesting/walk_forward.py:178-196`
- Test: `tests/test_position_sizing.py` (add `TestVolPctFromClose`); `tests/test_walk_forward_analysis.py` (sizing test)

- [ ] **Step 1: Write the failing helper test**

Append to `tests/test_position_sizing.py`:

```python
from risk.position_sizing import vol_pct_from_close


class TestVolPctFromClose:
    def test_flat_prices_have_zero_vol(self):
        close = np.full(30, 100.0)
        assert vol_pct_from_close(close, window=14) == pytest.approx(0.0)

    def test_one_pct_daily_moves_give_one_pct_vol(self):
        # Alternating +1% / -1% closes → mean abs daily return ≈ 1%
        close = [100.0]
        for i in range(30):
            close.append(close[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
        result = vol_pct_from_close(np.array(close), window=14)
        assert result == pytest.approx(1.0, abs=0.1)

    def test_insufficient_history_returns_fallback(self):
        assert vol_pct_from_close(np.array([100.0, 101.0]), window=14) == pytest.approx(1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_position_sizing.py::TestVolPctFromClose -v`
Expected: FAIL with `ImportError: cannot import name 'vol_pct_from_close'`.

- [ ] **Step 3: Implement `vol_pct_from_close`**

Append to `risk/position_sizing.py`:

```python
def vol_pct_from_close(close, window: int = 14, fallback: float = 1.0) -> float:
    """Close-only volatility proxy: mean absolute daily return (%) over ``window``.

    Stand-in for ATR% where intraday OHLC is unavailable (backtests). ATR% and
    mean-abs daily return are comparable in magnitude, so the same
    vol_target_size_pct() can be used for both live and backtest sizing.
    """
    close = np.asarray(close, dtype=float)
    if len(close) < window + 1:
        return fallback
    tail = close[-(window + 1):]
    rets = np.abs(np.diff(tail) / tail[:-1])
    return float(rets.mean() * 100.0)
```

- [ ] **Step 4: Run helper test to verify it passes**

Run: `pytest tests/test_position_sizing.py::TestVolPctFromClose -v`
Expected: PASS.

- [ ] **Step 5: Size PIT-backtest signals via vol targeting**

In `backtesting/run_strategy_backtest.py`:

Add imports near the top (after line 38's screener import):

```python
from risk.position_sizing import vol_pct_from_close, vol_target_size_pct
from system.config import settings as _settings
```

Change the `run_pit_backtest` signature (lines 70-81) to add two knobs (keep `position_pct` for backward compatibility, now used as a fallback only):

```python
def run_pit_backtest(
    provider: PITDataProvider,
    rebalance_dates: list[str],
    top_n: int = 20,
    position_pct: float = 5.0,
    initial_cash: float = 100_000.0,
    regime_label: str | None = None,
    slippage_bps: float = 10.0,
    commission_pct: float = 0.05,
    factor_csv_path: str | None = None,
    spy_prices: pd.Series | None = None,
    per_trade_risk_pct: float | None = None,
    max_position_pct: float | None = None,
) -> dict:
```

Immediately after the docstring (before `all_signals: list[dict] = []`), add:

```python
    risk_budget = per_trade_risk_pct if per_trade_risk_pct is not None else _settings.sizing.per_trade_risk_pct
    pos_ceiling = max_position_pct if max_position_pct is not None else _settings.risk.max_position_pct
```

Replace the signal-construction loop (lines 147-160) with:

```python
        for ticker in window_tickers:
            # Vol-target sizing from the trailing close window (matches live)
            vol_window = provider.prices(
                ticker, rebal_date - timedelta(days=40), rebal_date
            )
            atr_pct = vol_pct_from_close(vol_window.values, window=14) if not vol_window.empty else 1.0
            size_pct = vol_target_size_pct(atr_pct, risk_budget, pos_ceiling)
            all_signals.append({
                "date": rebal_str,
                "ticker": ticker,
                "conviction": int(top.loc[ticker, "composite_score"]),
                "position_pct": size_pct,
            })
            # Collect prices for the holding period
            if ticker not in all_prices:
                series = provider.prices(ticker,
                                         rebal_date - timedelta(days=5),
                                         hold_end)
                if not series.empty:
                    all_prices[ticker] = series
```

- [ ] **Step 6: Size walk-forward signals via vol targeting**

In `backtesting/walk_forward.py`, add the import after line 33:

```python
from risk.position_sizing import vol_pct_from_close, vol_target_size_pct
```

Add `sizing_cfg` resolution at the top of `run_walk_forward` (after line 101's `feature_cfg` default):

```python
    if alloc_cfg is None:
        pass  # regime scaling simply defaults to 1.0 below
    from system.config import settings as _settings
    _risk_budget = _settings.sizing.per_trade_risk_pct
    _pos_ceiling = _settings.risk.max_position_pct
```

Replace the `base_pct` assignment (line 179) — currently `base_pct = float(sig.get("position_pct", 5.0))` — with:

```python
            # Vol-target base size from the trailing close window (matches live).
            series_for_vol = price_data.get(sig["ticker"])
            atr_pct = 1.0
            if series_for_vol is not None and not series_for_vol.empty:
                upto = series_for_vol.loc[series_for_vol.index <= pd.Timestamp(sig_date)]
                if len(upto) >= 15:
                    atr_pct = vol_pct_from_close(upto.values, window=14)
            base_pct = vol_target_size_pct(atr_pct, _risk_budget, _pos_ceiling)
```

(The existing regime-multiplier and confidence-scaling lines below it stay unchanged — they now scale the vol-target base, exactly as live does.)

- [ ] **Step 7: Write a test proving backtest sizing varies with volatility**

Append to `tests/test_walk_forward_analysis.py` (it already imports walk-forward helpers; add this self-contained test):

```python
import numpy as np
import pandas as pd
from backtesting.simulation import simulate_portfolio
from risk.position_sizing import vol_pct_from_close, vol_target_size_pct


def test_backtest_sizes_smaller_for_higher_vol():
    """A high-vol name should receive a smaller position_pct than a low-vol name
    under the shared vol-target sizing used by the backtest runners."""
    low_vol_close = np.full(30, 100.0)
    low_vol_close = low_vol_close + np.linspace(0, 0.3, 30)  # ~flat
    high_vol_close = [100.0]
    for i in range(29):
        high_vol_close.append(high_vol_close[-1] * (1.05 if i % 2 == 0 else 1 / 1.05))

    low_size = vol_target_size_pct(vol_pct_from_close(low_vol_close), 0.15, 8.0)
    high_size = vol_target_size_pct(vol_pct_from_close(np.array(high_vol_close)), 0.15, 8.0)

    assert low_size > high_size
    assert high_size > 0.0
```

- [ ] **Step 8: Run the tests**

Run: `pytest tests/test_position_sizing.py tests/test_walk_forward_analysis.py -q`
Expected: PASS.

- [ ] **Step 9: Verify the full suite is green**

Run: `pytest -q`
Expected: PASS. If a walk-forward/PIT test asserted a specific flat-5% trade size, update it to reflect vol-target sizing (the sizes now vary by name).

- [ ] **Step 10: Commit**

```bash
git add risk/position_sizing.py backtesting/run_strategy_backtest.py backtesting/walk_forward.py tests/test_position_sizing.py tests/test_walk_forward_analysis.py
git commit -m "fix: size backtest signals with vol_target_size_pct so backtest matches live

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

# PHASE B — Execution safety (High)

### Task B1: Reject-safe `close_position` and `reduce_position`

A rejected sell must NOT book PnL or mutate the DB (that creates orphans + corrupts realized PnL). Alert and bail instead.

**Files:**
- Modify: `bot/portfolio.py:87-115` (`close_position`), `:131-161` (`reduce_position`)
- Test: `tests/test_portfolio.py` (add two tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_portfolio.py`:

```python
def test_close_position_rejected_sell_does_not_log_or_delete(mock_broker, db, mocker):
    """A rejected sell must leave the DB position intact and book no closed_position."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    rejected = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    # Position still in DB, nothing booked as closed
    assert any(p["ticker"] == "AAPL" for p in db.get_open_positions())
    assert db.get_closed_positions() == []


def test_reduce_position_rejected_sell_does_not_change_shares(mock_broker, db):
    """A rejected partial sell must not change DB shares or book a trade."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    rejected = Order(ticker="AAPL", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.reduce_position(
        "AAPL", 10.0, exit_price=110.0,
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    pos = [p for p in db.get_open_positions() if p["ticker"] == "AAPL"][0]
    assert pos["shares"] == pytest.approx(10.0)  # unchanged
    assert db.get_closed_positions() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_portfolio.py::test_close_position_rejected_sell_does_not_log_or_delete tests/test_portfolio.py::test_reduce_position_rejected_sell_does_not_change_shares -v`
Expected: FAIL — current code books the closed position / changes shares regardless of rejection.

- [ ] **Step 3: Gate `close_position` on a successful fill**

In `bot/portfolio.py`, replace the body of `close_position` (lines 90-115) so it returns early on rejection. The new body:

```python
        order = self._place_sell_with_retry(ticker, shares)
        if order.status == OrderStatus.REJECTED:
            emit_event(
                log, EventType.ORDER_REJECTED,
                f"Sell for {ticker} REJECTED after retries ({order.reject_reason}) — "
                "position left intact for next reconcile/poll",
                data={"ticker": ticker, "reason": order.reject_reason},
                level=logging.ERROR,
                alert=True,
            )
            return
        exit_commission = order.filled_qty * self.broker.get_commission_per_share()
        entry_commission = 0.0
        for pos in db.get_open_positions():
            if pos["ticker"] == ticker:
                entry_commission = pos["entry_commission"] if pos["entry_commission"] is not None else 0.0
                break
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
            costs=exit_commission,
            entry_commission=entry_commission,
        )
        db.delete_position(ticker)
```

Add `import logging` is already present (line 3). `emit_event`/`EventType` are already imported (line 8).

- [ ] **Step 4: Gate `reduce_position` on a successful fill**

In `bot/portfolio.py`, replace the body of `reduce_position` (lines 134-161) with:

```python
        sell_qty = shares / 2
        order = self._place_sell_with_retry(ticker, sell_qty)
        if order.status == OrderStatus.REJECTED:
            emit_event(
                log, EventType.ORDER_REJECTED,
                f"Reduce sell for {ticker} REJECTED after retries ({order.reject_reason}) — "
                "shares left unchanged",
                data={"ticker": ticker, "reason": order.reject_reason},
                level=logging.ERROR,
                alert=True,
            )
            return
        exit_commission = order.filled_qty * self.broker.get_commission_per_share()
        entry_commission = 0.0
        for pos in db.get_open_positions():
            if pos["ticker"] == ticker:
                full_entry_comm = pos["entry_commission"] if pos["entry_commission"] is not None else 0.0
                entry_commission = full_entry_comm * (sell_qty / shares) if shares > 0 else 0.0
                break
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
            costs=exit_commission,
            entry_commission=entry_commission,
        )
        db.update_position_shares(ticker, shares - sell_qty)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_portfolio.py -q`
Expected: PASS (new tests pass; existing close/reduce tests still pass because their mocked orders are FILLED, not REJECTED).

- [ ] **Step 6: Commit**

```bash
git add bot/portfolio.py tests/test_portfolio.py
git commit -m "fix: do not book PnL or mutate DB on rejected sell (close/reduce)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task B2: Stop-order lifecycle + source-filtered trailing

Cancel resting stops before replacing (trail-up) and on close/reduce, and make `enforce_stop_losses` only trail stops for positions in its source scope.

**Files:**
- Modify: `bot/portfolio.py:234-286` (`enforce_stop_losses`), `close_position` (add cancel), `reduce_position` (add cancel)
- Test: `tests/test_portfolio.py` (add three tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_portfolio.py`:

```python
def test_trailing_up_cancels_old_stop_before_placing_new(mock_broker, db):
    """On a trail-up the old resting stop must be cancelled before the new one is placed."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {}  # no existing stop → will place
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    mock_broker.cancel_stop_order.assert_called_once_with("AAPL")
    mock_broker.place_stop_order.assert_called_once()


def test_close_position_cancels_resting_stop(mock_broker, db):
    """Closing a position must cancel its resting stop so it can't fire later."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    filled = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    filled.status = OrderStatus.FILLED
    filled.filled_qty = 10.0
    mock_broker.place_order.return_value = filled
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    mock_broker.cancel_stop_order.assert_called_once_with("AAPL")


def test_hedge_stop_pass_does_not_retrail_long_positions(mock_broker, db):
    """enforce_stop_losses(source_include='hedge') must not touch a long position's stop."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    # AAPL is a long (congressional) position
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test", "congressional")

    portfolio.enforce_stop_losses(stop_loss_pct=10.0, source_include="hedge")

    mock_broker.place_stop_order.assert_not_called()
    mock_broker.cancel_stop_order.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_portfolio.py::test_trailing_up_cancels_old_stop_before_placing_new tests/test_portfolio.py::test_close_position_cancels_resting_stop tests/test_portfolio.py::test_hedge_stop_pass_does_not_retrail_long_positions -v`
Expected: FAIL — current code never calls `cancel_stop_order`, and trails stops before applying the source filter.

- [ ] **Step 3: Rewrite `enforce_stop_losses` (filter first, then cancel-before-replace)**

In `bot/portfolio.py`, replace the loop body of `enforce_stop_losses` (lines 246-286) with:

```python
        for pos in self.broker.get_positions():
            ticker = pos["ticker"]
            meta = open_positions.get(ticker, {})
            source = meta.get("signal_source", "congressional")

            # Scope filter FIRST: a hedge-only call must not touch long stops (and
            # vice versa), so each position's resting stop uses its own call's pct.
            if source_include is not None and source != source_include:
                continue
            if source_exclude is not None and source == source_exclude:
                continue

            current = pos["current_price"]
            peak = meta.get("peak_price") or pos["avg_entry_price"]
            db.update_position_peak(ticker, current)

            # Trail the resting stop upward (only-up). Cancel the old stop before
            # placing the new one so brokers (Alpaca) don't accumulate duplicates.
            new_stop = current * (1 - pct / 100)
            existing_stop = 0.0
            try:
                if hasattr(self.broker, "get_stop_orders"):
                    _stops = self.broker.get_stop_orders()
                    if isinstance(_stops, dict):
                        existing_stop = float(_stops.get(ticker, (0.0,))[0])
            except Exception:
                pass
            if new_stop > existing_stop:
                if hasattr(self.broker, "cancel_stop_order"):
                    self.broker.cancel_stop_order(ticker)
                self.broker.place_stop_order(ticker=ticker, qty=pos["qty"], stop_price=new_stop)

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

- [ ] **Step 4: Cancel the resting stop on close and reduce**

In `close_position` (Task B1's new version), add immediately after `db.delete_position(ticker)`:

```python
        if hasattr(self.broker, "cancel_stop_order"):
            self.broker.cancel_stop_order(ticker)
```

In `reduce_position` (Task B1's new version), add immediately after `db.update_position_shares(ticker, shares - sell_qty)`:

```python
        # Cancel the stale full-qty stop; the next enforce_stop_losses poll re-places
        # a fresh trailing stop for the reduced share count.
        if hasattr(self.broker, "cancel_stop_order"):
            self.broker.cancel_stop_order(ticker)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_portfolio.py -q`
Expected: PASS. Existing stop tests still pass: `test_enforce_stop_losses_trails_stop_upward` still asserts `place_stop_order` called once (cancel is also called, which it doesn't assert against); `test_enforce_stop_losses_does_not_trail_stop_downward` still asserts `place_stop_order` not called (cancel is inside the same `if`, so also not called).

- [ ] **Step 6: Commit**

```bash
git add bot/portfolio.py tests/test_portfolio.py
git commit -m "fix: cancel resting stops on trail/close/reduce; scope-filter stop trailing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

# PHASE C — Risk & inference (Medium)

### Task C1: Enforce `max_invested_pct` per entry

The risk manager must veto an entry that would push aggregate invested % over the cap, instead of relying on a single once-per-pipeline check.

**Files:**
- Modify: `risk/risk_manager.py:240-290` (`validate_order`)
- Modify: `orchestration/main_loop.py` (compute + pass `current_invested_pct` in both `_process_*`)
- Test: `tests/test_risk_manager.py` (add two tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_risk_manager.py`:

```python
def test_validate_order_vetoes_when_invested_cap_would_be_breached(tmp_path):
    mgr = _make_manager(tmp_path, max_invested_pct=80.0, max_position_pct=8.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=5.0, sector="Tech",
        sector_allocation={}, position_size_usd=5_000, adv_usd=1e9,
        current_invested_pct=78.0,  # 78 + 5 = 83 > 80
    )
    assert veto.allowed is False
    assert "invested" in veto.reason.lower()


def test_validate_order_allows_when_under_invested_cap(tmp_path):
    mgr = _make_manager(tmp_path, max_invested_pct=80.0, max_position_pct=8.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=5.0, sector="Tech",
        sector_allocation={}, position_size_usd=5_000, adv_usd=1e9,
        current_invested_pct=50.0,  # 50 + 5 = 55 < 80
    )
    assert veto.allowed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_risk_manager.py::test_validate_order_vetoes_when_invested_cap_would_be_breached tests/test_risk_manager.py::test_validate_order_allows_when_under_invested_cap -v`
Expected: FAIL — `validate_order` has no `current_invested_pct` parameter (`TypeError`).

- [ ] **Step 3: Add the parameter and check to `validate_order`**

In `risk/risk_manager.py`, change the `validate_order` signature (lines 240-248) to add the new keyword:

```python
    def validate_order(
        self,
        ticker: str,
        position_pct: float,
        sector: str,
        sector_allocation: dict[str, float],
        position_size_usd: float,
        adv_usd: float | None,
        current_invested_pct: float = 0.0,
    ) -> RiskVeto:
```

Remove the dead import on line 270 (`from system.config import settings` inside the method — it is unused). Then, immediately after the sector-concentration check (after line 275's closing `)`), add:

```python
        # Aggregate invested-capital cap (checked per entry, not once per pipeline)
        if current_invested_pct + position_pct > self._risk.max_invested_pct:
            return RiskVeto(
                allowed=False,
                reason=(
                    f"Invested cap: {current_invested_pct + position_pct:.1f}% would exceed "
                    f"max_invested_pct {self._risk.max_invested_pct:.1f}%"
                ),
            )
```

- [ ] **Step 4: Pass `current_invested_pct` from the orchestrator**

In `orchestration/main_loop.py`, in `_process_signal`, replace the block at lines 637-647 with:

```python
        _positions_now = self._broker.get_positions()
        _invested_usd = sum(p["qty"] * p["current_price"] for p in _positions_now)
        _nav = self._broker.get_cash() + _invested_usd
        _current_invested_pct = (_invested_usd / _nav * 100.0) if _nav > 0 else 0.0
        position_size_usd = _nav * final_pct / 100
        adv_usd = research.avg_daily_volume_usd if research else None
        veto = self._risk.validate_order(
            ticker=ticker, position_pct=final_pct, sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd, adv_usd=adv_usd,
            current_invested_pct=_current_invested_pct,
        )
```

In `_process_fundamental_candidate`, replace the block at lines 785-798 with:

```python
        _positions_now = self._broker.get_positions()
        _invested_usd = sum(p["qty"] * p["current_price"] for p in _positions_now)
        _nav = self._broker.get_cash() + _invested_usd
        _current_invested_pct = (_invested_usd / _nav * 100.0) if _nav > 0 else 0.0
        position_size_usd = _nav * final_pct / 100
        adv_usd = candidate.research.avg_daily_volume_usd if candidate.research else None
        veto = self._risk.validate_order(
            ticker=ticker,
            position_pct=final_pct,
            sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd,
            adv_usd=adv_usd,
            current_invested_pct=_current_invested_pct,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_risk_manager.py tests/test_orchestrator.py -q`
Expected: PASS (existing `validate_order` callers still work — the new param defaults to 0.0).

- [ ] **Step 6: Commit**

```bash
git add risk/risk_manager.py orchestration/main_loop.py tests/test_risk_manager.py
git commit -m "fix: enforce max_invested_pct per entry in risk veto; drop dead import

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task C2: Neutral feature padding in regime `update_single`

Pad missing trailing features with the training mean (scales to ~0, neutral) instead of zero (which scales to a large spurious z-score). Extract a pure helper so it is unit-testable.

**Files:**
- Modify: `regime/hmm_engine.py:394-401` (use helper), add module-level helper
- Test: `tests/test_regime.py` (add one test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_regime.py`:

```python
import numpy as np
from sklearn.preprocessing import StandardScaler
from regime.hmm_engine import _pad_features_to_scaler


def test_pad_features_uses_training_mean_so_missing_scales_to_zero():
    # Train a scaler on 4 features with distinct means
    scaler = StandardScaler().fit(np.array([
        [1.0, 2.0, 10.0, 20.0],
        [3.0, 4.0, 30.0, 40.0],
    ]))
    # Caller only has the first 2 features available this bar
    row = np.array([[2.0, 3.0]])
    padded = _pad_features_to_scaler(row, scaler)
    assert padded.shape == (1, 4)
    scaled = scaler.transform(padded)[0]
    # The two padded features were set to their training mean → scale to ~0
    assert scaled[2] == pytest.approx(0.0, abs=1e-9)
    assert scaled[3] == pytest.approx(0.0, abs=1e-9)


def test_pad_features_truncates_when_too_many_columns():
    scaler = StandardScaler().fit(np.array([[1.0, 2.0], [3.0, 4.0]]))
    row = np.array([[2.0, 3.0, 99.0]])  # one extra column
    padded = _pad_features_to_scaler(row, scaler)
    assert padded.shape == (1, 2)
```

(If `tests/test_regime.py` does not already `import pytest`, add it at the top.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_regime.py::test_pad_features_uses_training_mean_so_missing_scales_to_zero -v`
Expected: FAIL with `ImportError: cannot import name '_pad_features_to_scaler'`.

- [ ] **Step 3: Add the helper and use it**

In `regime/hmm_engine.py`, add a module-level helper after the imports (after line 36's `log = ...`):

```python
def _pad_features_to_scaler(last_row: np.ndarray, scaler) -> np.ndarray:
    """Align a feature row to the scaler's expected width.

    Missing (trailing) features are padded with the scaler's training mean so
    they standardise to ~0 (neutral) rather than a large spurious z-score; extra
    columns are truncated.
    """
    n_expected = scaler.n_features_in_
    n_have = last_row.shape[1]
    if n_have < n_expected:
        means = np.asarray(scaler.mean_)[n_have:n_expected].reshape(1, -1)
        last_row = np.hstack([last_row, means])
    elif n_have > n_expected:
        last_row = last_row[:, :n_expected]
    return last_row
```

Then replace the padding block in `update_single` (lines 394-401) with:

```python
        # Align to the scaler's expected feature count. Pad missing features with the
        # training mean (→ scales to ~0) instead of zero (→ spurious extreme z-score).
        n_have = last_row.shape[1]
        n_expected = self._result.scaler.n_features_in_
        if n_have != n_expected:
            log.warning(
                "update_single: feature width %d != expected %d — padding/truncating",
                n_have, n_expected,
            )
        last_row = _pad_features_to_scaler(last_row, self._result.scaler)
        obs_scaled = self._result.scaler.transform(last_row)[0]  # (D,)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_regime.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add regime/hmm_engine.py tests/test_regime.py
git commit -m "fix: pad missing regime features with training mean, not zero

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

# PHASE D — Hygiene (Low)

### Task D1: Next-bar fills in backtest runners (remove same-bar look-ahead)

**Files:**
- Modify: `backtesting/walk_forward.py:206-212` (main sim call)
- Modify: `backtesting/run_strategy_backtest.py:184-190` (sim call)
- Test: `tests/test_simulation.py` (add one test confirming the delay behaviour)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_simulation.py`:

```python
import pandas as pd
from backtesting.simulation import simulate_portfolio


def test_fill_delay_one_enters_on_next_bar():
    """With fill_delay_bars=1 a signal dated day 0 must fill at day 1's price."""
    idx = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    prices = {"AAA": pd.Series([100.0, 110.0, 120.0], index=idx)}
    signals = [{"date": "2026-01-02", "ticker": "AAA", "conviction": 7, "position_pct": 10.0}]

    sim = simulate_portfolio(
        signals=signals, price_data=prices, initial_cash=100_000.0,
        slippage_bps=0.0, commission_pct=0.0, fill_delay_bars=1,
    )
    # Entry should be at the 2026-01-05 price (110), not 2026-01-02 (100)
    assert sim.trades[0].entry_price == pytest.approx(110.0)
```

- [ ] **Step 2: Run test to verify it passes or fails**

Run: `pytest tests/test_simulation.py::test_fill_delay_one_enters_on_next_bar -v`
Expected: PASS already (the simulator supports `fill_delay_bars`). This test pins the behaviour the runners will now rely on. If it FAILS, stop and inspect `simulate_portfolio`'s pending-queue logic before changing the runners.

- [ ] **Step 3: Use next-bar fills in walk-forward**

In `backtesting/walk_forward.py`, the main `simulate_portfolio(...)` call (lines 206-212) — add `fill_delay_bars=1`:

```python
        sim = simulate_portfolio(
            signals=enriched_signals,
            price_data=test_price_data,
            initial_cash=initial_cash,
            slippage_bps=backtest_cfg.slippage_bps,
            commission_pct=backtest_cfg.commission_pct,
            fill_delay_bars=1,
        )
```

- [ ] **Step 4: Use next-bar fills in the PIT runner**

In `backtesting/run_strategy_backtest.py`, the `simulate_portfolio(...)` call (lines 184-190) — add `fill_delay_bars=1`:

```python
    sim = simulate_portfolio(
        signals=all_signals,
        price_data=all_prices,
        initial_cash=initial_cash,
        slippage_bps=slippage_bps,
        commission_pct=commission_pct,
        fill_delay_bars=1,
    )
```

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: PASS. If a walk-forward/PIT test asserted an exact same-bar entry price, update it to the next-bar price.

- [ ] **Step 6: Commit**

```bash
git add backtesting/walk_forward.py backtesting/run_strategy_backtest.py tests/test_simulation.py
git commit -m "fix: next-bar fills in backtest runners to remove same-bar look-ahead

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task D2: Documentation & comment accuracy

No tests — documentation/comment changes only. Verify the suite stays green at the end.

**Files:**
- Modify: `bot/portfolio.py:169-181` (reconcile docstring)
- Modify: `screener/factor_scorer.py:243` (stale comment)
- Modify: `CLAUDE.md` (Gotchas section)

- [ ] **Step 1: Fix the `reconcile_with_broker` docstring**

In `bot/portfolio.py`, the safe-mode bullet currently claims it adds untracked positions to a watchlist "by inserting a synthetic DB record." The code only logs + alerts. Replace that bullet (lines 171-176) with:

```python
        * ``auto_flatten_untracked=False`` (default, safe): emit a CRITICAL alert for every
          untracked broker position so a human can review before the next pipeline run. The
          position is **not** added to the DB and therefore is **not** covered by
          ``enforce_stop_losses`` until reconciled manually. Trade-off: no automatic exposure
          change, but no automatic stop coverage either.
```

- [ ] **Step 2: Fix the stale comment in `factor_scorer.py`**

In `screener/factor_scorer.py` line 243, change:

```python
    # mom12m is 12-month momentum passed as momentum_3m_override for research display.
```

to:

```python
    # mom12m is 12-month momentum passed as momentum_12m_override for research display.
```

- [ ] **Step 3: Refresh the CLAUDE.md Gotchas**

In `CLAUDE.md`, the Gotchas section is now stale (it predates deterministic sizing and resting stops). Replace the first three bullets under `## Gotchas` with:

```markdown
- **NAV-based sizing everywhere:** live `Portfolio.open_position`, `backtesting/simulation.py`, the walk-forward and PIT runners all size off NAV via `risk.position_sizing.vol_target_size_pct` (deterministic ATR/vol targeting). Position size is **not** LLM-driven; the LLM only gates buy/skip + a bounded conviction tilt. `per_trade_risk_pct` (in `SizingConfig`) is the gross-exposure knob.
- **Stops are resting broker orders (Alpaca) plus a polled backstop.** `enforce_stop_losses` trails the resting stop up (cancel-before-replace) and only touches positions in its source scope. Resting stops are cancelled on close/reduce. `SimulatedBroker` only enforces stops via the poll.
- **Rejected sells are no-ops at the DB layer:** `close_position`/`reduce_position` book nothing and mutate nothing on a REJECTED order — they alert and leave the position for the next reconcile/poll.
```

- [ ] **Step 4: Verify the suite is green**

Run: `pytest -q`
Expected: PASS (no code-behaviour change).

- [ ] **Step 5: Commit**

```bash
git add bot/portfolio.py screener/factor_scorer.py CLAUDE.md
git commit -m "docs: correct reconcile docstring, stale momentum comment, CLAUDE.md gotchas

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the whole suite once more from inside `trading bot/`:

Run: `pytest -q`
Expected: PASS (≈488+ tests, now including the new sizing/portfolio/risk/regime tests).

- [ ] **Calibration sanity check (manual, important):** run a backtest and confirm sizing is sane now.

Run: `python run_backtest_nokey.py` (no API keys needed)
Expected: trades show per-name `position_pct` in a sane range (~2–8%, varying by volatility), and total deployed capital is a meaningful fraction of NAV — **not** ~5% with everything in cash. If positions still look microscopic, re-check Task A1 (`per_trade_risk_pct`) before trusting any results.

---

## Self-Review (completed during planning)

**Spec coverage** — every review finding maps to a task: units bug → A1; backtest≠live → A2+A3; stop lifecycle → B2; reject-unsafe exits → B1; invested cap → C1; source-filtered trailing → B2; feature padding → C2; same-bar fills → D1; docstring/comment/CLAUDE.md → D2. The momentum 12-1 nuance and a `closed_positions` cost-column breakdown were deliberately **left out of scope** (methodology choice + nice-to-have schema change, not correctness) — note them to the user rather than implement silently.

**Placeholder scan** — every code step contains complete code; every command states expected PASS/FAIL. No TBD/TODO/"handle edge cases".

**Type consistency** — `vol_target_size_pct`, `atr_pct_from_ohlc`, `vol_pct_from_close`, `_pad_features_to_scaler`, and the new `current_invested_pct` parameter are used with identical signatures everywhere they appear.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-03-sizing-and-execution-safety-fixes.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Suggested order:** A1 → A2 → A3 → B1 → B2 → C1 → C2 → D1 → D2. A1 is the highest-value single change; B1 must land before B2 (B2's `enforce_stop_losses` calls the reject-safe `close_position`).
