# Technical Analysis Layer — Design Spec

**Date:** 2026-06-17
**Status:** Approved

## Goal

Add a deterministic technical-indicator pipeline plus an LLM synthesis stage that gates entries on timing/risk-geometry, and use its structural invalidation level to replace the blunt fixed 15% stop and the ATR-only sizing formula for gated trades. This is a confluence/risk-geometry filter on top of the existing fundamental + congressional signals, not a new alpha source. Source design: pasted "Technical Analysis Layer + Missing-Components Roadmap" doc, Part A only (Part B items are separate future work).

## Design Principle

Additive and config-gated. Default behavior (`SizingConfig.enable_technical_gate = False`) is byte-for-byte identical to today. No new dependency — indicators are hand-rolled in numpy/pandas, matching the existing `atr_pct_from_ohlc` pattern in `risk/position_sizing.py`. Reuse existing fetches and existing LLM-call infrastructure (`bot/ai_analyst.py`'s `_call_with_retry`/client) rather than building parallel plumbing.

---

## Architecture

Decision flow (`orchestration/main_loop.py`), TA gate inserted after the existing entry score/debate:

```
event-calendar gate → gather_research → AI entry score/debate (existing)
  → [NEW] technical gate (skipped entirely if enable_technical_gate=False)
  → regime allocation scaling → correlation filter → portfolio-vol gate → risk-manager veto → open_position
```

Both `_process_signal` (congressional) and `_process_fundamental_candidate` (fundamental/both) get the same gate — it runs on every signal_type that reaches this point.

---

## Component 1: Indicator pipeline (`technical/indicators.py`)

New module. Pure functions over a `pandas.DataFrame` (High/Low/Close/Volume, oldest→newest) plus benchmark/sector-ETF close series. All causal — only uses rows up to the last completed bar passed in.

`TechnicalSnapshot` frozen dataclass, fields as specified in the source doc's Part A.2:
- HTF context: `htf_trend`, `htf_above_200d`, `dist_to_52w_high_pct`, `dist_to_52w_low_pct`
- Trend: `sma20/50/200`, `ma_alignment`, `sma200_slope_pct_20d`, `price_vs_sma20_pct`, `price_vs_sma50_pct`, `market_structure` (HH_HL/LH_LL/range via fixed-lookback swing pivots)
- TS-momentum: `ret_1m/3m/6m_pct`, `ret_12m_1m_pct`, `tsmom_composite`
- Oscillators: `rsi14`, `rsi_regime`, `rsi_divergence` (computed from price/RSI pivot comparison), `macd_hist`, `macd_state`
- Volatility: `atr_pct`, `atr_pct_percentile_1y`, `bb_percent_b`, `bb_bandwidth_percentile_1y`
- Volume: `rel_volume_20d`, `obv_trend`, `volume_confirms_move`
- Relative strength: `rs_vs_spy_3m/6m_pct`, `rs_vs_sector_3m_pct`, `rs_line_slope`
- Levels: `nearest_support/resistance`, `dist_to_support/resistance_pct`, `fib_levels` (from last major swing), `anchored_vwap_from_low`
- Data quality: `bars_available`, `data_complete` (False when history is thin/gappy — caller must downgrade confidence)

Swing pivots (fixed-lookback local extrema) are the one piece of shared infrastructure feeding `market_structure`, `rsi_divergence`, `fib_levels`, and `anchored_vwap_from_low` — computed once per snapshot, not duplicated per field.

## Component 2: Sector ETF map (`technical/sector_map.py`)

Static dict: yfinance GICS sector string → sector ETF ticker (Technology→XLK, Financial Services→XLF, Healthcare→XLV, Energy→XLE, Industrials→XLI, Consumer Cyclical→XLY, Consumer Defensive→XLP, Utilities→XLU, Real Estate→XLRE, Basic Materials→XLB, Communication Services→XLC). Unknown/missing sector → `rs_vs_sector_3m_pct` omitted (treated neutral by the prompt), not an error.

## Component 3: LLM technical analyst (`bot/ai_analyst.py`, extended)

Adds, following the file's existing pattern (frozen dataclass, schema string, parser, scoring function):
- `_TECHNICAL_SCHEMA` — system prompt from the source doc's Part A.3 (risk-first discipline, confluence/conflict rules, setup classification, regime overlay, decision rule).
- `_TECHNICAL_BOTH_BONUS` — appended when the candidate already carries a fundamental/congressional signal (Part A.3 second block).
- `TechnicalScore` frozen dataclass mirroring the JSON schema (`conviction`, `entry`, `setup_type`, `trend_alignment`, `momentum_state`, `volume_confirmation`, `relative_strength`, `entry_trigger`, `invalidation_price`, `target_price`, `reward_risk`, `key_levels`, `conflicts`, `rationale`, `risk_flags`).
- `parse_technical_response(text) -> TechnicalScore` — strict JSON validation like `parse_entry_response`, **plus** in-code sanity checks not delegated to the model: reject/downgrade to `entry="skip"` if `invalidation_price >= last_close`, `target_price <= last_close`, or recomputed `reward_risk` doesn't match the returned prices within tolerance. Never trust the model's own arithmetic.
- `score_technical(snapshot, regime_label, signal_type) -> TechnicalScore` — single call (`temperature=0`), same `_call_with_retry` as existing functions. No bull/bear debate stage for this gate (keeps it to one extra call per candidate).
- Technical conviction is **not** blended into `EntryScore.conviction` or `ma_delta` — it only drives gate pass/fail and the stop/size inputs below, kept separate for auditability.

## Component 4: Structure-stop sizing (`risk/position_sizing.py`, extended)

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
```

Used **instead of** `vol_target_size_pct` when the technical gate is on and returned a valid `invalidation_price`; otherwise the existing ATR path is unchanged.

## Component 5: Orchestrator wiring (`orchestration/main_loop.py`)

In both `_process_signal` and `_process_fundamental_candidate`:
1. The existing `hist = _t.history(period="1y")` fetch (used today for ATR + MA delta) is widened to `period="2y"` and reused for the new `TechnicalSnapshot` — no additional per-candidate price fetch.
2. SPY and sector-ETF closes for relative strength are fetched once per pipeline run via a small `lru_cache`d helper (same pattern as `get_sector_for_ticker`), so candidates sharing a sector share one ETF fetch.
3. After the existing `if score.entry != "buy": return False` check: if `self._cfg.sizing.enable_technical_gate` is `False`, behavior is unchanged. If `True`: build the snapshot, call `score_technical`; a `"skip"` rejects the candidate (`emit_event(..., EventType.SIGNAL_REJECTED, ...)`, same as other gate rejections); a `"buy"` with a valid `invalidation_price` switches sizing to `structure_stop_size_pct` and passes `initial_stop_pct` through to `open_position` (Component 6). `reward_risk < SizingConfig.min_reward_risk` (default 2.0) is treated as a skip even if the model said buy — enforced in code, not just the prompt.

## Component 6: Per-position stop width (`bot/db.py`, `bot/portfolio.py`)

Today every stop is `entry_price * (1 - RiskConfig.trailing_stop_pct/100)`, and `enforce_stop_losses` recomputes from that same **global** constant on every poll — no per-position width is stored. To let a structural stop keep its own width as it trails:
- New migration: nullable `positions.stop_pct REAL` column (next `schema_version`), backfilled to `RiskConfig.trailing_stop_pct` for existing rows.
- `Portfolio.open_position(..., initial_stop_pct: float | None = None)` — when provided, used for the initial resting stop and persisted to `stop_pct`; when `None` (default/today's behavior), falls back to the global constant exactly as now.
- `enforce_stop_losses()` reads each position's own `stop_pct` (falling back to the global default if `NULL`) instead of the single global constant, so a structurally-derived stop trails at its own width, not the generic 15%.

## Component 7: Config (`system/config.py`)

`SizingConfig` gains:
- `enable_technical_gate: bool = False`
- `min_reward_risk: float = 2.0`

No other new config — the "≥3 confirming factors" and conflict logic live in the LLM decision rule (Part A.3), not as separate tunables.

---

## What Is NOT Changed / Out of Scope

| Item | Status |
|---|---|
| `backtesting/simulation.py`, `backtesting/walk_forward.py` | Unchanged. Live/paper pipeline only — Phase 0 PIT data is still blocked, so a historical ablation of this LLM layer isn't runnable yet regardless. Future work. |
| Exit review (`review_exit`) | Unchanged. Feeding the snapshot into exits (source doc A.6.5) is a separate follow-up — avoids double-firing with the existing stop-trail/take-profit logic. |
| Part A.7 (decision logging, ablation harness) and all of Part B (DSR/PSR, purged CV, cost model, portfolio risk budget) | Separate, not part of this build. |
| `vol_target_size_pct`, ATR-based sizing | Unchanged; still the default path when the gate is off or returns no valid invalidation price. |
| New dependency (TA-Lib / pandas-ta) | None — hand-rolled per your answer. |

---

## New Files

| File | Purpose |
|---|---|
| `technical/__init__.py` | Package init |
| `technical/indicators.py` | `TechnicalSnapshot` + deterministic compute functions |
| `technical/sector_map.py` | Sector → sector-ETF static map |

## New Tests

| File | Covers |
|---|---|
| `tests/test_technical_indicators.py` | Each indicator against synthetic fixtures with known answers (constant uptrend/downtrend/flat series); pivot/divergence detection; `data_complete=False` on short/gappy history |
| Updates to `tests/test_ai_analyst.py` | `score_technical` mocked-response parsing; schema validation; in-code rejection of invalid invalidation/target/reward_risk |
| Updates to `tests/test_position_sizing.py` | `structure_stop_size_pct` (closer stop ⇒ larger size, capped at `max_position_pct`, safe fallback) |
| Updates to `tests/test_portfolio.py` | `open_position(initial_stop_pct=...)` persists and is honored by `enforce_stop_losses`; default path unchanged |
| Updates to orchestration tests | Gate off → identical to current behavior (regression); gate on + mocked skip → rejected; gate on + mocked buy → structure-stop sizing path used, not ATR |

---

## Cost Estimate

- No new yfinance load beyond widening one existing fetch from 1y→2y per candidate, plus one SPY/sector-ETF fetch per pipeline run (cached).
- One additional Claude call (`temperature=0`, cached system block) per candidate that already passed the existing entry score/debate — adds roughly the same per-call cost as today's single (non-debate) entry score call, only for candidates that reach this stage.
