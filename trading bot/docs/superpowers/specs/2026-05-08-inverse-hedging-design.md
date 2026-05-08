# Inverse ETF Hedging — Design Spec

**Date:** 2026-05-08
**Status:** Approved

## Goal

When the HMM regime engine classifies the market as bear, deep-bear, or crash, the bot
allocates to a predefined universe of single-inverse ETFs instead of sitting idle in cash.
Inverse positions exit immediately when the regime transitions back to neutral or bull.
Individual stock shorting is out of scope.

---

## Architecture

A new `HedgeEngine` class (`hedge/hedge_engine.py`) owns all inverse-hedging logic.
The orchestrator calls it as a black box — it does not bleed into `AllocationEngine` or
the existing signal pipeline.

### New files
| File | Purpose |
|---|---|
| `hedge/__init__.py` | package marker |
| `hedge/hedge_engine.py` | `HedgeEngine` + `HedgeOrder` dataclass |
| `tests/test_hedge_engine.py` | unit tests for HedgeEngine |

### Modified files
| File | Change |
|---|---|
| `system/config.py` | Add `HedgeConfig`; add `enable_inverse_hedging` to `RiskConfig`; add `hedge: HedgeConfig` to `Settings` |
| `orchestration/main_loop.py` | Instantiate `HedgeEngine`; wire `_run_hedge_pass()` and `_run_hedge_exits()` into morning pipeline |
| `monitoring/logger.py` | Add `HEDGE_ENTRY`, `HEDGE_EXIT`, `HEDGE_STOP_LOSS` to `EventType` |
| `bot/portfolio.py` | Add `source_include: str | None` and `source_exclude: str | None` to `enforce_stop_losses` |

### No DB schema changes
Hedge positions use `signal_source="hedge"` and `signal_id=None` — both already
supported by the `positions` and `closed_positions` tables.

---

## Config

```python
@dataclass(frozen=True)
class HedgeConfig:
    # ETF ticker → sectors it conflicts with (empty = no sector filter)
    inverse_etf_universe: dict[str, list[str]] = field(default_factory=lambda: {
        "SH":  [],                                         # broad S&P 500
        "PSQ": ["Technology", "Communication Services"],   # Nasdaq
        "RWM": [],                                         # Russell 2000
        "SBB": [],                                         # short small-cap
        "EFZ": [],                                         # short MSCI EAFE
    })
    # Max total inverse allocation (% of NAV) per regime label
    max_inverse_pct_by_regime: dict[str, float] = field(default_factory=lambda: {
        "bear":      30.0,
        "crash":     50.0,
        "deep-bear": 50.0,
    })
    max_single_position_pct: float = 15.0   # per-ETF cap (% of NAV)
    conflict_threshold_pct: float = 10.0    # block ETF if portfolio > this % in its sectors
    stop_loss_pct: float = 10.0             # tighter than long 15%
```

`RiskConfig` gets: `enable_inverse_hedging: bool = True`

---

## HedgeEngine API

```python
@dataclass(frozen=True)
class HedgeOrder:
    ticker: str
    position_pct: float   # % of NAV
    rationale: str

class HedgeEngine:
    def __init__(self, cfg: Settings) -> None: ...

    def is_hedge_regime(self, regime_state: RegimeState) -> bool:
        """True if regime label is in max_inverse_pct_by_regime keys."""

    def compute_hedge_plan(
        self,
        regime_state: RegimeState,
        open_positions_meta: list[dict],    # from db.get_open_positions()
        sector_allocation: dict[str, float],  # % by sector from current longs
        nav: float,
    ) -> list[HedgeOrder]:
        """
        Returns orders to open. Already-open hedges and conflicting ETFs are excluded.
        """

    def get_exits_needed(self, open_positions_meta: list[dict]) -> list[str]:
        """Returns tickers where signal_source == 'hedge'."""
```

---

## compute_hedge_plan Algorithm

1. Return `[]` if `enable_inverse_hedging` is False or regime is not a hedge regime
2. Get `max_alloc = max_inverse_pct_by_regime[regime_label]`
3. Identify already-open hedge tickers from `open_positions_meta` where `signal_source == "hedge"`
4. For each ETF in `inverse_etf_universe`:
   - Skip if already open
   - Skip if any of the ETF's conflict sectors has portfolio allocation > `conflict_threshold_pct`
5. If no eligible ETFs, return `[]`
6. `alloc_per_etf = min(max_alloc / len(eligible), max_single_position_pct)`
7. Return one `HedgeOrder` per eligible ETF with `position_pct = alloc_per_etf`

---

## Orchestrator Integration

