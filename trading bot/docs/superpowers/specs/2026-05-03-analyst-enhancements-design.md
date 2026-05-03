# Analyst Enhancements Design

**Date:** 2026-05-03
**Status:** Approved
**Scope:** Three targeted enhancements inspired by TradingAgents framework review

---

## Background

A review of the TauricResearch/TradingAgents framework identified two genuine additions not present in this bot's stack: adversarial bull/bear deliberation before committing to high-conviction trades, and social/news sentiment scoring. A risk threshold audit was also conducted; TradingAgents uses no quantitative circuit breakers (their "risk management" is a pure LLM debate), so threshold calibration is based on independent analysis of the strategy's characteristics.

---

## Feature 1: Bull/Bear Debate (`bot/ai_analyst.py`)

### Motivation

The current `score_entry()` makes a single Claude call that must simultaneously gather evidence, weigh risks, and commit to a conviction score. For high-conviction signals (≥7) this is where the most capital is deployed — and where a devil's advocate is most valuable.

### Design

**New public function:** `score_entry_with_debate()`

- Identical signature to `score_entry()`: same parameters, same `EntryScore` return type.
- All callers in `orchestration/main_loop.py` replace `score_entry` with `score_entry_with_debate`. No other changes at call sites.

**Internal flow:**

```
Call 1: score_entry() — existing logic unchanged
  └─ conviction < 7  →  return EntryScore immediately (no extra cost)
  └─ conviction ≥ 7:
       Call 2: _bull_argument()  →  free-form str (Haiku)
       Call 3: _bear_argument()  →  free-form str (Haiku), given bull text to counter
       Call 4: score_entry(..., debate_context=combined text)  →  EntryScore (Sonnet)
       return Call 4 result
```

**New prompt constants:**

- `_BULL_SYSTEM`: instructs Claude to build the strongest possible investment case using the provided research. Must cite specific metrics, not vague sentiment.
- `_BEAR_SYSTEM`: instructs Claude to challenge the bull's specific claims with evidence. Must counter, not restate.

**Call 4 prompt construction:** the existing `score_entry` prompt is extended with a `--- DEBATE ---` block appended after the research section, containing both arguments verbatim. The system prompt (scoring rules) is unchanged.

**Model assignment:**
- Calls 2 and 3: `claude-haiku-4-5-20251001` (free-form, fast, cheap)
- Call 4: `claude-sonnet-4-6` (structured scoring, same as today)

**Schema:** `EntryScore` is unchanged. Debate text is ephemeral — not stored.

**Cost profile:** low-conviction signals (the majority) cost 1 call as today. High-conviction signals cost 4 calls (~$0.01–0.02 per signal at current pricing).

---

## Feature 2: Sentiment Scoring (`bot/researcher.py`)

### Motivation

`gather_research()` currently fetches 8 news headlines (titles only). yfinance returns up to 28+ items with a `summary` field (1–3 sentence article excerpt). A Claude sentiment pass on this richer corpus can disambiguate directional news ("acquisition denied" vs "acquisition announced") that title-only scanning misses.

### Design

**`ResearchReport` — three new fields:**

```python
sentiment_label: str | None         # "bullish" | "neutral" | "bearish"
sentiment_strength: int | None      # 1 (weak) | 2 (moderate) | 3 (strong)
sentiment_themes: tuple[str, ...]   # 2-3 key themes extracted by Claude
```

**`gather_research()` changes:**

- News fetch cap: 8 → 28 items.
- Extract `title` + `content.summary` per item; fall back to title-only if summary absent.
- New `_score_sentiment(news_block: str) -> tuple` helper:
  - Model: `claude-haiku-4-5-20251001`
  - System prompt enforces JSON: `{"sentiment": "bullish"|"neutral"|"bearish", "strength": 1|2|3, "key_themes": ["...", "..."]}`
  - On any failure: returns `(None, None, ())` — sentiment is best-effort and must not cause `gather_research()` to return `None`.

**`format_research_for_prompt()` — one new line** (inserted before the headlines block):

```
Sentiment (28 headlines, AI-scored): bullish/2 — themes: earnings beat, guidance raised
```

Omitted entirely if `sentiment_label` is `None`.

**`headlines` field** remains in `ResearchReport` and the prompt — raw evidence for Claude to verify the sentiment score against.

