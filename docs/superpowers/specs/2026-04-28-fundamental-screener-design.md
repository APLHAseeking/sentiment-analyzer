# Fundamental Screener — Design Spec

**Date:** 2026-04-28
**Status:** Approved

## Goal

Extend the trading bot from a pure congressional-signal bot into a general-purpose trading bot where congressional signals are one conviction input among several. The bot should be able to independently discover and trade fundamentally attractive stocks, with congressional signals boosting conviction when both align.

## Design Principle

Reuse and extend existing code wherever possible. `gather_research()`, `score_entry()`, the existing universe, scheduler pipeline, risk manager, and DB layer are all retained. New code is additive.

---

## Architecture

Two parallel pipelines run each morning, feeding into the same entry logic, risk checks, and broker:

```
Phase 1 (existing):  scrape → filter_disclosures → score_entry(signal_type="congressional") → open
Phase 2 (new):       factor_screen → top 10-15 → score_entry(signal_type="fundamental") → open
Overlap:             ticker in both → score_entry(signal_type="both") → open with combined context
```

---

## Component 1: Factor Screener (`screener/factor_scorer.py`)

New module. **Does not replace `researcher.py`** — it is a separate lightweight daily ranking pass. `gather_research()` is still used to fetch the full `ResearchReport` for the shortlisted candidates before the AI call.

### Lightweight fetch

Two-step fetch, kept separate from `gather_research()`:

1. **Fundamentals** (`yf.Ticker(ticker).info` per ticker, concurrent via `ThreadPoolExecutor`) — P/E, P/B, FCF, ROE, margin, D/E
2. **Momentum** (`yf.download(all_tickers, period="3mo")` in a single batch call) — 1-month and 3-month price returns computed from the resulting price history

Failures on individual tickers are silently skipped. The batch price download runs once for the whole universe.

### Factors (equal weight, 33 points each)

| Factor | Metrics | Direction |
|---|---|---|
| Value | trailing P/E, P/B ratio, FCF yield (FCF / market cap) | Lower P/E + P/B, higher FCF yield = better |
| Momentum | 1-month return, 3-month return | Higher = better |
| Quality | ROE, profit margin, D/E ratio | Higher ROE + margin, lower D/E = better |

### Scoring method

Each metric is **percentile-ranked within the universe** for that day (0–100). Individual metric percentiles are averaged within each factor. Factor scores are summed for a composite 0–100. This avoids absolute thresholds and sector bias.

Stocks with fewer than 4 of 6 metrics available are excluded from that day's ranking.

### Output

Top 10–15 tickers by composite score. `gather_research()` is then called on each to produce a full `ResearchReport` for the AI prompt.

---

## Component 2: Extended `score_entry` (`bot/ai_analyst.py`)

**Extends the existing function** — does not replace it. Default behaviour (`signal_type="congressional"`) is unchanged; all existing callers and tests continue to work without modification.

### Signature change

```python
def score_entry(
    disclosure: dict | None,          # None for fundamental-only signals
    committees: list[str],            # [] for fundamental-only
    sector: str,
    lag_days: int,                    # 0 for fundamental-only
    estimated_cost_pct: float,
    research: ResearchReport | None = None,
    cluster_count: int = 1,
    signal_type: Literal["congressional", "fundamental", "both"] = "congressional",
    factor_score: int | None = None,  # composite 0-100, for fundamental + both
) -> EntryScore:
```

### System prompt adaptation

The cached system prompt gains two conditional blocks:

- **Congressional block** — lag decay rules, cluster boost, committee overlap guidance. Included when `signal_type` is `"congressional"` or `"both"`.
- **Fundamental block** — factor score interpretation, value/momentum/quality conviction guidance. Included when `signal_type` is `"fundamental"` or `"both"`.

When `signal_type="both"`: both blocks are included, plus a **+1 conviction bonus rule** — two independent signals agreeing is meaningful.

### User prompt adaptation

- Congressional fields (politician, committees, lag, cluster) omitted when `signal_type="fundamental"`
- `factor_score` line added when `signal_type` is `"fundamental"` or `"both"`: `"Composite factor score: 78/100 (value: 28, momentum: 25, quality: 25)"`

---

## Component 3: Scheduler changes (`bot/scheduler.py`)

**Extends `run_morning_pipeline`** — Phase 1 is untouched except it now also collects `congress_skipped: set[str]` — tickers that had a qualified disclosure but whose `score.entry != "buy"`. Phase 2 runs after Phase 1 completes.

```python
# Phase 1 change: collect skipped tickers alongside existing logic
congress_skipped: set[str] = set()
for disc in qualified:
    ...
    if score.entry != "buy":
        congress_skipped.add(disc["ticker"])   # <-- new
        continue
    ...

# Phase 2 — new
screener_candidates = run_factor_screen(top_n=12)   # returns [(ticker, factor_score, research), ...]
already_opened = {pos["ticker"] for pos in get_open_positions()}

for ticker, factor_score, research in screener_candidates:
    if ticker in already_opened:
        continue   # Phase 1 already opened it — don't double-enter
    signal_type = "both" if ticker in congress_skipped else "fundamental"
    sector = get_sector_for_ticker(ticker)   # existing function from signal_engine.py
    score = score_entry(
        disclosure=None, committees=[], sector=sector, lag_days=0,
        estimated_cost_pct=_ESTIMATED_COST_PCT, research=research,
        signal_type=signal_type, factor_score=factor_score,
    )
    # same sector cap, liquidity gate, risk manager checks as Phase 1
```

The daily position limit (`can_open_new_position()`) applies across both phases combined.

---

## Component 4: DB change (`bot/db.py`)

**One new column on the existing `signals` table:**

```sql
ALTER TABLE signals ADD COLUMN signal_source TEXT DEFAULT 'congressional';
```

Values: `"congressional"`, `"fundamental"`, `"both"`.

`insert_signal()` gains an optional `signal_source: str = "congressional"` parameter. The weekly performance report (`bot/analytics.py`) surfaces alpha by source so the two strategies can be compared.

---

## What Is NOT Changed

| Component | Status |
|---|---|
| `bot/researcher.py` — `gather_research()` | Unchanged. Still used for shortlisted candidates. |
| `bot/universe.py` — S&P 500 + Russell 1000 | Unchanged. Screener reuses the same universe. |
| `bot/portfolio.py` — risk checks | Unchanged. Same stop-loss, take-profit, sector cap, liquidity gate apply to all positions. |
| `review_exit()` | Unchanged. Exit logic is signal-source agnostic. |
| All existing tests | Unchanged. `score_entry` default remains `"congressional"`. |

---

## New Files

| File | Purpose |
|---|---|
| `screener/__init__.py` | Package init |
| `screener/factor_scorer.py` | Lightweight fetch + percentile ranking + top-N output |

---

## New Tests

| File | Covers |
|---|---|
| `tests/test_factor_scorer.py` | Percentile ranking, missing-data exclusion, top-N output, empty universe edge case |
| Updates to `tests/test_ai_analyst.py` | `score_entry` with `signal_type="fundamental"` and `"both"`, correct prompt fields |
| Updates to `tests/test_scheduler.py` | Phase 2 runs, deduplication, `signal_source` set correctly |

---

## Cost Estimate

- Lightweight screener fetch: ~1,000 yfinance calls, no API cost
- `gather_research()` on top 12: 12 yfinance calls, no API cost
- Claude calls: 12 × ~800 input tokens + ~200 output = ~$0.10–0.15/day on Sonnet 4.6 with caching