### New instance variable
```python
self._hedge_engine = HedgeEngine(self._cfg)
self._prev_regime_was_hedge: bool = False
```

### `run_morning_pipeline()` additions

After existing stop-loss/take-profit enforcement — split by source:
```python
# Longs: existing thresholds, skip hedges
self._portfolio.enforce_stop_losses(source_exclude="hedge")
self._portfolio.enforce_take_profits(source_exclude="hedge")
# Hedges: tighter stop-loss, no take-profit
self._portfolio.enforce_stop_losses(
    stop_loss_pct=self._cfg.hedge.stop_loss_pct,
    source_include="hedge",
)
```

After regime update, check for transition and run exits:
```python
is_hedge_now = self._hedge_engine.is_hedge_regime(self._regime_state)
if self._prev_regime_was_hedge and not is_hedge_now:
    self._run_hedge_exits()
self._prev_regime_was_hedge = is_hedge_now
```

After long signal processing, run hedge entry pass:
```python
if is_hedge_now and not _at_capacity:
    self._run_hedge_pass()
```

### `_run_hedge_pass()`
1. Compute `sector_allocation` from open long positions (same logic as signal pipeline)
2. Call `hedge_engine.compute_hedge_plan(regime_state, open_positions_meta, sector_allocation, nav)`
3. For each `HedgeOrder`:
   - Fetch price via yfinance
   - Call `portfolio.open_position(ticker, position_pct, signal_id=None, rationale=order.rationale, signal_source="hedge")`
   - Emit `HEDGE_ENTRY` event with regime label
4. Wrap in try/except — a failed hedge pass must not crash the loop

### `_run_hedge_exits()`
1. Call `hedge_engine.get_exits_needed(open_positions_meta)`
2. For each ticker:
   - Fetch current price
   - Call `portfolio.close_position(...)` with `signal_source="hedge"`, `exit_reason="regime_transition"`
   - Emit `HEDGE_EXIT` event with entry regime label and current regime label
3. Wrap in try/except

---

## Portfolio Changes

`enforce_stop_losses` signature:
```python
def enforce_stop_losses(
    self,
    stop_loss_pct: float | None = None,
    source_include: str | None = None,  # only process this signal_source
    source_exclude: str | None = None,  # skip this signal_source
) -> list[str]:
```

When both are None — existing behavior (all positions). `source_include` and
`source_exclude` are mutually exclusive; raise `ValueError` if both set.

`enforce_take_profits` gets the same `source_exclude` parameter (no `source_include`
needed since hedges never take profit — they exit on regime change only).

---

## Logging

New `EventType` values:
```python
HEDGE_ENTRY = "hedge_entry"
HEDGE_EXIT  = "hedge_exit"
HEDGE_STOP_LOSS = "hedge_stop_loss"   # fired from enforce_stop_losses when source="hedge"
```

`HEDGE_ENTRY` payload: `{ticker, position_pct, regime_label, regime_confidence, rationale}`
`HEDGE_EXIT` payload: `{ticker, exit_reason, entry_regime, exit_regime, realized_pnl}`

Both fire `alert=True` so they reach the webhook/log sender from the alerts rework.

---

## Kill Switch

`settings.risk.enable_inverse_hedging = False` makes `compute_hedge_plan` return `[]`
immediately. No other code path changes. Existing hedge positions (opened before the
kill-switch was flipped) are unaffected — they exit via the normal regime-transition
logic or stop-loss.

---

## Testing Strategy

`tests/test_hedge_engine.py` covers:
- `is_hedge_regime` returns True for bear/crash/deep-bear, False for neutral/bull
- `compute_hedge_plan` returns empty list when kill-switch is False
- `compute_hedge_plan` returns empty list when regime is neutral
- `compute_hedge_plan` excludes already-open hedge tickers
- `compute_hedge_plan` excludes ETFs with conflicting sector allocation
- `compute_hedge_plan` equal-weights eligible ETFs
- `compute_hedge_plan` caps each position at `max_single_position_pct`
- `compute_hedge_plan` caps total allocation at regime limit
- `get_exits_needed` returns only positions where `signal_source == "hedge"`
- `get_exits_needed` returns empty list when no hedge positions open

`tests/test_portfolio.py` additions:
- `enforce_stop_losses(source_include="hedge")` only processes hedge positions
- `enforce_stop_losses(source_exclude="hedge")` skips hedge positions
- Both `source_include` and `source_exclude` set → `ValueError`

`tests/test_orchestrator.py` additions:
- `_run_hedge_pass` called when regime is bear, skipped when neutral
- `_run_hedge_exits` called when regime transitions from bear → neutral
- `_run_hedge_exits` not called when regime stays bear
