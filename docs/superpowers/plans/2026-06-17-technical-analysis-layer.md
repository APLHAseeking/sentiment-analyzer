# Technical Analysis Layer Implementation Plan

> **Status: COMPLETE — merged to `main` at `5de0a39` on 2026-06-17.** All 22 tasks implemented
> (4 parallel tracks + 4 sequential integration tasks), 659/659 tests passing. Default behavior
> (`enable_technical_gate=False`) verified byte-for-byte unchanged. See `trading bot/CLAUDE.md`
> Gotchas for the user-facing summary.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-gated, deterministic technical-indicator pipeline plus an LLM synthesis call that gates entries on timing/risk-geometry, and use its structural invalidation level to replace the fixed 15% stop and ATR-only sizing — for gated trades only. Default (`enable_technical_gate=False`) behavior is byte-for-byte unchanged.

**Architecture:** A new `technical/` package computes a `TechnicalSnapshot` from OHLCV + SPY/sector closes using hand-rolled numpy/pandas indicators (no new dependency). `bot/ai_analyst.py` gains a `score_technical` LLM call that turns the snapshot into a `TechnicalScore` (buy/skip + invalidation/target prices), with in-code sanity checks on the model's own arithmetic. `orchestration/main_loop.py` wires this in after the existing AI entry score, in both `_process_signal` and `_process_fundamental_candidate`: a skip (or weak reward:risk) rejects the candidate; a buy switches sizing to a new `structure_stop_size_pct` and threads a per-position `initial_stop_pct` through `Portfolio.open_position` and a new `positions.stop_pct` column, so `enforce_stop_losses` trails each position at its own structurally-derived width instead of one global constant.

**Tech Stack:** Python 3.11+, numpy/pandas (hand-rolled indicators, no TA-Lib/pandas-ta), Anthropic `claude-sonnet-4-6` (existing `bot/ai_analyst.py` call infra), SQLite (existing `bot/db.py` migration system), pytest + pytest-mock (offline, no network).

Full design context: `docs/superpowers/specs/2026-06-17-technical-analysis-layer-design.md` (read before executing — this plan implements it task-by-task and does not re-litigate any design decision).

---

## File Structure

**New files:**
- `technical/__init__.py` — package marker.
- `technical/sector_map.py` — static GICS-sector → sector-ETF dict.
- `technical/indicators.py` — pure indicator functions + `TechnicalSnapshot` dataclass + `compute_snapshot()` pipeline. Built incrementally across Tasks 3–12; each task appends a self-contained group of functions plus their tests.
- `tests/test_technical_indicators.py` — synthetic-fixture tests for every function in `technical/indicators.py`, grown alongside it.
- `tests/test_technical_sector_map.py` — tests for the sector map.

**Modified files:**
- `system/config.py` — `SizingConfig` gains `enable_technical_gate`, `min_reward_risk` (Task 1).
- `risk/position_sizing.py` — gains `structure_stop_size_pct` (Task 13).
- `bot/ai_analyst.py` — gains `_TECHNICAL_SCHEMA`, `_TECHNICAL_BOTH_BONUS`, `TechnicalScore`, `parse_technical_response`, `_build_technical_prompt`, `score_technical` (Tasks 14–15).
- `bot/db.py` — schema migration 5 (`positions.stop_pct`), `insert_position(..., stop_pct=15.0)` (Task 16).
- `bot/portfolio.py` — `open_position(..., initial_stop_pct=None)`, `enforce_stop_losses()` reads per-position `stop_pct` (Tasks 17–18).
- `bot/signal_engine.py` — gains cached `get_etf_close_history`/`clear_etf_cache` (Task 19).
- `orchestration/main_loop.py` — wires the gate into `_process_signal` and `_process_fundamental_candidate` (Tasks 20–21).
- `trading bot/CLAUDE.md` — documents the new flag, column, and package (Task 22).

**Existing test files touched:** `tests/test_config.py` (Task 1), `tests/test_position_sizing.py` (Task 13), `tests/test_ai_analyst.py` (Tasks 14–15), `tests/test_db.py` (Task 16), `tests/test_portfolio.py` (Tasks 17–18, plus one **existing test fix** in Task 18), `tests/test_signal_engine.py` (Task 19), `tests/test_orchestrator.py` (Tasks 20–21).

All file paths below are relative to the repo root `/Users/thomasvromen/Documents/Claude code test/`, except commands, which give full paths explicitly since `pytest` must run from inside `trading bot/` while `git` must run from the repo root.

---

## Task 1: Config — `enable_technical_gate` / `min_reward_risk`

**Files:**
- Modify: `trading bot/system/config.py:182-188` (`SizingConfig`)
- Test: `trading bot/tests/test_config.py`

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_config.py`:

```python
def test_sizing_config_technical_gate_defaults():
    s = Settings()
    assert s.sizing.enable_technical_gate is False
    assert s.sizing.min_reward_risk == 2.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_config.py::test_sizing_config_technical_gate_defaults -v`
Expected: FAIL with `AttributeError: 'SizingConfig' object has no attribute 'enable_technical_gate'`

- [x] **Step 3: Implement**

In `trading bot/system/config.py`, change the `SizingConfig` dataclass (currently lines 173-187) from:

```python
@dataclass(frozen=True)
class SizingConfig:
    """Parameters for volatility-targeted position sizing.

    The formula is:
        size_pct = clamp(per_trade_risk_pct / atr_pct, 0, max_position_pct)

    where atr_pct is the 14-bar ATR expressed as % of entry price.
    """
    target_portfolio_vol_pct: float = 15.0   # target annualised portfolio vol (informational)
    # Risk budget per trade as % of NAV. With the corrected vol-target formula a
    # 2%-ATR name → 7.5%, a 3%-ATR name → 5%, low-vol names cap at max_position_pct.
    # Tune this to your target gross exposure; keep it ≤ max_position_pct (validated).
    per_trade_risk_pct: float = 0.15
    atr_window: int = 14                     # ATR lookback in bars
```

to:

```python
@dataclass(frozen=True)
class SizingConfig:
    """Parameters for volatility-targeted position sizing.

    The formula is:
        size_pct = clamp(per_trade_risk_pct / atr_pct, 0, max_position_pct)

    where atr_pct is the 14-bar ATR expressed as % of entry price.
    """
    target_portfolio_vol_pct: float = 15.0   # target annualised portfolio vol (informational)
    # Risk budget per trade as % of NAV. With the corrected vol-target formula a
    # 2%-ATR name → 7.5%, a 3%-ATR name → 5%, low-vol names cap at max_position_pct.
    # Tune this to your target gross exposure; keep it ≤ max_position_pct (validated).
    per_trade_risk_pct: float = 0.15
    atr_window: int = 14                     # ATR lookback in bars
    enable_technical_gate: bool = False      # config-gated TA layer; False = today's behavior
    min_reward_risk: float = 2.0             # below this, a technical "buy" is treated as skip
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_config.py -v`
Expected: PASS (all tests in the file, no regressions)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/system/config.py" "trading bot/tests/test_config.py" && git commit -m "$(cat <<'EOF'
feat: add enable_technical_gate and min_reward_risk to SizingConfig

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 2: `technical/` package skeleton + sector ETF map

**Files:**
- Create: `trading bot/technical/__init__.py`
- Create: `trading bot/technical/sector_map.py`
- Test: `trading bot/tests/test_technical_sector_map.py`

- [x] **Step 1: Write the failing test**

Create `trading bot/tests/test_technical_sector_map.py`:

```python
from technical.sector_map import SECTOR_ETF_MAP


def test_known_sectors_map_to_expected_etfs():
    assert SECTOR_ETF_MAP["Technology"] == "XLK"
    assert SECTOR_ETF_MAP["Financial Services"] == "XLF"
    assert SECTOR_ETF_MAP["Healthcare"] == "XLV"
    assert SECTOR_ETF_MAP["Energy"] == "XLE"
    assert SECTOR_ETF_MAP["Industrials"] == "XLI"
    assert SECTOR_ETF_MAP["Consumer Cyclical"] == "XLY"
    assert SECTOR_ETF_MAP["Consumer Defensive"] == "XLP"
    assert SECTOR_ETF_MAP["Utilities"] == "XLU"
    assert SECTOR_ETF_MAP["Real Estate"] == "XLRE"
    assert SECTOR_ETF_MAP["Basic Materials"] == "XLB"
    assert SECTOR_ETF_MAP["Communication Services"] == "XLC"


def test_unknown_sector_returns_none_via_get():
    assert SECTOR_ETF_MAP.get("Unknown Sector") is None
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_sector_map.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'technical'`

- [x] **Step 3: Implement**

Create `trading bot/technical/__init__.py`:

```python
"""Technical-analysis indicator pipeline (config-gated; see SizingConfig.enable_technical_gate)."""
```

Create `trading bot/technical/sector_map.py`:

```python
from __future__ import annotations

