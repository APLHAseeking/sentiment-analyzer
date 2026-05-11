# Correlation-Aware Position Sizing — Design Spec

**Date:** 2026-05-11
**Status:** Approved

## Goal

Reduce position size when a candidate is highly correlated with an existing holding, using 60-day rolling returns. Preserves opportunity in profitable sectors while limiting redundant exposure. Portfolio volatility targeting is out of scope.

---

## Architecture

A new `CorrelationFilter` class in `risk/correlation.py` follows the same pattern as `AllocationEngine` and `RiskManager` — pure computation class, injected into the orchestrator, independently testable.

### New files
| File | Purpose |
|---|---|
| `risk/correlation.py` | `CorrelationFilter` class |
| `tests/test_correlation.py` | Unit tests |

### Modified files
| File | Change |
|---|---|
| `system/config.py` | Add `CorrelationConfig`; add `correlation: CorrelationConfig` to `Settings` |
| `orchestration/main_loop.py` | Instantiate filter; load returns at pipeline start; apply multiplier in both signal paths; clear at end |

---

## Config

```python
@dataclass(frozen=True)
class CorrelationConfig:
    threshold: float = 0.7        # ρ at or below this → no reduction
    window_days: int = 60         # daily return lookback period
    min_overlap_days: int = 20    # skip correlation check if fewer shared days
```

Insert between `HedgeConfig` and `RiskConfig` in `system/config.py`.
Add `correlation: CorrelationConfig = field(default_factory=CorrelationConfig)` to `Settings` after `hedge`.

---

## CorrelationFilter

```python
class CorrelationFilter:
    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg.correlation
        self._holdings_returns: dict[str, pd.Series] = {}
        self._candidate_cache: dict[str, pd.Series] = {}

    def load_holdings_returns(self, tickers: list[str]) -> None:
        """Pre-fetch window_days returns for long holdings. Call once per morning."""

    def size_multiplier(self, candidate_ticker: str) -> float:
        """Return multiplier ∈ [0.0, 1.0].

        multiplier = max(0.0, 1.0 - (max_ρ - threshold) / (1.0 - threshold))

        Returns 1.0 (no penalty) when:
        - no holdings are loaded
        - max_ρ ≤ threshold
        - yfinance fails for candidate
        - insufficient shared trading days with all holdings
        """

    def clear(self) -> None:
        """Reset returns cache. Call at end of morning pipeline."""
```

### `load_holdings_returns` logic

1. If tickers is empty, set `_holdings_returns = {}` and return
2. `raw = yf.download(tickers, period="3mo", auto_adjust=True, progress=False)`
3. Extract `Close` column (handle MultiIndex if multiple tickers)
4. Compute daily pct_change, drop NaN
5. For each ticker, store the return Series in `_holdings_returns`
6. On any exception: log warning, set `_holdings_returns = {}`, return

### `size_multiplier` logic

1. If `_holdings_returns` is empty → return 1.0
2. Fetch candidate returns (check `_candidate_cache` first):
   - `raw = yf.download(candidate_ticker, period="3mo", auto_adjust=True, progress=False)`
   - Compute daily pct_change, drop NaN
   - Store in `_candidate_cache[candidate_ticker]`
   - On any exception: log warning, return 1.0
3. For each holding in `_holdings_returns`:
   - Align candidate and holding returns on common dates
   - If overlap < `min_overlap_days` → skip this holding
   - Compute Pearson correlation
4. If no valid pairwise correlations computed → return 1.0
5. `max_ρ = max of all valid pairwise correlations`
6. If `max_ρ ≤ threshold` → return 1.0
7. Return `max(0.0, 1.0 - (max_ρ - threshold) / (1.0 - threshold))`

### `clear` logic

Set `_holdings_returns = {}` and `_candidate_cache = {}`.

---

## Orchestrator Integration

### `__init__`

After `self._hedge_engine = HedgeEngine(self._cfg)`:

```python
self._corr_filter = CorrelationFilter(self._cfg)
```

### `run_morning_pipeline` — load returns

After the invested-pct capacity check and scrape (inside the `if not _at_capacity:` block, before the congressional signal loop):

```python
# Pre-load holdings returns for correlation filter
_long_tickers = [
    pos["ticker"] for pos in get_open_positions()
    if pos.get("signal_source") != "hedge"
]
self._corr_filter.load_holdings_returns(_long_tickers)
```

### `run_morning_pipeline` — clear at end

At the very end of `run_morning_pipeline`, after Phase 3 (hedge pass):

```python
self._corr_filter.clear()
```

### `_process_signal` — apply multiplier

After regime allocation (`alloc_decision = self._alloc.compute(...)`) and before the risk veto:

```python
corr_mult = self._corr_filter.size_multiplier(ticker)
final_pct *= corr_mult
if final_pct < 0.1:
    emit_event(log, EventType.SIGNAL_REJECTED,
               f"{ticker} reduced to zero by correlation filter (mult={corr_mult:.2f})")
    return
```

### `_process_fundamental_candidate` — apply multiplier

Same placement (after regime allocation, before risk veto):

```python
corr_mult = self._corr_filter.size_multiplier(ticker)
final_pct *= corr_mult
if final_pct < 0.1:
    emit_event(log, EventType.SIGNAL_REJECTED,
               f"{ticker} ({signal_type}) reduced to zero by correlation filter (mult={corr_mult:.2f})")
    return False
```

---

## Testing

`tests/test_correlation.py`:

- `test_size_multiplier_returns_one_when_no_holdings` — empty holdings → 1.0
- `test_size_multiplier_returns_one_below_threshold` — ρ = 0.5 → 1.0
- `test_size_multiplier_scales_linearly_above_threshold` — ρ = 0.85, threshold = 0.7 → 0.5
- `test_size_multiplier_returns_zero_at_perfect_correlation` — ρ = 1.0 → 0.0
- `test_size_multiplier_returns_one_on_candidate_yfinance_failure` — yfinance raises → 1.0
- `test_size_multiplier_uses_max_correlation_across_holdings` — two holdings, max ρ used
- `test_size_multiplier_skips_holdings_with_insufficient_overlap` — < min_overlap_days → skip, return 1.0
- `test_load_holdings_returns_handles_empty_tickers` — no-op, returns safely
- `test_load_holdings_returns_handles_yfinance_failure` — exception → empty cache
- `test_clear_resets_both_caches`