**Downstream:** feeds directly into existing `_RESEARCH_ADJUSTMENTS` logic in `ai_analyst.py`. No changes needed there.

---

## Feature 3: Risk Threshold Changes

### Background

TradingAgents offers no quantitative guidance — their risk module is an LLM debate with no hard thresholds. The following changes are based on independent analysis of the strategy's characteristics (congressional signals, hold weeks–months, $100k simulated capital, regime engine already pre-filtering exposure in bear/crash regimes).

### Changes

**`system/config.py` — `RiskConfig`:**

| Field | Old | New | Rationale |
|---|---|---|---|
| `daily_loss_reduce_pct` | 2.0% | 3.0% | 2% false-triggers in normal bull-regime volatility with 8–10 open positions |
| `max_adv_pct` | 10.0% | 5.0% | More conservative liquidity gate; original 10% is loose for smaller universe names |
| `max_invested_pct` | *(new)* | 80.0% | Caps total deployed NAV; prevents over-concentration when many signals fire simultaneously |

**`settings.validate()` — new check:**

```python
if self.risk.max_invested_pct <= 0 or self.risk.max_invested_pct > 100:
    raise ValueError("max_invested_pct must be in (0, 100]")
```

**`orchestration/main_loop.py` — `run_morning_pipeline()` enforcement:**

After stop-loss / take-profit enforcement and risk manager update, but before Phase 1 and Phase 2 entry loops, compute total invested NAV % and gate new entries behind it. Use a conditional block (not `return`) so that exit enforcement always runs regardless of the cap:

```python
positions = self._broker.get_positions()
if positions:
    nav = self._broker.get_cash() + sum(p["qty"] * p["current_price"] for p in positions)
    invested = sum(p["qty"] * p["current_price"] for p in positions)
    invested_pct = invested / nav * 100 if nav > 0 else 0.0
else:
    invested_pct = 0.0

_at_capacity = invested_pct >= self._cfg.risk.max_invested_pct
if _at_capacity:
    log.info("Portfolio at %.1f%% invested (cap %.1f%%) — skipping new entries",
             invested_pct, self._cfg.risk.max_invested_pct)

if not _at_capacity:
    # Phase 1: congressional signals
    ...
    # Phase 2: fundamental screener
    ...
```

Stop-losses, take-profits, and exit review always run — they reduce exposure, never increase it.

**`RiskManager.status_dict()`** — add `"max_invested_pct": self._risk.max_invested_pct` for dashboard visibility.

---

## Files Changed

| File | Change |
|---|---|
| `bot/ai_analyst.py` | Add `_BULL_SYSTEM`, `_BEAR_SYSTEM`, `_bull_argument()`, `_bear_argument()`, `score_entry_with_debate()` |
| `bot/researcher.py` | Expand news fetch, add `_score_sentiment()`, extend `ResearchReport`, update `format_research_for_prompt()` |
| `system/config.py` | Adjust 2 thresholds, add `max_invested_pct` field and validation |
| `risk/risk_manager.py` | Add `max_invested_pct` to `status_dict()` |
| `orchestration/main_loop.py` | Add invested-pct cap check at top of `run_morning_pipeline()`; swap `score_entry` → `score_entry_with_debate` |

## Files NOT Changed

- `bot/db.py` — no new tables; debate text is ephemeral
- `bot/portfolio.py` — position management unchanged
- `execution/`, `backtesting/`, `regime/` — no changes
- `EntryScore` dataclass — unchanged; debate is transparent to callers

---

## Testing

Each feature has natural unit test targets:

- `score_entry_with_debate()`: mock the Claude client; assert that conviction < 7 makes exactly 1 call, conviction ≥ 7 makes exactly 4 calls, and the return type is always `EntryScore`.
- `_score_sentiment()`: mock the Claude client; assert graceful fallback on JSON parse error or API failure.
- `gather_research()` with sentiment: assert `sentiment_label` is populated when client works, `None` when it fails, without the overall function returning `None`.
- Risk config validation: assert `ValueError` on invalid `max_invested_pct`.
- Invested-pct gate: mock broker returning positions summing to 85% of NAV; assert Phase 1 and Phase 2 entry loops are skipped (no call to `_process_signal`) while stop-loss enforcement still runs.