# yfinance GICS sector string -> sector ETF ticker. Unknown/missing sectors should use
# .get() and treat the result as neutral (omit rs_vs_sector_3m_pct), not an error.
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_sector_map.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/__init__.py" "trading bot/technical/sector_map.py" "trading bot/tests/test_technical_sector_map.py" && git commit -m "$(cat <<'EOF'
feat: add technical package skeleton and sector ETF map

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 3: Indicators — SMA trend / alignment

**Files:**
- Create: `trading bot/technical/indicators.py`
- Create: `trading bot/tests/test_technical_indicators.py`

- [x] **Step 1: Write the failing test**

Create `trading bot/tests/test_technical_indicators.py`:

```python
import numpy as np
import pandas as pd
import pytest

from technical.indicators import rolling_sma, ma_alignment, sma_slope_pct, price_vs_sma_pct


class TestRollingSma:
    def test_sma_of_constant_series_equals_constant(self):
        close = pd.Series(np.full(30, 100.0))
        sma = rolling_sma(close, window=20)
        assert sma.iloc[-1] == pytest.approx(100.0)

    def test_sma_window_matches_manual_mean(self):
        close = pd.Series(np.arange(1, 31, dtype=float))  # 1..30
        sma = rolling_sma(close, window=5)
        # last 5 values: 26,27,28,29,30 -> mean 28
        assert sma.iloc[-1] == pytest.approx(28.0)


class TestMaAlignment:
    def test_bullish_when_strictly_descending_smas(self):
        assert ma_alignment(sma20=110.0, sma50=105.0, sma200=100.0) == "bullish"

    def test_bearish_when_strictly_ascending_smas(self):
        assert ma_alignment(sma20=90.0, sma50=95.0, sma200=100.0) == "bearish"

    def test_mixed_when_not_monotonic(self):
        assert ma_alignment(sma20=100.0, sma50=90.0, sma200=95.0) == "mixed"


class TestSmaSlopePct:
    def test_rising_sma_gives_positive_slope(self):
        sma = pd.Series(np.linspace(100.0, 110.0, 25))
        assert sma_slope_pct(sma, lookback=20) > 0

    def test_flat_sma_gives_zero_slope(self):
        sma = pd.Series(np.full(25, 100.0))
        assert sma_slope_pct(sma, lookback=20) == pytest.approx(0.0)

    def test_too_short_series_returns_zero_not_crash(self):
        sma = pd.Series(np.full(5, 100.0))
        assert sma_slope_pct(sma, lookback=20) == pytest.approx(0.0)


class TestPriceVsSmaPct:
    def test_price_above_sma_is_positive(self):
        assert price_vs_sma_pct(price=110.0, sma_value=100.0) == pytest.approx(10.0)

    def test_price_below_sma_is_negative(self):
        assert price_vs_sma_pct(price=90.0, sma_value=100.0) == pytest.approx(-10.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'technical.indicators'`

- [x] **Step 3: Implement**

Create `trading bot/technical/indicators.py`:

```python
"""Deterministic technical-indicator pipeline (hand-rolled, no TA-Lib/pandas-ta).

All functions are causal — they only use data up to the last row passed in.
Series are oldest -> newest. Built incrementally; see compute_snapshot() at the
bottom of this module (added last) for how everything wires together.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def ma_alignment(sma20: float, sma50: float, sma200: float) -> str:
    if sma20 > sma50 > sma200:
        return "bullish"
    if sma20 < sma50 < sma200:
        return "bearish"
    return "mixed"


def sma_slope_pct(sma_series: pd.Series, lookback: int = 20) -> float:
    if len(sma_series) <= lookback:
        return 0.0
    past = sma_series.iloc[-1 - lookback]
    now = sma_series.iloc[-1]
    if past == 0:
        return 0.0
    return float((now - past) / past * 100.0)


def price_vs_sma_pct(price: float, sma_value: float) -> float:
    if sma_value == 0:
        return 0.0
    return float((price - sma_value) / sma_value * 100.0)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add SMA trend/alignment indicators

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 4: Indicators — time-series momentum

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import pct_return, momentum_12m_1m, tsmom_composite


class TestPctReturn:
    def test_positive_return(self):
        close = pd.Series(np.linspace(100.0, 121.0, 22))
        result = pct_return(close, bars_back=21)
        assert result == pytest.approx((121.0 - 100.0) / 100.0 * 100.0)

    def test_flat_series_zero_return(self):
        close = pd.Series(np.full(30, 100.0))
        assert pct_return(close, bars_back=20) == pytest.approx(0.0)

    def test_insufficient_history_returns_zero(self):
        close = pd.Series([100.0, 101.0])
        assert pct_return(close, bars_back=20) == pytest.approx(0.0)


class TestMomentum12m1m:
    def test_uptrend_gives_positive_momentum(self):
        close = pd.Series(np.linspace(100.0, 200.0, 260))
        assert momentum_12m_1m(close) > 0

    def test_flat_series_gives_zero_momentum(self):
        close = pd.Series(np.full(260, 100.0))
        assert momentum_12m_1m(close) == pytest.approx(0.0)

    def test_short_history_returns_zero(self):
        close = pd.Series(np.full(50, 100.0))
        assert momentum_12m_1m(close) == pytest.approx(0.0)


class TestTsmomComposite:
    def test_all_positive_returns_positive_composite(self):
        assert tsmom_composite(10.0, 20.0, 30.0) == pytest.approx(0.2)

    def test_clips_to_one(self):
        assert tsmom_composite(200.0, 200.0, 200.0) == pytest.approx(1.0)

    def test_clips_to_negative_one(self):
        assert tsmom_composite(-200.0, -200.0, -200.0) == pytest.approx(-1.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'pct_return' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def pct_return(close: pd.Series, bars_back: int) -> float:
    if len(close) <= bars_back:
        return 0.0
    past = close.iloc[-1 - bars_back]
    now = close.iloc[-1]
    if past == 0:
        return 0.0
    return float((now - past) / past * 100.0)


def momentum_12m_1m(close: pd.Series) -> float:
    """Classic 12-month-minus-1-month momentum: return from 253 bars ago to 22 bars ago."""
    if len(close) < 253:
        return 0.0
    past = close.iloc[-253]
    recent = close.iloc[-22]
    if past == 0:
        return 0.0
    return float((recent - past) / past * 100.0)


def tsmom_composite(ret_1m_pct: float, ret_3m_pct: float, ret_12m_1m_pct: float) -> float:
    raw = (ret_1m_pct + ret_3m_pct + ret_12m_1m_pct) / 300.0
    return float(np.clip(raw, -1.0, 1.0))
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add time-series momentum indicators

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 5: Indicators — RSI and MACD

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import compute_rsi, compute_macd, macd_state_from_hist


class TestComputeRsi:
    def test_monotonic_uptrend_gives_rsi_100(self):
        close = pd.Series(np.arange(1.0, 31.0))
        rsi = compute_rsi(close, window=14)
        assert rsi.iloc[-1] == pytest.approx(100.0)

    def test_monotonic_downtrend_gives_rsi_0(self):
        close = pd.Series(np.arange(30.0, 0.0, -1.0))
        rsi = compute_rsi(close, window=14)
        assert rsi.iloc[-1] == pytest.approx(0.0)

    def test_alternating_moves_give_rsi_near_50(self):
        vals = [100.0]
        for i in range(100):
            vals.append(vals[-1] + (1.0 if i % 2 == 0 else -1.0))
        close = pd.Series(vals)
        rsi = compute_rsi(close, window=14)
        assert rsi.iloc[-1] == pytest.approx(50.0, abs=5.0)


class TestComputeMacd:
    def test_returns_three_arrays_of_equal_length(self):
        close = pd.Series(np.linspace(100.0, 150.0, 60))
        macd_line, signal_line, hist = compute_macd(close)
        assert len(macd_line) == len(signal_line) == len(hist) == 60

    def test_uptrend_gives_positive_macd_line(self):
        close = pd.Series(np.linspace(100.0, 150.0, 60))
        macd_line, _, _ = compute_macd(close)
        assert macd_line[-1] > 0

    def test_downtrend_gives_negative_macd_line(self):
        close = pd.Series(np.linspace(150.0, 100.0, 60))
        macd_line, _, _ = compute_macd(close)
        assert macd_line[-1] < 0


class TestMacdStateFromHist:
    def test_bullish_expanding(self):
        assert macd_state_from_hist([0.1, 0.2, 0.5]) == "bullish_expanding"

    def test_bullish_fading(self):
        assert macd_state_from_hist([0.1, 0.5, 0.2]) == "bullish_fading"

    def test_bearish_expanding(self):
        assert macd_state_from_hist([-0.1, -0.2, -0.5]) == "bearish_expanding"

    def test_bearish_fading(self):
        assert macd_state_from_hist([-0.1, -0.5, -0.2]) == "bearish_fading"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_rsi' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder-smoothed RSI via ewm(alpha=1/window). Forced to 100 where avg_loss==0."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    return rsi


def compute_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line.to_numpy(), signal_line.to_numpy(), histogram.to_numpy()


def macd_state_from_hist(hist) -> str:
    arr = np.asarray(hist, dtype=float)
    direction = "bullish" if arr[-1] > 0 else "bearish"
    momentum = "expanding" if abs(arr[-1]) > abs(arr[-2]) else "fading"
    return f"{direction}_{momentum}"
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add RSI and MACD indicators

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 6: Indicators — rolling ATR%, Bollinger Bands, percentile rank

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from risk.position_sizing import atr_pct_from_ohlc
from technical.indicators import rolling_atr_pct, bollinger_bands, _percentile_rank


class TestRollingAtrPct:
    def test_constant_range_gives_expected_pct(self):
        n = 30
        close = pd.Series(np.full(n, 100.0))
        high = close + 1.0
        low = close - 1.0
        result = rolling_atr_pct(high, low, close, window=14)
        assert result.iloc[-1] == pytest.approx(2.0, abs=0.1)

    def test_matches_scalar_atr_pct_from_ohlc(self):
        n = 30
        close = pd.Series(np.full(n, 100.0))
        high = close + 1.0
        low = close - 1.0
        rolling_result = rolling_atr_pct(high, low, close, window=14)
        scalar_result = atr_pct_from_ohlc(high.values, low.values, close.values, window=14)
        assert rolling_result.iloc[-1] == pytest.approx(scalar_result, abs=0.05)


class TestBollingerBands:
    def test_noisy_series_gives_finite_percent_b_and_bandwidth(self):
        np.random.seed(0)
        close = pd.Series(100.0 + np.random.normal(0, 1.0, 30))
        percent_b, bandwidth = bollinger_bands(close, window=20, num_std=2.0)
        assert not np.isnan(percent_b[-1])
        assert not np.isnan(bandwidth[-1])

    def test_flat_series_gives_zero_bandwidth(self):
        close = pd.Series(np.full(30, 100.0))
        _, bandwidth = bollinger_bands(close, window=20, num_std=2.0)
        assert bandwidth[-1] == pytest.approx(0.0, abs=1e-6)


class TestPercentileRank:
    def test_max_value_gives_100th_percentile(self):
        history = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert _percentile_rank(history, value=5.0, lookback=5) == pytest.approx(100.0)

    def test_min_value_gives_lowest_percentile(self):
        history = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert _percentile_rank(history, value=1.0, lookback=5) == pytest.approx(20.0)

    def test_empty_history_returns_50(self):
        assert _percentile_rank(np.array([]), value=1.0, lookback=5) == pytest.approx(50.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'rolling_atr_pct' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def rolling_atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Rolling ATR% array — for the percentile field. The single latest ATR% value used
    for sizing/snapshot should come from risk.position_sizing.atr_pct_from_ohlc instead;
    this function exists only to provide the historical series for _percentile_rank."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window).mean()
    return atr / close * 100.0


def bollinger_bands(close: pd.Series, window: int = 20, num_std: float = 2.0) -> tuple[np.ndarray, np.ndarray]:
    mid = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    band_range = (upper - lower).replace(0.0, np.nan)
    percent_b = (close - lower) / band_range
    bandwidth = (upper - lower) / mid.replace(0.0, np.nan) * 100.0
    return percent_b.to_numpy(), bandwidth.to_numpy()


def _percentile_rank(history, value: float, lookback: int = 252) -> float:
    arr = np.asarray(history, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) == 0:
        return 50.0
    window = arr[-lookback:]
    if len(window) == 0:
        return 50.0
    return float(np.mean(window <= value)) * 100.0
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add rolling ATR%, Bollinger Bands, and percentile rank

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 7: Indicators — OBV and volume confirmation

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import compute_obv, rel_volume, obv_trend_from_series, volume_confirms_move


class TestComputeObv:
    def test_rising_close_gives_rising_obv(self):
        close = pd.Series(np.linspace(100.0, 110.0, 10))
        volume = pd.Series(np.full(10, 1000.0))
        obv = compute_obv(close, volume)
        assert obv.iloc[-1] > obv.iloc[0]

    def test_falling_close_gives_falling_obv(self):
        close = pd.Series(np.linspace(110.0, 100.0, 10))
        volume = pd.Series(np.full(10, 1000.0))
        obv = compute_obv(close, volume)
        assert obv.iloc[-1] < 0


class TestRelVolume:
    def test_spike_volume_gives_high_rel_volume(self):
        volume = pd.Series(np.full(25, 1000.0))
        volume.iloc[-1] = 5000.0
        assert rel_volume(volume, window=20) == pytest.approx(5.0, abs=0.1)

    def test_insufficient_history_returns_one(self):
        volume = pd.Series(np.full(5, 1000.0))
        assert rel_volume(volume, window=20) == pytest.approx(1.0)


class TestObvTrendFromSeries:
    def test_rising_obv_detected(self):
        obv = pd.Series(np.linspace(0.0, 1000.0, 30))
        assert obv_trend_from_series(obv, window=20) == "rising"

    def test_falling_obv_detected(self):
        obv = pd.Series(np.linspace(1000.0, 0.0, 30))
        assert obv_trend_from_series(obv, window=20) == "falling"

    def test_flat_obv_detected(self):
        obv = pd.Series(np.full(30, 500.0))
        assert obv_trend_from_series(obv, window=20) == "flat"


class TestVolumeConfirmsMove:
    def test_directional_bar_with_high_volume_confirms(self):
        close = pd.Series([100.0, 102.0])
        assert volume_confirms_move(close, rel_vol=1.5) is True

    def test_directional_bar_with_low_volume_does_not_confirm(self):
        close = pd.Series([100.0, 102.0])
        assert volume_confirms_move(close, rel_vol=0.8) is False

    def test_flat_bar_does_not_confirm_even_with_high_volume(self):
        close = pd.Series([100.0, 100.0])
        assert volume_confirms_move(close, rel_vol=1.5) is False
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_obv' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def rel_volume(volume: pd.Series, window: int = 20) -> float:
    if len(volume) < window + 1:
        return 1.0
    baseline = volume.iloc[-window - 1:-1].mean()
    if baseline == 0:
        return 1.0
    return float(volume.iloc[-1] / baseline)


def obv_trend_from_series(obv: pd.Series, window: int = 20) -> str:
    if len(obv) <= window:
        return "flat"
    past = obv.iloc[-1 - window]
    now = obv.iloc[-1]
    if now > past:
        return "rising"
    if now < past:
        return "falling"
    return "flat"


def volume_confirms_move(close: pd.Series, rel_vol: float) -> bool:
    if len(close) < 2:
        return False
    directional = close.iloc[-1] != close.iloc[-2]
    return bool(directional and rel_vol > 1.0)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add OBV and volume confirmation indicators

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 8: Indicators — swing pivots, market structure, RSI divergence

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import find_pivots, market_structure_from_pivots, rsi_divergence_from_pivots


class TestFindPivots:
    def test_finds_single_peak(self):
        values = [1, 2, 3, 5, 3, 2, 1, 1, 1, 1]
        assert 3 in find_pivots(values, k=3, kind="high")

    def test_finds_single_trough(self):
        values = [9, 8, 7, 1, 7, 8, 9, 9, 9, 9]
        assert 3 in find_pivots(values, k=3, kind="low")

    def test_no_pivots_in_monotonic_series(self):
        values = list(range(20))
        assert find_pivots(values, k=3, kind="high") == []


class TestMarketStructureFromPivots:
    def test_higher_highs_and_higher_lows_is_uptrend_structure(self):
        high = pd.Series([10.0, 12.0, 11.0, 15.0, 13.0])
        low = pd.Series([8.0, 9.0, 9.5, 11.0, 12.0])
        result = market_structure_from_pivots(
            pivot_highs=[1, 3], pivot_lows=[0, 2], high=high, low=low
        )
        assert result == "HH_HL"

    def test_lower_highs_and_lower_lows_is_downtrend_structure(self):
        high = pd.Series([15.0, 13.0, 12.0, 10.0, 9.0])
        low = pd.Series([12.0, 11.0, 9.0, 8.0, 7.0])
        result = market_structure_from_pivots(
            pivot_highs=[0, 1], pivot_lows=[2, 3], high=high, low=low
        )
        assert result == "LH_LL"

    def test_fewer_than_two_pivots_each_side_is_range(self):
        high = pd.Series([10.0, 12.0])
        low = pd.Series([8.0, 9.0])
        assert market_structure_from_pivots([1], [0], high, low) == "range"


class TestRsiDivergenceFromPivots:
    def test_bullish_divergence_detected(self):
        low = pd.Series([10.0, 9.0, 8.0, 7.0])
        high = pd.Series([20.0, 21.0, 22.0, 23.0])
        rsi = pd.Series([30.0, 25.0, 35.0, 40.0])
        result = rsi_divergence_from_pivots(
            pivot_highs=[], pivot_lows=[1, 3], high=high, low=low, rsi=rsi
        )
        assert result == "bullish"

    def test_bearish_divergence_detected(self):
        high = pd.Series([20.0, 22.0, 24.0, 26.0])
        low = pd.Series([10.0, 11.0, 12.0, 13.0])
        rsi = pd.Series([70.0, 75.0, 65.0, 60.0])
        result = rsi_divergence_from_pivots(
            pivot_highs=[1, 3], pivot_lows=[], high=high, low=low, rsi=rsi
        )
        assert result == "bearish"

    def test_no_divergence_when_price_and_rsi_agree(self):
        low = pd.Series([10.0, 9.0, 8.0, 7.0])
        high = pd.Series([20.0, 21.0, 22.0, 23.0])
        rsi = pd.Series([30.0, 25.0, 20.0, 15.0])
        result = rsi_divergence_from_pivots(
            pivot_highs=[], pivot_lows=[1, 3], high=high, low=low, rsi=rsi
        )
        assert result == "none"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'find_pivots' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def find_pivots(values, k: int = 3, kind: str = "high") -> list[int]:
    """Fixed-lookback local extrema. An index needs k bars on both sides to be
    confirmed, so the most recent k bars never produce a pivot (expected/causal)."""
    arr = np.asarray(values, dtype=float)
    pivots: list[int] = []
    n = len(arr)
    for i in range(k, n - k):
        window = arr[i - k: i + k + 1]
        center = arr[i]
        if kind == "high" and center == window.max():
            pivots.append(i)
        elif kind == "low" and center == window.min():
            pivots.append(i)
    return pivots


def market_structure_from_pivots(
    pivot_highs: list[int], pivot_lows: list[int], high: pd.Series, low: pd.Series
) -> str:
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "range"
    h_prev, h_last = high.iloc[pivot_highs[-2]], high.iloc[pivot_highs[-1]]
    l_prev, l_last = low.iloc[pivot_lows[-2]], low.iloc[pivot_lows[-1]]
    if h_last > h_prev and l_last > l_prev:
        return "HH_HL"
    if h_last < h_prev and l_last < l_prev:
        return "LH_LL"
    return "range"


def rsi_divergence_from_pivots(
    pivot_highs: list[int], pivot_lows: list[int],
    high: pd.Series, low: pd.Series, rsi: pd.Series,
) -> str:
    if len(pivot_lows) >= 2:
        l_prev_idx, l_last_idx = pivot_lows[-2], pivot_lows[-1]
        price_lower_low = low.iloc[l_last_idx] < low.iloc[l_prev_idx]
        rsi_higher_low = rsi.iloc[l_last_idx] > rsi.iloc[l_prev_idx]
        if price_lower_low and rsi_higher_low:
            return "bullish"
    if len(pivot_highs) >= 2:
        h_prev_idx, h_last_idx = pivot_highs[-2], pivot_highs[-1]
        price_higher_high = high.iloc[h_last_idx] > high.iloc[h_prev_idx]
        rsi_lower_high = rsi.iloc[h_last_idx] < rsi.iloc[h_prev_idx]
        if price_higher_high and rsi_lower_high:
            return "bearish"
    return "none"
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add swing pivots, market structure, and RSI divergence

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 9: Indicators — support/resistance, fib levels, anchored VWAP

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import support_resistance_from_pivots, fib_levels_from_swing, anchored_vwap


class TestSupportResistanceFromPivots:
    def test_picks_nearest_pivot_below_and_above_price(self):
        high = pd.Series([110.0, 120.0, 130.0])
        low = pd.Series([90.0, 80.0, 70.0])
        support, resistance = support_resistance_from_pivots(
            pivot_highs=[0, 1, 2], pivot_lows=[0, 1, 2],
            high=high, low=low, last_close=100.0,
        )
        assert support == pytest.approx(90.0)
        assert resistance == pytest.approx(110.0)

    def test_falls_back_to_series_extremes_when_no_pivot_qualifies(self):
        high = pd.Series([50.0, 60.0])     # all below last_close -> no resistance pivot
        low = pd.Series([200.0, 210.0])    # all above last_close -> no support pivot
        support, resistance = support_resistance_from_pivots(
            pivot_highs=[0, 1], pivot_lows=[0, 1],
            high=high, low=low, last_close=100.0,
        )
        assert support == pytest.approx(200.0)   # low.min()
        assert resistance == pytest.approx(60.0)  # high.max()


class TestFibLevelsFromSwing:
    def test_levels_between_high_and_low(self):
        levels = fib_levels_from_swing(swing_high=200.0, swing_low=100.0)
        assert levels["38.2"] == pytest.approx(161.8)
        assert levels["50.0"] == pytest.approx(150.0)
        assert levels["61.8"] == pytest.approx(138.2)


class TestAnchoredVwap:
    def test_constant_price_gives_same_vwap(self):
        n = 10
        high = pd.Series(np.full(n, 101.0))
        low = pd.Series(np.full(n, 99.0))
        close = pd.Series(np.full(n, 100.0))
        volume = pd.Series(np.full(n, 1000.0))
        result = anchored_vwap(high, low, close, volume, anchor_idx=0)
        assert result == pytest.approx(100.0)

    def test_zero_volume_falls_back_to_last_close(self):
        n = 5
        high = pd.Series(np.full(n, 101.0))
        low = pd.Series(np.full(n, 99.0))
        close = pd.Series(np.full(n, 100.0))
        volume = pd.Series(np.zeros(n))
        result = anchored_vwap(high, low, close, volume, anchor_idx=0)
        assert result == pytest.approx(100.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'support_resistance_from_pivots' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def support_resistance_from_pivots(
    pivot_highs: list[int], pivot_lows: list[int],
    high: pd.Series, low: pd.Series, last_close: float,
) -> tuple[float, float]:
    """Nearest support (highest pivot low below price) and resistance (lowest pivot
    high above price); falls back to series extremes if no pivot qualifies."""
    low_values = [float(low.iloc[i]) for i in pivot_lows if low.iloc[i] < last_close]
    high_values = [float(high.iloc[i]) for i in pivot_highs if high.iloc[i] > last_close]
    support = max(low_values) if low_values else float(low.min())
    resistance = min(high_values) if high_values else float(high.max())
    return support, resistance


def fib_levels_from_swing(swing_high: float, swing_low: float) -> dict[str, float]:
    diff = swing_high - swing_low
    return {
        "38.2": swing_high - 0.382 * diff,
        "50.0": swing_high - 0.500 * diff,
        "61.8": swing_high - 0.618 * diff,
    }


def anchored_vwap(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, anchor_idx: int,
) -> float:
    typical = (high.iloc[anchor_idx:] + low.iloc[anchor_idx:] + close.iloc[anchor_idx:]) / 3.0
    vol = volume.iloc[anchor_idx:]
    total_vol = vol.sum()
    if total_vol == 0:
        return float(close.iloc[-1])
    return float((typical * vol).sum() / total_vol)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add support/resistance, fib levels, and anchored VWAP

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 10: Indicators — higher-timeframe trend, 52-week distance

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import htf_trend_from_weekly, dist_to_52w_extremes_pct


class TestHtfTrendFromWeekly:
    def test_uptrend_daily_series_gives_up(self):
        idx = pd.date_range("2024-01-01", periods=500, freq="D")
        close = pd.Series(np.linspace(100.0, 300.0, 500), index=idx)
        assert htf_trend_from_weekly(close) == "up"

    def test_downtrend_daily_series_gives_down(self):
        idx = pd.date_range("2024-01-01", periods=500, freq="D")
        close = pd.Series(np.linspace(300.0, 100.0, 500), index=idx)
        assert htf_trend_from_weekly(close) == "down"

    def test_short_history_gives_flat(self):
        idx = pd.date_range("2024-01-01", periods=20, freq="D")
        close = pd.Series(np.full(20, 100.0), index=idx)
        assert htf_trend_from_weekly(close) == "flat"


class TestDistTo52wExtremesPct:
    def test_price_at_high_gives_zero_distance_to_high(self):
        close = pd.Series(np.linspace(50.0, 100.0, 252))
        dist_high, dist_low = dist_to_52w_extremes_pct(close, last_close=100.0)
        assert dist_high == pytest.approx(0.0, abs=0.01)
        assert dist_low > 0

    def test_price_at_low_gives_zero_distance_to_low(self):
        close = pd.Series(np.linspace(50.0, 100.0, 252))
        dist_high, dist_low = dist_to_52w_extremes_pct(close, last_close=50.0)
        assert dist_low == pytest.approx(0.0, abs=0.01)
        assert dist_high < 0
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'htf_trend_from_weekly' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def htf_trend_from_weekly(daily_close: pd.Series, window_weeks: int = 30) -> str:
    """Resample to weekly closes, take a window_weeks SMA, compare now vs 4 weeks ago."""
    weekly = daily_close.resample("W").last().dropna()
    sma = weekly.rolling(window_weeks).mean()
    valid = sma.dropna()
    if len(valid) < 5:
        return "flat"
    now = valid.iloc[-1]
    past = valid.iloc[-5]
    if now > past:
        return "up"
    if now < past:
        return "down"
    return "flat"


def dist_to_52w_extremes_pct(close: pd.Series, last_close: float) -> tuple[float, float]:
    window = close.iloc[-252:] if len(close) >= 252 else close
    high_52w = float(window.max())
    low_52w = float(window.min())
    dist_to_high = (last_close - high_52w) / high_52w * 100.0 if high_52w != 0 else 0.0
    dist_to_low = (last_close - low_52w) / low_52w * 100.0 if low_52w != 0 else 0.0
    return dist_to_high, dist_to_low
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add higher-timeframe trend and 52-week distance indicators

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 11: Indicators — relative strength

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import relative_strength_pct, rs_line_slope


class TestRelativeStrengthPct:
    def test_outperformance_gives_positive_relative_strength(self):
        asset = pd.Series(np.linspace(100.0, 130.0, 70))   # +30%
        bench = pd.Series(np.linspace(100.0, 110.0, 70))   # +10%
        assert relative_strength_pct(asset, bench, window=60) > 0

    def test_underperformance_gives_negative_relative_strength(self):
        asset = pd.Series(np.linspace(100.0, 105.0, 70))   # +5%
        bench = pd.Series(np.linspace(100.0, 120.0, 70))   # +20%
        assert relative_strength_pct(asset, bench, window=60) < 0


class TestRsLineSlope:
    def test_asset_outpacing_benchmark_is_rising(self):
        asset = pd.Series(np.linspace(100.0, 150.0, 40))
        bench = pd.Series(np.linspace(100.0, 110.0, 40))
        assert rs_line_slope(asset, bench, window=20) == "rising"

    def test_asset_lagging_benchmark_is_falling(self):
        asset = pd.Series(np.linspace(100.0, 105.0, 40))
        bench = pd.Series(np.linspace(100.0, 150.0, 40))
        assert rs_line_slope(asset, bench, window=20) == "falling"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'relative_strength_pct' from 'technical.indicators'`

- [x] **Step 3: Implement**

Append to `trading bot/technical/indicators.py`:

```python
def relative_strength_pct(asset_close: pd.Series, bench_close: pd.Series, window: int) -> float:
    """Asset's window-bar return minus the benchmark's window-bar return (each
    measured positionally from its own series' end — both are daily US-market
    series so they cover the same trading days)."""
    asset_ret = pct_return(asset_close, bars_back=window)
    bench_ret = pct_return(bench_close, bars_back=window)
    return float(asset_ret - bench_ret)


def rs_line_slope(asset_close: pd.Series, bench_close: pd.Series, window: int = 20) -> str:
    n = min(len(asset_close), len(bench_close))
    if n <= window:
        return "flat"
    asset_tail = asset_close.iloc[-n:].to_numpy()
    bench_tail = bench_close.iloc[-n:].to_numpy()
    rs_line = asset_tail / bench_tail
    past = rs_line[-1 - window]
    now = rs_line[-1]
    if now > past:
        return "rising"
    if now < past:
        return "falling"
    return "flat"
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add relative-strength indicators

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 12: `TechnicalSnapshot` + `compute_snapshot` pipeline

**Files:**
- Modify: `trading bot/technical/indicators.py` (append)
- Modify: `trading bot/tests/test_technical_indicators.py` (append)

This task wires every function from Tasks 3–11 into one frozen dataclass and one pipeline function — the only entry point the rest of the codebase will call.

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_technical_indicators.py`:

```python
from technical.indicators import TechnicalSnapshot, compute_snapshot


def _make_ohlcv(n: int, start_price: float = 100.0, trend: float = 0.0) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame: close compounds by `trend` per bar (e.g. 0.003 = +0.3%/bar)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    closes = start_price * (1.0 + trend) ** np.arange(n)
    high = closes * 1.01
    low = closes * 0.99
    volume = np.full(n, 1_000_000.0)
    return pd.DataFrame({"High": high, "Low": low, "Close": closes, "Volume": volume}, index=idx)


class TestComputeSnapshot:
    def test_uptrend_snapshot_has_expected_shape_and_bullish_signals(self):
        ohlcv = _make_ohlcv(300, start_price=100.0, trend=0.003)
        spy_close = pd.Series(np.linspace(400.0, 420.0, 300))
        snapshot = compute_snapshot(
            ticker="TEST", ohlcv=ohlcv, spy_close=spy_close,
            sector_close=None, as_of="2026-06-17",
        )
        assert isinstance(snapshot, TechnicalSnapshot)
        assert snapshot.ticker == "TEST"
        assert snapshot.bars_available == 300
        assert snapshot.data_complete is True
        assert snapshot.ma_alignment == "bullish"
        assert snapshot.htf_trend == "up"
        assert snapshot.rs_vs_spy_3m_pct > 0
        assert snapshot.rs_vs_sector_3m_pct is None
        assert isinstance(snapshot.fib_levels, dict)

    def test_short_history_marks_data_incomplete_without_crashing(self):
        ohlcv = _make_ohlcv(50, start_price=100.0, trend=0.001)
        spy_close = pd.Series(np.linspace(400.0, 410.0, 50))
        snapshot = compute_snapshot(
            ticker="SHORT", ohlcv=ohlcv, spy_close=spy_close,
            sector_close=None, as_of="2026-06-17",
        )
        assert snapshot.bars_available == 50
        assert snapshot.data_complete is False

    def test_sector_close_provided_computes_relative_strength(self):
        ohlcv = _make_ohlcv(300, start_price=100.0, trend=0.003)
        spy_close = pd.Series(np.linspace(400.0, 420.0, 300))
        sector_close = pd.Series(np.linspace(50.0, 55.0, 300))
        snapshot = compute_snapshot(
            ticker="TEST", ohlcv=ohlcv, spy_close=spy_close,
            sector_close=sector_close, as_of="2026-06-17",
        )
        assert snapshot.rs_vs_sector_3m_pct is not None
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'TechnicalSnapshot' from 'technical.indicators'`

- [x] **Step 3: Implement**

Add `from dataclasses import dataclass` and `from risk.position_sizing import atr_pct_from_ohlc` to the top of `trading bot/technical/indicators.py` (alongside the existing `import numpy as np` / `import pandas as pd`), then append:

```python
@dataclass(frozen=True)
class TechnicalSnapshot:
    ticker: str
    as_of: str
    last_close: float
    htf_trend: str
    htf_above_200d: bool
    dist_to_52w_high_pct: float
    dist_to_52w_low_pct: float
    sma20: float
    sma50: float
    sma200: float
    ma_alignment: str
    sma200_slope_pct_20d: float
    price_vs_sma20_pct: float
    price_vs_sma50_pct: float
    market_structure: str
    ret_1m_pct: float
    ret_3m_pct: float
    ret_6m_pct: float
    ret_12m_1m_pct: float
    tsmom_composite: float
    rsi14: float
    rsi_regime: str
    rsi_divergence: str
    macd_hist: float
    macd_state: str
    atr_pct: float
    atr_pct_percentile_1y: float
    bb_percent_b: float
    bb_bandwidth_percentile_1y: float
    rel_volume_20d: float
    obv_trend: str
    volume_confirms_move: bool
    rs_vs_spy_3m_pct: float
    rs_vs_spy_6m_pct: float
    rs_vs_sector_3m_pct: float | None
    rs_line_slope: str
    nearest_support: float
    nearest_resistance: float
    dist_to_support_pct: float
    dist_to_resistance_pct: float
    fib_levels: dict[str, float]
    anchored_vwap_from_low: float
    bars_available: int
    data_complete: bool


def compute_snapshot(
    ticker: str,
    ohlcv: pd.DataFrame,
    spy_close: pd.Series,
    sector_close: pd.Series | None,
    as_of: str,
) -> TechnicalSnapshot:
    high, low, close, volume = ohlcv["High"], ohlcv["Low"], ohlcv["Close"], ohlcv["Volume"]
    bars_available = len(close)
    data_complete = bars_available >= 250
    last_close = float(close.iloc[-1])

    sma20_series = rolling_sma(close, 20)
    sma50_series = rolling_sma(close, 50)
    sma200_series = rolling_sma(close, 200)
    sma20 = float(sma20_series.iloc[-1]) if not pd.isna(sma20_series.iloc[-1]) else last_close
    sma50 = float(sma50_series.iloc[-1]) if not pd.isna(sma50_series.iloc[-1]) else last_close
    sma200 = float(sma200_series.iloc[-1]) if not pd.isna(sma200_series.iloc[-1]) else last_close

    htf_trend = htf_trend_from_weekly(close)
    dist_to_high_pct, dist_to_low_pct = dist_to_52w_extremes_pct(close, last_close)

    ret_1m = pct_return(close, 22)
    ret_3m = pct_return(close, 65)
    ret_6m = pct_return(close, 130)
    ret_12m_1m = momentum_12m_1m(close)
    tsmom = tsmom_composite(ret_1m, ret_3m, ret_12m_1m)

    rsi_series = compute_rsi(close, window=14)
    rsi14 = float(rsi_series.iloc[-1])
    rsi_regime = "overbought" if rsi14 >= 70 else "oversold" if rsi14 <= 30 else "neutral"

    _, _, macd_hist_arr = compute_macd(close)
    macd_hist = float(macd_hist_arr[-1])
    macd_state = macd_state_from_hist(macd_hist_arr)

    atr_pct = atr_pct_from_ohlc(high.values, low.values, close.values, window=14)
    atr_series = rolling_atr_pct(high, low, close, window=14).to_numpy()
    atr_pct_percentile_1y = _percentile_rank(atr_series, atr_pct, lookback=252)

    percent_b_arr, bandwidth_arr = bollinger_bands(close, window=20, num_std=2.0)
    last_percent_b = percent_b_arr[-1]
    bb_percent_b = float(last_percent_b) if not np.isnan(last_percent_b) else 0.5
    last_bandwidth = bandwidth_arr[-1]
    bb_bandwidth_percentile_1y = _percentile_rank(
        bandwidth_arr, float(last_bandwidth) if not np.isnan(last_bandwidth) else 0.0, lookback=252
    )

    rel_vol = rel_volume(volume, window=20)
    obv_series = compute_obv(close, volume)
    obv_trend = obv_trend_from_series(obv_series, window=20)
    vol_confirms = volume_confirms_move(close, rel_vol)

    pivot_highs = find_pivots(high.values, k=3, kind="high")
    pivot_lows = find_pivots(low.values, k=3, kind="low")
    market_structure = market_structure_from_pivots(pivot_highs, pivot_lows, high, low)
    rsi_divergence = rsi_divergence_from_pivots(pivot_highs, pivot_lows, high, low, rsi_series)
    support, resistance = support_resistance_from_pivots(pivot_highs, pivot_lows, high, low, last_close)
    dist_to_support_pct = (last_close - support) / support * 100.0 if support != 0 else 0.0
    dist_to_resistance_pct = (resistance - last_close) / last_close * 100.0 if last_close != 0 else 0.0

    if pivot_highs and pivot_lows:
        swing_high = float(high.iloc[pivot_highs[-1]])
        swing_low = float(low.iloc[pivot_lows[-1]])
    else:
        swing_high = float(high.max())
        swing_low = float(low.min())
    fib_levels = fib_levels_from_swing(swing_high, swing_low)

    low_idx = int(np.asarray(low.values).argmin())
    anchored_vwap_from_low = anchored_vwap(high, low, close, volume, anchor_idx=low_idx)

    rs_vs_spy_3m_pct = relative_strength_pct(close, spy_close, window=65)
    rs_vs_spy_6m_pct = relative_strength_pct(close, spy_close, window=130)
    rs_vs_sector_3m_pct = (
        relative_strength_pct(close, sector_close, window=65)
        if sector_close is not None else None
    )
    rs_slope = rs_line_slope(close, spy_close, window=20)

    return TechnicalSnapshot(
        ticker=ticker,
        as_of=as_of,
        last_close=last_close,
        htf_trend=htf_trend,
        htf_above_200d=last_close > sma200,
        dist_to_52w_high_pct=dist_to_high_pct,
        dist_to_52w_low_pct=dist_to_low_pct,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        ma_alignment=ma_alignment(sma20, sma50, sma200),
        sma200_slope_pct_20d=sma_slope_pct(sma200_series.dropna(), lookback=20),
        price_vs_sma20_pct=price_vs_sma_pct(last_close, sma20),
        price_vs_sma50_pct=price_vs_sma_pct(last_close, sma50),
        market_structure=market_structure,
        ret_1m_pct=ret_1m,
        ret_3m_pct=ret_3m,
        ret_6m_pct=ret_6m,
        ret_12m_1m_pct=ret_12m_1m,
        tsmom_composite=tsmom,
        rsi14=rsi14,
        rsi_regime=rsi_regime,
        rsi_divergence=rsi_divergence,
        macd_hist=macd_hist,
        macd_state=macd_state,
        atr_pct=atr_pct,
        atr_pct_percentile_1y=atr_pct_percentile_1y,
        bb_percent_b=bb_percent_b,
        bb_bandwidth_percentile_1y=bb_bandwidth_percentile_1y,
        rel_volume_20d=rel_vol,
        obv_trend=obv_trend,
        volume_confirms_move=vol_confirms,
        rs_vs_spy_3m_pct=rs_vs_spy_3m_pct,
        rs_vs_spy_6m_pct=rs_vs_spy_6m_pct,
        rs_vs_sector_3m_pct=rs_vs_sector_3m_pct,
        rs_line_slope=rs_slope,
        nearest_support=support,
        nearest_resistance=resistance,
        dist_to_support_pct=dist_to_support_pct,
        dist_to_resistance_pct=dist_to_resistance_pct,
        fib_levels=fib_levels,
        anchored_vwap_from_low=anchored_vwap_from_low,
        bars_available=bars_available,
        data_complete=data_complete,
    )
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_technical_indicators.py -v`
Expected: PASS (full file — confirms every Task 3–11 function composes correctly)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/technical/indicators.py" "trading bot/tests/test_technical_indicators.py" && git commit -m "$(cat <<'EOF'
feat: add TechnicalSnapshot and compute_snapshot pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 13: Structure-stop position sizing

**Files:**
- Modify: `trading bot/risk/position_sizing.py` (append, after `vol_pct_from_close`)
- Test: `trading bot/tests/test_position_sizing.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_position_sizing.py`:

```python
from risk.position_sizing import structure_stop_size_pct


class TestStructureStopSizePct:
    def test_close_stop_gives_larger_size_than_far_stop(self):
        close_stop = structure_stop_size_pct(100.0, 98.0, 0.15, 8.0)   # 2% stop distance
        far_stop = structure_stop_size_pct(100.0, 90.0, 0.15, 8.0)     # 10% stop distance
        assert close_stop > far_stop

    def test_two_pct_stop_distance_size(self):
        # stop_distance = (100-98)/100*100 = 2% -> 0.15/2*100 = 7.5%
        assert structure_stop_size_pct(100.0, 98.0, 0.15, 8.0) == pytest.approx(7.5)

    def test_caps_at_max_position_pct(self):
        # stop_distance = 0.5% -> 0.15/0.5*100 = 30% -> capped at 8.0
        assert structure_stop_size_pct(100.0, 99.5, 0.15, 8.0) == pytest.approx(8.0)

    def test_invalid_invalidation_price_uses_fallback_distance(self):
        # invalidation_price >= entry_price -> fallback to 1.0% distance -> 15% -> capped at 8.0
        assert structure_stop_size_pct(100.0, 100.0, 0.15, 8.0) == pytest.approx(8.0)
        assert structure_stop_size_pct(100.0, 105.0, 0.15, 8.0) == pytest.approx(8.0)

    def test_result_is_always_non_negative(self):
        for inval in [50.0, 80.0, 95.0, 99.0]:
            assert structure_stop_size_pct(100.0, inval, 0.15, 8.0) >= 0.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_position_sizing.py -v`
Expected: FAIL with `ImportError: cannot import name 'structure_stop_size_pct' from 'risk.position_sizing'`

- [x] **Step 3: Implement**

Append to `trading bot/risk/position_sizing.py`:

```python
def structure_stop_size_pct(
    entry_price: float,
    invalidation_price: float,
    per_trade_risk_pct: float,
    max_position_pct: float,
) -> float:
    """size_pct = clamp(per_trade_risk_pct / stop_distance_pct, 0, max_position_pct).

    stop_distance_pct = (entry_price - invalidation_price) / entry_price * 100.
    Falls back to a 1.0% distance (matching atr_pct_from_ohlc's fallback style)
    if invalidation_price >= entry_price (should already be rejected upstream).
    """
    stop_distance_pct = (entry_price - invalidation_price) / entry_price * 100.0
    if stop_distance_pct <= 0:
        stop_distance_pct = 1.0
    raw = per_trade_risk_pct / stop_distance_pct * 100.0
    return float(min(max(raw, 0.0), max_position_pct))
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_position_sizing.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/risk/position_sizing.py" "trading bot/tests/test_position_sizing.py" && git commit -m "$(cat <<'EOF'
feat: add structure-stop position sizing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 14: `TechnicalScore` + `parse_technical_response`

**Files:**
- Modify: `trading bot/bot/ai_analyst.py` (insert after `_BOTH_BONUS`, and after `ExitDecision`/`parse_exit_response`)
- Test: `trading bot/tests/test_ai_analyst.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_ai_analyst.py`:

```python
from bot.ai_analyst import TechnicalScore, parse_technical_response


def _technical_payload(**overrides):
    base = {
        "conviction": 8, "entry": "buy", "setup_type": "pullback_to_support",
        "trend_alignment": "bullish", "momentum_state": "rsi_rising",
        "volume_confirmation": "confirmed", "relative_strength": "outperforming",
        "entry_trigger": "reclaim of 20-day SMA", "invalidation_price": 95.0,
        "target_price": 115.0, "reward_risk": 3.0,
        "key_levels": "support 95, resistance 115",
        "conflicts": [], "rationale": "Clean setup", "risk_flags": [],
    }
    base.update(overrides)
    return json.dumps(base)


class TestParseTechnicalResponse:
    def test_valid_buy_passes_through(self):
        # last_close=100: reward_risk = (115-100)/(100-95) = 3.0, matches reported 3.0
        score = parse_technical_response(_technical_payload(), last_close=100.0)
        assert isinstance(score, TechnicalScore)
        assert score.entry == "buy"
        assert score.reward_risk == pytest.approx(3.0)
        assert score.conviction == 8

    def test_invalidation_above_last_close_downgrades_to_skip(self):
        payload = _technical_payload(invalidation_price=105.0)  # >= last_close=100
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"
        assert score.reward_risk == 0.0

    def test_target_below_last_close_downgrades_to_skip(self):
        payload = _technical_payload(target_price=95.0)  # <= last_close=100
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"
        assert score.reward_risk == 0.0

    def test_reward_risk_mismatch_downgrades_to_skip(self):
        # Geometry is valid (recomputed reward_risk=3.0) but model self-reports 10.0
        payload = _technical_payload(reward_risk=10.0)
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"
        assert score.reward_risk == 0.0

    def test_skip_entry_is_not_escalated(self):
        payload = _technical_payload(entry="skip")
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            parse_technical_response("not json", last_close=100.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -k TestParseTechnicalResponse -v`
Expected: FAIL with `ImportError: cannot import name 'TechnicalScore' from 'bot.ai_analyst'`

- [x] **Step 3: Implement**

In `trading bot/bot/ai_analyst.py`, insert the following block right after the existing `_BOTH_BONUS` string (currently lines 55-57, before `_RESEARCH_ADJUSTMENTS`):

```python
_TECHNICAL_SCHEMA = """You are a technical analyst gating a trade candidate on timing and risk geometry.
Respond with ONLY valid JSON matching this exact schema:
{"conviction": <int 1-10>, "entry": <"buy"|"skip">, "setup_type": <str>,
 "trend_alignment": <str>, "momentum_state": <str>, "volume_confirmation": <str>,
 "relative_strength": <str>, "entry_trigger": <str>, "invalidation_price": <float>,
 "target_price": <float>, "reward_risk": <float>, "key_levels": <str>,
 "conflicts": [<str>], "rationale": <str>, "risk_flags": [<str>]}

## Risk-First Discipline
- This is a timing/risk-geometry filter, not a fresh source of alpha. The fundamental or
  congressional signal has already cleared the entry bar — your job is to judge whether
  RIGHT NOW is a structurally sound place to enter, and where the trade is proven wrong.
- invalidation_price MUST be below the current price (the structural stop — where the
  setup is invalidated, e.g. below the nearest support or below a key moving average).
- target_price MUST be above the current price (a realistic level — e.g. the next
  resistance, prior swing high, or measured move).
- reward_risk MUST equal (target_price - last_close) / (last_close - invalidation_price),
  computed from the same two prices you return. Do not round it independently.

## Confluence Rules
- Count confirming factors: trend alignment (price above rising SMAs), momentum
  (RSI/MACD agreeing with trend direction), volume confirmation (move backed by
  above-average volume), relative strength (outperforming SPY/sector), and a clean
  entry trigger (pullback to support, breakout, reclaim of a moving average).
- A conflict (e.g. RSI divergence against the move, price into resistance, momentum
  fading, weak relative strength) subtracts from confluence and must be listed in
  `conflicts`.

## Setup Classification
- setup_type must be one of: "breakout", "pullback_to_support", "trend_continuation",
  "reversal", "range_bound", "no_clean_setup".

## Regime Overlay
- In "bear"/"crash"/"deep-bear" regimes: require stronger confluence (more confirming
  factors, tighter invalidation) before buy — momentum/breakout setups are riskier when
  the tape is weak.
- In "bull"/"euphoria"/"melt-up" regimes: pullback and trend-continuation setups are
  favored; breakouts chasing an extended move need volume confirmation.

## Decision Rule — set entry="buy" ONLY if ALL of the following hold:
1. reward_risk >= 2.0
2. at least 3 confirming factors support the setup (see Confluence Rules)
3. the setup type suits the current regime (see Regime Overlay)
4. the entry is not being taken directly into nearby resistance
5. there is no disqualifying conflict (e.g. active bearish RSI divergence on a long entry)
Otherwise set entry="skip". False negatives are cheaper than false positives — when in
doubt, skip."""

_TECHNICAL_BOTH_BONUS = """
## Combined Signal Note
This candidate already carries a fundamental and/or congressional signal that passed the
entry bar. Do not let that bias your timing judgment — score the chart on its own merits.
A strong fundamental/congressional signal with poor risk geometry right now should still
be scored "skip" (the position can be revisited later at a better entry)."""


_REWARD_RISK_TOLERANCE = 0.05  # 5% relative tolerance on the model's self-reported reward:risk
```

Then, immediately after the existing `ExitDecision` dataclass and before `parse_entry_response` (or anywhere after `EntryScore`/`ExitDecision` are defined — exact placement doesn't matter as long as it's after the dataclasses), add:

```python
@dataclass(frozen=True)
class TechnicalScore:
    conviction: int
    entry: str
    setup_type: str
    trend_alignment: str
    momentum_state: str
    volume_confirmation: str
    relative_strength: str
    entry_trigger: str
    invalidation_price: float
    target_price: float
    reward_risk: float
    key_levels: str
    conflicts: tuple[str, ...]
    rationale: str
    risk_flags: tuple[str, ...]
```

Then, after `parse_exit_response`, add:

```python
def parse_technical_response(text: str, last_close: float) -> TechnicalScore:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid JSON for technical score: {text!r}") from exc

    conviction = int(data["conviction"])
    if not (1 <= conviction <= 10):
        raise ValueError(f"conviction {conviction} out of range 1-10")
    entry = data["entry"]
    if entry not in _VALID_ENTRY_VALUES:
        raise ValueError(f"entry {entry!r} not in {_VALID_ENTRY_VALUES}")

    invalidation_price = float(data["invalidation_price"])
    target_price = float(data["target_price"])
    reported_reward_risk = float(data["reward_risk"])

    valid_geometry = 0 < invalidation_price < last_close < target_price
    if valid_geometry:
        recomputed_reward_risk = (target_price - last_close) / (last_close - invalidation_price)
        matches = abs(recomputed_reward_risk - reported_reward_risk) <= (
            _REWARD_RISK_TOLERANCE * max(abs(recomputed_reward_risk), 1e-9)
        )
    else:
        recomputed_reward_risk = 0.0
        matches = False

    if entry == "buy" and (not valid_geometry or not matches):
        entry = "skip"
        reward_risk = 0.0
    else:
        reward_risk = recomputed_reward_risk if valid_geometry else 0.0

    return TechnicalScore(
        conviction=conviction,
        entry=entry,
        setup_type=data.get("setup_type", ""),
        trend_alignment=data.get("trend_alignment", ""),
        momentum_state=data.get("momentum_state", ""),
        volume_confirmation=data.get("volume_confirmation", ""),
        relative_strength=data.get("relative_strength", ""),
        entry_trigger=data.get("entry_trigger", ""),
        invalidation_price=invalidation_price,
        target_price=target_price,
        reward_risk=reward_risk,
        key_levels=data.get("key_levels", ""),
        conflicts=tuple(data.get("conflicts", [])),
        rationale=data["rationale"],
        risk_flags=tuple(data.get("risk_flags", [])),
    )
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS (full file, no regressions)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/ai_analyst.py" "trading bot/tests/test_ai_analyst.py" && git commit -m "$(cat <<'EOF'
feat: add TechnicalScore and parse_technical_response

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 15: `score_technical` LLM gate call

**Files:**
- Modify: `trading bot/bot/ai_analyst.py` (append, after `review_exit`)
- Test: `trading bot/tests/test_ai_analyst.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_ai_analyst.py`:

```python
from bot.ai_analyst import score_technical
from technical.indicators import TechnicalSnapshot


def _make_snapshot(**overrides) -> TechnicalSnapshot:
    base = dict(
        ticker="AAPL", as_of="2026-06-17", last_close=100.0,
        htf_trend="up", htf_above_200d=True,
        dist_to_52w_high_pct=-5.0, dist_to_52w_low_pct=40.0,
        sma20=99.0, sma50=95.0, sma200=90.0, ma_alignment="bullish",
        sma200_slope_pct_20d=2.0, price_vs_sma20_pct=1.0, price_vs_sma50_pct=5.0,
        market_structure="HH_HL",
        ret_1m_pct=3.0, ret_3m_pct=8.0, ret_6m_pct=15.0, ret_12m_1m_pct=20.0,
        tsmom_composite=0.3,
        rsi14=60.0, rsi_regime="neutral", rsi_divergence="none",
        macd_hist=0.5, macd_state="bullish_expanding",
        atr_pct=2.0, atr_pct_percentile_1y=50.0,
        bb_percent_b=0.7, bb_bandwidth_percentile_1y=40.0,
        rel_volume_20d=1.2, obv_trend="rising", volume_confirms_move=True,
        rs_vs_spy_3m_pct=2.0, rs_vs_spy_6m_pct=5.0, rs_vs_sector_3m_pct=1.0,
        rs_line_slope="rising",
        nearest_support=95.0, nearest_resistance=110.0,
        dist_to_support_pct=5.0, dist_to_resistance_pct=10.0,
        fib_levels={"38.2": 97.0, "50.0": 95.0, "61.8": 93.0},
        anchored_vwap_from_low=96.0,
        bars_available=300, data_complete=True,
    )
    base.update(overrides)
    return TechnicalSnapshot(**base)


def test_score_technical_returns_technical_score(mocker):
    _mock_claude(mocker, _technical_payload())
    result = score_technical(_make_snapshot(), regime_label="bull", signal_type="fundamental")
    assert isinstance(result, TechnicalScore)
    assert result.entry == "buy"


def test_score_technical_both_includes_bonus_text_in_system_prompt(mocker):
    _mock_claude(mocker, _technical_payload())
    score_technical(_make_snapshot(), regime_label="bull", signal_type="both")
    import bot.ai_analyst as m
    system_text = m._get_client().messages.create.call_args[1]["system"][0]["text"]
    assert "Combined Signal Note" in system_text


def test_score_technical_congressional_omits_bonus_text(mocker):
    _mock_claude(mocker, _technical_payload())
    score_technical(_make_snapshot(), regime_label="bull", signal_type="congressional")
    import bot.ai_analyst as m
    system_text = m._get_client().messages.create.call_args[1]["system"][0]["text"]
    assert "Combined Signal Note" not in system_text
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -k score_technical -v`
Expected: FAIL with `ImportError: cannot import name 'score_technical' from 'bot.ai_analyst'`

- [x] **Step 3: Implement**

Append to `trading bot/bot/ai_analyst.py`:

```python
def _build_technical_prompt(snapshot: "TechnicalSnapshot", regime_label: str) -> str:
    fib = ", ".join(f"{k}%={v:.2f}" for k, v in snapshot.fib_levels.items())
    rs_sector = (
        "n/a" if snapshot.rs_vs_sector_3m_pct is None
        else f"{snapshot.rs_vs_sector_3m_pct:+.2f}%"
    )
    lines = [
        f"Ticker: {snapshot.ticker} | As of: {snapshot.as_of} | "
        f"Last close: {snapshot.last_close:.2f} | Regime: {regime_label}",
        f"HTF trend: {snapshot.htf_trend} | Above 200d SMA: {snapshot.htf_above_200d} | "
        f"Dist to 52w high: {snapshot.dist_to_52w_high_pct:.1f}% | "
        f"Dist to 52w low: {snapshot.dist_to_52w_low_pct:.1f}%",
        f"Trend: SMA20={snapshot.sma20:.2f} SMA50={snapshot.sma50:.2f} SMA200={snapshot.sma200:.2f} "
        f"alignment={snapshot.ma_alignment} sma200_slope_20d={snapshot.sma200_slope_pct_20d:.2f}% "
        f"price_vs_sma20={snapshot.price_vs_sma20_pct:+.2f}% price_vs_sma50={snapshot.price_vs_sma50_pct:+.2f}% "
        f"market_structure={snapshot.market_structure}",
        f"TS-momentum: ret_1m={snapshot.ret_1m_pct:+.2f}% ret_3m={snapshot.ret_3m_pct:+.2f}% "
        f"ret_6m={snapshot.ret_6m_pct:+.2f}% ret_12m_1m={snapshot.ret_12m_1m_pct:+.2f}% "
        f"tsmom_composite={snapshot.tsmom_composite:+.2f}",
        f"Oscillators: RSI14={snapshot.rsi14:.1f} ({snapshot.rsi_regime}) "
        f"divergence={snapshot.rsi_divergence} MACD_hist={snapshot.macd_hist:+.3f} "
        f"macd_state={snapshot.macd_state}",
        f"Volatility: ATR%={snapshot.atr_pct:.2f} (pct1y={snapshot.atr_pct_percentile_1y:.0f}) "
        f"BB%B={snapshot.bb_percent_b:.2f} BB_bandwidth_pct1y={snapshot.bb_bandwidth_percentile_1y:.0f}",
        f"Volume: rel_volume_20d={snapshot.rel_volume_20d:.2f}x obv_trend={snapshot.obv_trend} "
        f"volume_confirms_move={snapshot.volume_confirms_move}",
        f"Relative strength: vs_SPY_3m={snapshot.rs_vs_spy_3m_pct:+.2f}% "
        f"vs_SPY_6m={snapshot.rs_vs_spy_6m_pct:+.2f}% vs_sector_3m={rs_sector} "
        f"rs_line_slope={snapshot.rs_line_slope}",
        f"Levels: support={snapshot.nearest_support:.2f} resistance={snapshot.nearest_resistance:.2f} "
        f"dist_to_support={snapshot.dist_to_support_pct:.2f}% dist_to_resistance={snapshot.dist_to_resistance_pct:.2f}% "
        f"fib=[{fib}] anchored_vwap_from_low={snapshot.anchored_vwap_from_low:.2f}",
        f"Data quality: bars_available={snapshot.bars_available} data_complete={snapshot.data_complete}",
    ]
    return "\n".join(lines)


def score_technical(
    snapshot: "TechnicalSnapshot",
    regime_label: str,
    signal_type: str = "fundamental",
) -> TechnicalScore:
    prompt = _build_technical_prompt(snapshot, regime_label)
    system_text = _TECHNICAL_SCHEMA
    if signal_type == "both":
        system_text += _TECHNICAL_BOTH_BONUS

    def _call():
        return parse_technical_response(_claude_call(system_text, prompt), last_close=snapshot.last_close)

    return _call_with_retry(_call)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_ai_analyst.py -v`
Expected: PASS (full file, no regressions)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/ai_analyst.py" "trading bot/tests/test_ai_analyst.py" && git commit -m "$(cat <<'EOF'
feat: add score_technical LLM gate call

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 16: `positions.stop_pct` column + `insert_position` param

**Files:**
- Modify: `trading bot/bot/db.py:154-175` (`_MIGRATIONS`), `:242-260` (`insert_position`)
- Test: `trading bot/tests/test_db.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_db.py`:

```python
def test_insert_position_defaults_stop_pct_to_15(db):
    db.insert_disclosures([{
        "id": "sp-001", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    sid = db.insert_signal("sp-001", "AAPL", 7, 4.0, "test", [])
    db.insert_position("AAPL", 100.0, 10.0, 4.0, "2026-04-28", sid, "test")
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["stop_pct"] == pytest.approx(15.0)


def test_insert_position_stores_custom_stop_pct(db):
    db.insert_disclosures([{
        "id": "sp-002", "politician": "Jane", "ticker": "MSFT",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    sid = db.insert_signal("sp-002", "MSFT", 7, 4.0, "test", [])
    db.insert_position("MSFT", 200.0, 5.0, 4.0, "2026-04-28", sid, "test", stop_pct=3.5)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "MSFT")
    assert pos["stop_pct"] == pytest.approx(3.5)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_db.py -k stop_pct -v`
Expected: FAIL with `sqlite3.OperationalError: no such column: stop_pct` (or `KeyError`/`IndexError` from the row mapping)

- [x] **Step 3: Implement**

In `trading bot/bot/db.py`, add a 5th migration to `_MIGRATIONS` (currently lines 154-175), after the `take_profit_taken`/`entry_commission` migrations:

```python
    (
        5,
        "Add stop_pct to positions",
        "ALTER TABLE positions ADD COLUMN stop_pct REAL NOT NULL DEFAULT 15.0",
    ),
```

Then change `insert_position` (currently lines 242-260) from:

```python
def insert_position(ticker: str, entry_price: float, shares: float,
                    position_pct: float, entry_date: str,
                    signal_id: int | None, rationale: str,
                    signal_source: str = "congressional",
                    entry_commission: float = 0.0) -> None:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO positions
               (ticker, entry_price, shares, position_pct, entry_date, signal_id,
                rationale, peak_price, signal_source, entry_commission)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, entry_price, shares, position_pct, entry_date, signal_id,
             rationale, entry_price, signal_source, entry_commission),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Position already exists for {ticker} — cannot open duplicate. "
                "Close the existing position before re-entering."
            )
```

to:

```python
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
```

(`get_open_positions()` already does `SELECT *`, so `stop_pct` appears automatically — no change needed there.)

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_db.py -v`
Expected: PASS (full file, no regressions)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/db.py" "trading bot/tests/test_db.py" && git commit -m "$(cat <<'EOF'
feat: add positions.stop_pct column and insert_position param

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 17: `Portfolio.open_position` gains `initial_stop_pct`

**Files:**
- Modify: `trading bot/bot/portfolio.py:35-93` (`open_position`)
- Test: `trading bot/tests/test_portfolio.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_portfolio.py`:

```python
def test_open_position_uses_initial_stop_pct_when_provided(mock_broker, db):
    """A structural stop width overrides the global trailing_stop_pct for both
    the resting broker stop and the persisted positions.stop_pct."""
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("AAPL", 5.0, None, "test", 100.0, initial_stop_pct=2.0)
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    assert call_kwargs["stop_price"] == pytest.approx(98.0)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["stop_pct"] == pytest.approx(2.0)


def test_open_position_default_stop_pct_unchanged_when_not_provided(mock_broker, db):
    """initial_stop_pct=None (default) must behave exactly as before this feature."""
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("MSFT", 5.0, None, "test", 200.0)
    from system.config import settings
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    expected_stop = 200.0 * (1 - settings.risk.trailing_stop_pct / 100)
    assert call_kwargs["stop_price"] == pytest.approx(expected_stop)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "MSFT")
    assert pos["stop_pct"] == pytest.approx(settings.risk.trailing_stop_pct)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_portfolio.py -k initial_stop_pct -v`
Expected: FAIL with `TypeError: open_position() got an unexpected keyword argument 'initial_stop_pct'`

- [x] **Step 3: Implement**

In `trading bot/bot/portfolio.py`, change `open_position` (currently lines 35-93) from:

```python
    def open_position(self, ticker: str, position_pct: float, signal_id: int | None,
                      rationale: str, entry_price: float,
                      signal_source: str = "congressional") -> bool:
        """Returns True if position was successfully opened."""
        position_pct = min(position_pct, self._risk.max_position_pct)
```

to:

```python
    def open_position(self, ticker: str, position_pct: float, signal_id: int | None,
                      rationale: str, entry_price: float,
                      signal_source: str = "congressional",
                      initial_stop_pct: float | None = None) -> bool:
        """Returns True if position was successfully opened."""
        position_pct = min(position_pct, self._risk.max_position_pct)
        stop_pct_used = (
            initial_stop_pct if initial_stop_pct is not None else self._risk.trailing_stop_pct
        )
```

then change the `db.insert_position(...)` call inside the same method from:

```python
            db.insert_position(
                ticker=ticker,
                entry_price=actual_entry_price,
                shares=actual_shares,
                position_pct=position_pct,
                entry_date=date.today().isoformat(),
                signal_id=signal_id,
                rationale=rationale,
                signal_source=signal_source,
                entry_commission=entry_commission,
            )
```

to:

```python
            db.insert_position(
                ticker=ticker,
                entry_price=actual_entry_price,
                shares=actual_shares,
                position_pct=position_pct,
                entry_date=date.today().isoformat(),
                signal_id=signal_id,
                rationale=rationale,
                signal_source=signal_source,
                entry_commission=entry_commission,
                stop_pct=stop_pct_used,
            )
```

and finally change the resting-stop line from:

```python
        stop_price = actual_entry_price * (1 - self._risk.trailing_stop_pct / 100)
        self.broker.place_stop_order(ticker=ticker, qty=actual_shares, stop_price=stop_price)
```

to:

```python
        stop_price = actual_entry_price * (1 - stop_pct_used / 100)
        self.broker.place_stop_order(ticker=ticker, qty=actual_shares, stop_price=stop_price)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_portfolio.py -v`
Expected: PASS (full file, no regressions — `test_open_position_registers_stop_order` and `test_open_position_stop_uses_custom_trailing_pct` must still pass unchanged)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/portfolio.py" "trading bot/tests/test_portfolio.py" && git commit -m "$(cat <<'EOF'
feat: support per-position initial_stop_pct in open_position

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 18: `enforce_stop_losses` reads per-position `stop_pct`

**Files:**
- Modify: `trading bot/bot/portfolio.py:258-317` (`enforce_stop_losses`)
- Modify: `trading bot/tests/test_portfolio.py:298-315` (fix one existing test — see Step 1)
- Test: `trading bot/tests/test_portfolio.py` (append two new tests)

**Important — read before starting:** the existing test `test_portfolio_reads_stop_loss_from_config` (line ~298) currently inserts a position via `db.insert_position(...)` *without* a `stop_pct`, then asserts that `enforce_stop_losses()` with no override falls back to a custom `RiskConfig(trailing_stop_pct=5.0)` injected into `Portfolio`. That was correct under the old global-constant design. Under the new per-position design, a position's *own stored* `stop_pct` (set once, at open time) is what governs its stop — not whatever `RiskConfig.trailing_stop_pct` happens to be at poll time. This is the entire point of Component 6 (a structural stop must keep its own width as it trails, immune to later config edits). So this existing test's premise is now stale and must be fixed as part of this task, not left to break.

- [x] **Step 1: Write the failing test**

First, fix the existing test. In `trading bot/tests/test_portfolio.py`, change `test_portfolio_reads_stop_loss_from_config` from:

```python
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
```

to:

```python
def test_portfolio_reads_stop_loss_from_config(mock_broker, db):
    # 6% drop — triggers a 5% per-position stop_pct, would NOT trigger the 15% DB default.
    # NOTE: enforce_stop_losses() with no override now reads each position's OWN stored
    # stop_pct (set at open time), not RiskConfig.trailing_stop_pct directly — see
    # Component 6 of the technical-analysis-layer change. risk_cfg is still passed since
    # Portfolio requires a risk_cfg instance, but it no longer drives this stop's width.
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
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=5.0)
    closed = p.enforce_stop_losses()   # no explicit pct — reads the position's own stop_pct
    assert "AAPL" in closed
```

Then append two new tests to the same file:

```python
def test_enforce_stop_losses_uses_per_position_stop_pct_with_no_override(mock_broker, db):
    """Each position's own stop_pct governs its trailing/closing when no
    explicit stop_loss_pct override is given to enforce_stop_losses()."""
    portfolio = Portfolio(broker=mock_broker)
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
        {"ticker": "MSFT", "qty": 5.0, "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "pp-stop-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("pp-stop-01", "AAPL", 8, 5.0, "Good", [])
    # AAPL: tight 5% structural stop -> a 6% drop from peak closes it
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=5.0)
    # MSFT: default 15% stop -> the same 6% drop does NOT close it
    db.insert_position("MSFT", 100.0, 5.0, 4.0, "2026-04-01", None, "Test")

    closed = portfolio.enforce_stop_losses()

    assert "AAPL" in closed
    assert "MSFT" not in closed


def test_enforce_stop_losses_explicit_override_ignores_per_position_stop_pct(mock_broker, db):
    """An explicit stop_loss_pct override (used by hedge-scoped polling) must
    apply uniformly, even when a position has its own stored stop_pct."""
    portfolio = Portfolio(broker=mock_broker)
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "pp-stop-02", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("pp-stop-02", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=5.0)

    # 6% drop from peak would close under the position's own 5% stop_pct, but the
    # explicit 20% override must keep it open.
    closed = portfolio.enforce_stop_losses(stop_loss_pct=20.0)

    assert "AAPL" not in closed
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_portfolio.py -k "per_position_stop_pct or explicit_override_ignores" -v`
Expected: FAIL — `test_enforce_stop_losses_uses_per_position_stop_pct_with_no_override` fails because MSFT (no per-position override, 6% drop) closes today since `enforce_stop_losses()` currently uses the single global `trailing_stop_pct` (15% by default, so a 6% drop should NOT close it today either — verify by reading the assertion: actually re-check this fails for the right reason, i.e. AAPL not closing since today's code uses one global pct, not per-position; rerun and confirm the actual failure message before moving to Step 3)

- [x] **Step 3: Implement**

In `trading bot/bot/portfolio.py`, change `enforce_stop_losses` (currently lines 258-317) from:

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
```

to:

```python
    def enforce_stop_losses(
        self,
        stop_loss_pct: float | None = None,
        source_include: str | None = None,
        source_exclude: str | None = None,
    ) -> list[str]:
        if source_include is not None and source_exclude is not None:
            raise ValueError("source_include and source_exclude are mutually exclusive")
        default_pct = self._risk.trailing_stop_pct
        closed = []
        open_positions = {p["ticker"]: dict(p) for p in db.get_open_positions()}

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

            # An explicit override (e.g. hedge-scoped polling) applies uniformly and
            # ignores the stored per-position value. With no override, each position
            # trails/closes at its OWN stop_pct (set once at open time), not whatever
            # RiskConfig.trailing_stop_pct happens to be right now.
            pct = stop_loss_pct if stop_loss_pct is not None else (meta.get("stop_pct") or default_pct)

            current = pos["current_price"]
            peak = meta.get("peak_price") or pos["avg_entry_price"]
            db.update_position_peak(ticker, current)
```

The rest of the method (the resting-stop trail logic and the `drop_from_peak >= pct` close, lines ~288-317) already references the local `pct` variable and needs no further changes.

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_portfolio.py -v`
Expected: PASS (full file — confirms the fixed test, the two new tests, and every other existing test in the file, which all pass an explicit `stop_loss_pct` and are therefore unaffected)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/portfolio.py" "trading bot/tests/test_portfolio.py" && git commit -m "$(cat <<'EOF'
feat: read per-position stop_pct in enforce_stop_losses

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 19: Cached ETF close-history fetch

**Files:**
- Modify: `trading bot/bot/signal_engine.py` (top imports, and after `clear_sector_cache`)
- Test: `trading bot/tests/test_signal_engine.py` (top imports, append)

- [x] **Step 1: Write the failing test**

In `trading bot/tests/test_signal_engine.py`, extend the existing import block (currently lines 2-6) from:

```python
from bot.signal_engine import (
    compute_lag_days, is_qualified_signal, filter_disclosures,
    parse_amount_min_usd, is_large_enough_trade, get_cluster_count,
    get_sector_for_ticker, clear_sector_cache,
)
```

to:

```python
from bot.signal_engine import (
    compute_lag_days, is_qualified_signal, filter_disclosures,
    parse_amount_min_usd, is_large_enough_trade, get_cluster_count,
    get_sector_for_ticker, clear_sector_cache,
    get_etf_close_history, clear_etf_cache,
)
```

Then append to the end of the file:

```python
import pandas as pd


def test_get_etf_close_history_is_cached():
    """get_etf_close_history should call yf.Ticker only once for repeated lookups."""
    clear_etf_cache()
    mock_ticker = MagicMock()
    mock_hist = pd.DataFrame({"Close": [400.0, 401.0, 402.0]})
    mock_ticker.history.return_value = mock_hist
    with patch("bot.signal_engine.yf.Ticker", return_value=mock_ticker) as mock_yf:
        result1 = get_etf_close_history("SPY")
        result2 = get_etf_close_history("SPY")
    assert result1 is result2
    mock_yf.assert_called_once_with("SPY")
    clear_etf_cache()  # cleanup
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_signal_engine.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_etf_close_history' from 'bot.signal_engine'`

- [x] **Step 3: Implement**

In `trading bot/bot/signal_engine.py`, add `import pandas as pd` to the top imports (currently lines 1-9), then add the following after `clear_sector_cache` (currently lines 25-26):

```python
@functools.lru_cache(maxsize=32)
def get_etf_close_history(etf_ticker: str, period: str = "2y") -> pd.Series:
    return yf.Ticker(etf_ticker).history(period=period)["Close"]


def clear_etf_cache() -> None:
    get_etf_close_history.cache_clear()
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_signal_engine.py -v`
Expected: PASS (full file, no regressions)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/bot/signal_engine.py" "trading bot/tests/test_signal_engine.py" && git commit -m "$(cat <<'EOF'
feat: add cached ETF close-history fetch

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 20: Wire technical gate into `_process_signal` (congressional)

**Files:**
- Modify: `trading bot/orchestration/main_loop.py` (imports, `_get_sector_etf_close` helper, `_process_signal`)
- Test: `trading bot/tests/test_orchestrator.py` (append)

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_orchestrator.py`:

```python
def test_technical_gate_off_by_default_skips_gate(mocker, orch):
    """enable_technical_gate=False (default) must not call compute_snapshot/score_technical
    and must pass initial_stop_pct=None through to open_position — byte-for-byte
    unchanged behavior vs. before this feature."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)
    snapshot_spy = mocker.patch("orchestration.main_loop.compute_snapshot")
    score_spy = mocker.patch("orchestration.main_loop.score_technical")

    disc = {
        "id": "tg1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    snapshot_spy.assert_not_called()
    score_spy.assert_not_called()
    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] is None


def test_technical_gate_on_skip_rejects_signal(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=4, entry="skip", setup_type="no_clean_setup",
                     trend_alignment="mixed", momentum_state="fading",
                     volume_confirmation="unconfirmed", relative_strength="lagging",
                     entry_trigger="none", invalidation_price=0.0, target_price=0.0,
                     reward_risk=0.0, key_levels="", conflicts=(), rationale="weak setup",
                     risk_flags=(),
                 ))

    disc = {
        "id": "tg2", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_technical_gate_on_buy_passes_structure_stop_pct(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=8, entry="buy", setup_type="pullback_to_support",
                     trend_alignment="bullish", momentum_state="rising",
                     volume_confirmation="confirmed", relative_strength="outperforming",
                     entry_trigger="reclaim of 20-day SMA", invalidation_price=98.0,
                     target_price=110.0, reward_risk=6.0, key_levels="support 98",
                     conflicts=(), rationale="clean setup", risk_flags=(),
                 ))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg3", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] == pytest.approx(2.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_orchestrator.py -k technical_gate -v`
Expected: FAIL — `test_technical_gate_off_by_default_skips_gate` fails with `TypeError: open_position() got an unexpected keyword argument 'initial_stop_pct'` (the other two fail similarly, plus `AttributeError`/`ImportError` for `compute_snapshot`/`score_technical`/`TechnicalScore` not yet wired into `orchestration.main_loop`)

- [x] **Step 3: Implement**

In `trading bot/orchestration/main_loop.py`, update imports. Add `import pandas as pd` after `import numpy as np` (line 33). Change line 43 from:

```python
from bot.signal_engine import filter_disclosures, get_sector_for_ticker, compute_lag_days, get_cluster_count, clear_sector_cache
```

to:

```python
from bot.signal_engine import (
    filter_disclosures, get_sector_for_ticker, compute_lag_days, get_cluster_count,
    clear_sector_cache, get_etf_close_history, clear_etf_cache,
)
```

Change line 45 from:

```python
from bot.ai_analyst import score_entry_with_debate, review_exit, EntryScore
```

to:

```python
from bot.ai_analyst import score_entry_with_debate, review_exit, EntryScore, score_technical
```

Change line 58 from:

```python
from risk.position_sizing import vol_target_size_pct, apply_conviction_tilt, atr_pct_from_ohlc
```

to:

```python
from risk.position_sizing import vol_target_size_pct, apply_conviction_tilt, atr_pct_from_ohlc, structure_stop_size_pct
```

Add two new import lines after line 63 (`from risk.correlation import CorrelationFilter`):

```python
from technical.indicators import compute_snapshot
from technical.sector_map import SECTOR_ETF_MAP
```

Add a new helper function right after `_ma_conviction_delta` (currently lines 81-99):

```python
def _get_sector_etf_close(sector: str) -> pd.Series | None:
    etf = SECTOR_ETF_MAP.get(sector)
    if etf is None:
        return None
    try:
        return get_etf_close_history(etf)
    except Exception:
        return None
```

In `run_morning_pipeline`, change the existing cache-clear line (currently line 368) from:

```python
        clear_sector_cache()
```

to:

```python
        clear_sector_cache()
        clear_etf_cache()
```

Now rewrite the ATR/sizing block inside `_process_signal` (currently lines 615-638) from:

```python
        # ATR-based deterministic position sizing + MA50/MA200 conviction modifier.
        # Fetch 1y so we have enough history for both ATR (last 14 bars) and MA200.
        ma_delta = 0
        try:
            hist = _t.history(period="1y")
            atr_pct = atr_pct_from_ohlc(
                hist["High"].values, hist["Low"].values, hist["Close"].values,
                window=self._cfg.sizing.atr_window,
            )
            ma_delta = _ma_conviction_delta(hist["Close"].values, entry_price)
        except Exception:
            atr_pct = _ATR_FALLBACK_PCT

        conviction = max(1, min(10, score.conviction + ma_delta))
        base_pct = vol_target_size_pct(
            atr_pct=atr_pct,
            per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
        base_pct = apply_conviction_tilt(
            base_pct=base_pct,
            conviction=conviction,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
```

to:

```python
        # ATR-based deterministic position sizing + MA50/MA200 conviction modifier.
        # Fetch 2y: enough history for ATR/MA200 plus the technical-snapshot pipeline.
        ma_delta = 0
        try:
            hist = _t.history(period="2y")
            atr_pct = atr_pct_from_ohlc(
                hist["High"].values, hist["Low"].values, hist["Close"].values,
                window=self._cfg.sizing.atr_window,
            )
            ma_delta = _ma_conviction_delta(hist["Close"].values, entry_price)
        except Exception:
            atr_pct = _ATR_FALLBACK_PCT
            hist = None

        conviction = max(1, min(10, score.conviction + ma_delta))

        initial_stop_pct: float | None = None
        if self._cfg.sizing.enable_technical_gate and hist is not None:
            try:
                snapshot = compute_snapshot(
                    ticker=ticker,
                    ohlcv=hist,
                    spy_close=get_etf_close_history("SPY"),
                    sector_close=_get_sector_etf_close(sector),
                    as_of=date.today().isoformat(),
                )
                tech_score = score_technical(
                    snapshot,
                    regime_label=self._regime_state.regime_label if self._regime_state else "neutral",
                    signal_type="congressional",
                )
            except Exception:
                log.exception("Technical gate failed for %s — rejecting", ticker)
                return False
            if (tech_score.entry != "buy"
                    or tech_score.reward_risk < self._cfg.sizing.min_reward_risk):
                emit_event(log, EventType.SIGNAL_REJECTED,
                           f"{ticker} rejected by technical gate (entry={tech_score.entry}, "
                           f"reward_risk={tech_score.reward_risk:.2f})")
                return False
            base_pct = structure_stop_size_pct(
                entry_price=entry_price,
                invalidation_price=tech_score.invalidation_price,
                per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
                max_position_pct=self._cfg.risk.max_position_pct,
            )
            initial_stop_pct = (entry_price - tech_score.invalidation_price) / entry_price * 100
        else:
            base_pct = vol_target_size_pct(
                atr_pct=atr_pct,
                per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
                max_position_pct=self._cfg.risk.max_position_pct,
            )
        base_pct = apply_conviction_tilt(
            base_pct=base_pct,
            conviction=conviction,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
```

Finally, pass `initial_stop_pct` through to `open_position` — change (currently lines 702-705):

```python
        self._portfolio.open_position(
            ticker=ticker, position_pct=final_pct,
            signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
        )
```

to:

```python
        self._portfolio.open_position(
            ticker=ticker, position_pct=final_pct,
            signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
            initial_stop_pct=initial_stop_pct,
        )
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_orchestrator.py -v`
Expected: PASS (full file, no regressions — every pre-existing test calls `open_position` with the gate off, so `initial_stop_pct=None` flows through identically to before)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/orchestration/main_loop.py" "trading bot/tests/test_orchestrator.py" && git commit -m "$(cat <<'EOF'
feat: wire technical gate into congressional signal processing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 21: Wire technical gate into `_process_fundamental_candidate`

**Files:**
- Modify: `trading bot/orchestration/main_loop.py` (`_process_fundamental_candidate`)
- Test: `trading bot/tests/test_orchestrator.py` (append)

This mirrors Task 20 exactly, inside `_process_fundamental_candidate` (starts at line 714 as of Task 20's edits having already landed — locate it by function name, since Task 20 shifted line numbers below it). The only difference: use the existing local `signal_type` variable (`"both"` or `"fundamental"`) instead of the hardcoded `"congressional"` string.

- [x] **Step 1: Write the failing test**

Append to `trading bot/tests/test_orchestrator.py`:

```python
def test_fundamental_technical_gate_off_by_default_skips_gate(mocker, orch):
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)
    snapshot_spy = mocker.patch("orchestration.main_loop.compute_snapshot")
    score_spy = mocker.patch("orchestration.main_loop.score_technical")

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    orch._process_fundamental_candidate(candidate, {}, set())

    snapshot_spy.assert_not_called()
    score_spy.assert_not_called()
    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] is None


def test_fundamental_technical_gate_on_skip_rejects_candidate(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=4, entry="skip", setup_type="no_clean_setup",
                     trend_alignment="mixed", momentum_state="fading",
                     volume_confirmation="unconfirmed", relative_strength="lagging",
                     entry_trigger="none", invalidation_price=0.0, target_price=0.0,
                     reward_risk=0.0, key_levels="", conflicts=(), rationale="weak setup",
                     risk_flags=(),
                 ))

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    result = orch._process_fundamental_candidate(candidate, {}, set())

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_fundamental_technical_gate_on_buy_passes_structure_stop_pct(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=8, entry="buy", setup_type="pullback_to_support",
                     trend_alignment="bullish", momentum_state="rising",
                     volume_confirmation="confirmed", relative_strength="outperforming",
                     entry_trigger="reclaim of 20-day SMA", invalidation_price=98.0,
                     target_price=110.0, reward_risk=6.0, key_levels="support 98",
                     conflicts=(), rationale="clean setup", risk_flags=(),
                 ))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    orch._process_fundamental_candidate(candidate, {}, set())

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] == pytest.approx(2.0)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_orchestrator.py -k fundamental_technical_gate -v`
Expected: FAIL with `TypeError: open_position() got an unexpected keyword argument 'initial_stop_pct'`

- [x] **Step 3: Implement**

In `trading bot/orchestration/main_loop.py`, inside `_process_fundamental_candidate`, find the block (mirroring the pre-Task-20 shape of `_process_signal`):

```python
        # ATR-based deterministic position sizing + MA50/MA200 conviction modifier.
        # Fetch 1y so we have enough history for both ATR (last 14 bars) and MA200.
        ma_delta = 0
        try:
            hist = _t.history(period="1y")
            atr_pct = atr_pct_from_ohlc(
                hist["High"].values, hist["Low"].values, hist["Close"].values,
                window=self._cfg.sizing.atr_window,
            )
            ma_delta = _ma_conviction_delta(hist["Close"].values, entry_price)
        except Exception:
            atr_pct = _ATR_FALLBACK_PCT

        conviction = max(1, min(10, score.conviction + ma_delta))
        base_pct = vol_target_size_pct(
            atr_pct=atr_pct,
            per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
        base_pct = apply_conviction_tilt(
            base_pct=base_pct,
            conviction=conviction,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
```

and replace it with:

```python
        # ATR-based deterministic position sizing + MA50/MA200 conviction modifier.
        # Fetch 2y: enough history for ATR/MA200 plus the technical-snapshot pipeline.
        ma_delta = 0
        try:
            hist = _t.history(period="2y")
            atr_pct = atr_pct_from_ohlc(
                hist["High"].values, hist["Low"].values, hist["Close"].values,
                window=self._cfg.sizing.atr_window,
            )
            ma_delta = _ma_conviction_delta(hist["Close"].values, entry_price)
        except Exception:
            atr_pct = _ATR_FALLBACK_PCT
            hist = None

        conviction = max(1, min(10, score.conviction + ma_delta))

        initial_stop_pct: float | None = None
        if self._cfg.sizing.enable_technical_gate and hist is not None:
            try:
                snapshot = compute_snapshot(
                    ticker=ticker,
                    ohlcv=hist,
                    spy_close=get_etf_close_history("SPY"),
                    sector_close=_get_sector_etf_close(sector),
                    as_of=date.today().isoformat(),
                )
                tech_score = score_technical(
                    snapshot,
                    regime_label=self._regime_state.regime_label if self._regime_state else "neutral",
                    signal_type=signal_type,
                )
            except Exception:
                log.exception("Technical gate failed for %s (%s) — rejecting", ticker, signal_type)
                return False
            if (tech_score.entry != "buy"
                    or tech_score.reward_risk < self._cfg.sizing.min_reward_risk):
                emit_event(
                    log, EventType.SIGNAL_REJECTED,
                    f"{ticker} ({signal_type}) rejected by technical gate "
                    f"(entry={tech_score.entry}, reward_risk={tech_score.reward_risk:.2f})",
                )
                return False
            base_pct = structure_stop_size_pct(
                entry_price=entry_price,
                invalidation_price=tech_score.invalidation_price,
                per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
                max_position_pct=self._cfg.risk.max_position_pct,
            )
            initial_stop_pct = (entry_price - tech_score.invalidation_price) / entry_price * 100
        else:
            base_pct = vol_target_size_pct(
                atr_pct=atr_pct,
                per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
                max_position_pct=self._cfg.risk.max_position_pct,
            )
        base_pct = apply_conviction_tilt(
            base_pct=base_pct,
            conviction=conviction,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
```

Then pass `initial_stop_pct` through to `open_position` — change:

```python
        self._portfolio.open_position(
            ticker=ticker,
            position_pct=final_pct,
            signal_id=None,
            rationale=score.rationale,
            entry_price=entry_price,
            signal_source=signal_type,
        )
```

to:

```python
        self._portfolio.open_position(
            ticker=ticker,
            position_pct=final_pct,
            signal_id=None,
            rationale=score.rationale,
            entry_price=entry_price,
            signal_source=signal_type,
            initial_stop_pct=initial_stop_pct,
        )
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest tests/test_orchestrator.py -v`
Expected: PASS (full file, no regressions)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/orchestration/main_loop.py" "trading bot/tests/test_orchestrator.py" && git commit -m "$(cat <<'EOF'
feat: wire technical gate into fundamental candidate processing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

---

## Task 22: Full regression + document in `trading bot/CLAUDE.md`

**Files:**
- Modify: `trading bot/CLAUDE.md` (header test count, "Verifying changes" test count, new "Gotchas" bullets)

- [x] **Step 1: Run the full suite and capture the count**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest -q`
Expected: all tests PASS; note the final count reported (e.g. `623 passed`) — it must be the 552 baseline plus every test added in Tasks 1–21. If anything fails, stop and fix it before continuing (do not edit CLAUDE.md against a red suite).

- [x] **Step 2: Confirm gate-off behavior is unchanged for every pre-existing test**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest -q -k "not technical"`
Expected: same pass count as before this feature branch existed minus the new gate-specific tests filtered out by `-k` — i.e. zero failures among tests that predate this plan. This confirms `enable_technical_gate=False` (the default) didn't change any existing behavior.

- [x] **Step 3: Update `trading bot/CLAUDE.md`**

Change the header line (currently):

```
> Hardening plans A–E are complete (552 tests green as of 2026-06-16).
```

to (using the real count from Step 1, and today's date):

```
> Hardening plans A–E are complete; the technical-analysis gate (config-gated, default
> off) landed 2026-06-17 — <N> tests green.
```

Change the "Verifying changes" section's:

```
pytest                                 # 552 tests; keep green (run from inside trading bot/)
```

to:

```
pytest                                 # <N> tests; keep green (run from inside trading bot/)
```

Then add the following bullets to the end of the "Gotchas" section (after the existing "Stops are resting broker orders…" bullet):

```markdown
- **Technical-analysis gate (config-gated, default off):** `SizingConfig.enable_technical_gate`
  (default `False`) inserts a deterministic indicator pipeline (`technical/indicators.py`,
  `TechnicalSnapshot`/`compute_snapshot`) plus one extra Claude call
  (`bot/ai_analyst.score_technical`) after the existing AI entry score, in both
  `_process_signal` and `_process_fundamental_candidate`. When off, behavior is
  byte-for-byte identical to before (the `hist` fetch widened from `period="1y"` to
  `period="2y"` is the only universal change, reused by both the existing ATR/MA-delta
  code and the new gate). When on: a `"skip"` or `reward_risk < SizingConfig.min_reward_risk`
  (default 2.0) rejects the candidate; a `"buy"` switches sizing from `vol_target_size_pct`
  to `risk.position_sizing.structure_stop_size_pct` (risk-budget ÷ stop-distance, using the
  model's `invalidation_price`) and passes a per-position `initial_stop_pct` through to
  `Portfolio.open_position`. Technical conviction never blends into `EntryScore.conviction`
  or `ma_delta` — it only gates pass/fail and drives sizing/stop inputs, kept separate for
  auditability.
- **`positions.stop_pct` column (schema v5):** every position now carries its own stop
  width (`NOT NULL DEFAULT 15.0`), set at open time by `Portfolio.open_position`'s
  `initial_stop_pct` param (falls back to `RiskConfig.trailing_stop_pct` when not given —
  today's default path). `enforce_stop_losses()` with **no** explicit `stop_loss_pct`
  override now reads each position's own stored `stop_pct`, not the live
  `RiskConfig.trailing_stop_pct` — a position's stop width is fixed at entry, not
  retroactively changed by later config edits. An explicit `stop_loss_pct` override (used
  by hedge-scoped polling) still applies uniformly and ignores the per-position value,
  exactly as before.
- **`technical/` package:** hand-rolled indicators only (no TA-Lib/pandas-ta) —
  `technical/indicators.py` (pure functions + `TechnicalSnapshot`/`compute_snapshot`) and
  `technical/sector_map.py` (GICS sector string → sector ETF ticker, used for
  relative-strength vs. sector; unmapped sectors are treated as neutral, not an error).
```

- [x] **Step 4: Run the full suite one more time**

Run: `cd "/Users/thomasvromen/Documents/Claude code test/trading bot" && pytest -q`
Expected: same pass count as Step 1 (CLAUDE.md changes don't affect test outcomes — this just re-confirms nothing is broken before the final commit)

- [x] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test" && git add "trading bot/CLAUDE.md" && git commit -m "$(cat <<'EOF'
docs: document technical-analysis gate in trading bot CLAUDE.md

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" && git push
```

