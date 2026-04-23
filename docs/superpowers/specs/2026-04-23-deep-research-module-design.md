# Deep Research Module — Design Spec

**Date:** 2026-04-23
**Status:** Approved
**Scope:** Add a `bot/researcher.py` module that gathers fundamental, valuation, momentum, and news data for each ticker via FinceptTerminal's analytics scripts, and injects that research context into every Claude AI call.

---

## Problem

The current `score_entry()` and `review_exit()` calls give Claude only congressional signal metadata: who bought, which ticker, disclosure lag, and committees. Claude has no knowledge of the company's financial health, valuation, price momentum, or recent news. This means it cannot form an independent view — it can only assess whether the congressional signal is credible, not whether the underlying stock is actually worth buying.

---

## Solution

A new `bot/researcher.py` module integrates with FinceptTerminal's existing Python analytics scripts (via `sys.path` injection) to produce a `ResearchReport` for each ticker. This report is passed into both `score_entry()` and `review_exit()`, giving Claude analyst-grade context before it decides.

---

## Architecture

### Files changed

| File | Change |
|------|--------|
| `bot/researcher.py` | **NEW** — deep research module |
| `tests/test_researcher.py` | **NEW** — unit tests |
| `bot/config.py` | Add `FINCEPT_SCRIPTS_PATH` env var |
| `bot/ai_analyst.py` | `score_entry()` and `review_exit()` accept `ResearchReport` |
| `bot/scheduler.py` | Call `gather_research()` before each AI scoring call |
| `trading bot/.env.example` | Add `FINCEPT_SCRIPTS_PATH` entry |

### Data flow — morning pipeline

```
scrape → filter_disclosures → gather_research(ticker)
       → score_entry(disclosure + research) → open position
```

### Data flow — exit review

```
open positions → gather_research(ticker)
              → review_exit(position + research) → hold/reduce/exit
```

---

## `bot/researcher.py`

### sys.path setup

At module import time, `FINCEPT_SCRIPTS_PATH` (from `bot/config.py`) is prepended to `sys.path`. This makes the FinceptTerminal analytics packages importable:

```python
import sys, os
_FINCEPT_PATH = os.environ.get(
    "FINCEPT_SCRIPTS_PATH",
    "/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics"
)
if _FINCEPT_PATH not in sys.path:
    sys.path.insert(0, _FINCEPT_PATH)
```

The FinceptTerminal import (`YahooFinanceProvider`) is attempted at module load; if it fails, a clear `ImportError` is raised with instructions to set `FINCEPT_SCRIPTS_PATH`.

### `ResearchReport` dataclass

```python
@dataclass(frozen=True)
class ResearchReport:
    ticker: str
    company_name: str
    sector: str
    market_cap: float
    # Valuation multiples
    pe_trailing: float | None
    pe_forward: float | None
    pb_ratio: float | None
    ps_ratio: float | None
    peg_ratio: float | None
    ev_ebitda: float | None
    # Financial health
    roe: float | None
    roa: float | None
    profit_margin: float | None
    debt_to_equity: float | None
    current_ratio: float | None
    free_cash_flow: float | None
    revenue_growth: float | None
    earnings_growth: float | None
    # Market context
    beta: float | None
    week52_high: float | None
    week52_low: float | None
    momentum_1m: float | None     # % price change, 1 month
    momentum_3m: float | None     # % price change, 3 months
    # Analyst consensus (from yfinance)
    analyst_target: float | None
    analyst_rating: str | None    # "Buy" / "Hold" / "Sell"
    # News
    headlines: tuple[str, ...]    # up to 8 recent headlines
```

### `gather_research(ticker: str) -> ResearchReport | None`

1. Call `YahooFinanceProvider().get_company_data(ticker)` — populates all financial and market data fields.
2. Fetch 3-month price history via `yf.Ticker(ticker).history(period="3mo")` — compute `momentum_1m` and `momentum_3m` from close prices.
3. Fetch analyst consensus via `yf.Ticker(ticker).info` fields `targetMeanPrice` and `recommendationKey`. Normalize `recommendationKey` → `analyst_rating`: `"strong_buy"/"buy"` → `"Buy"`, `"hold"` → `"Hold"`, `"sell"/"strong_sell"` → `"Sell"`, anything else → `None`.
4. Compute `ev_ebitda` directly from `yf.Ticker(ticker).info.get('enterpriseToEbitda')` — this field is available from yfinance without additional calculation.
5. Fetch headlines via `yf.Ticker(ticker).news[:8]`, extracting `content.title`.
5. Assemble and return `ResearchReport`.

On any exception, log a warning and return `None`. The pipeline continues without research — Claude scores on congressional signal alone.

### `format_research_for_prompt(report: ResearchReport) -> str`

Serializes a `ResearchReport` to a compact, structured text block suitable for appending to a Claude prompt:

```
--- INDEPENDENT RESEARCH ---
Company: {name} | Sector: {sector} | Market cap: ${market_cap}B
Valuation: P/E {pe_trailing}x (fwd {pe_forward}x) | P/B {pb_ratio}x | EV/EBITDA {ev_ebitda}x | PEG {peg_ratio}
Financial health: ROE {roe}% | Margin {profit_margin}% | D/E {debt_to_equity} | FCF ${free_cash_flow}B
Momentum: {momentum_1m:+.1f}% (1m) | {momentum_3m:+.1f}% (3m) | 52w range ${week52_low}–${week52_high} | Beta {beta}
Growth: Revenue {revenue_growth:+.1f}% YoY | Earnings {earnings_growth:+.1f}% YoY
Analyst consensus: {analyst_rating} | Target ${analyst_target}
Recent headlines:
{formatted headlines}
---
```

`None` fields are rendered as `n/a`. If the entire report is `None`, this function is not called and the prompt is unchanged.

---

## `bot/config.py`

Add one new config value:

```python
FINCEPT_SCRIPTS_PATH = os.getenv(
    "FINCEPT_SCRIPTS_PATH",
    "/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics"
)
```

No required-key validation — FinceptTerminal path has a usable default. If the path is wrong, the import error at module load will surface it immediately.

---

## `bot/ai_analyst.py`

### `score_entry()` signature change

```python
# Before
def score_entry(disclosure: dict, committees: list[str], sector: str,
                lag_days: int, estimated_cost_pct: float) -> EntryScore:

# After
def score_entry(disclosure: dict, committees: list[str], sector: str,
                lag_days: int, estimated_cost_pct: float,
                research: ResearchReport | None = None) -> EntryScore:
```

When `research` is not `None`, `format_research_for_prompt(research)` is appended to the user message before the final "Score this signal." line.

The `_ENTRY_SYSTEM` prompt gains one additional instruction:

> "If independent research is provided, weigh it heavily. A high-conviction congressional signal in a fundamentally weak or overvalued company should have conviction penalised by 1–2 points. A strong signal in a financially healthy, undervalued company may have conviction raised by 1–2 points."

### `review_exit()` signature change

```python
# Before
def review_exit(ticker: str, entry_price: float, current_price: float,
                days_held: int, recent_headlines: list[str]) -> ExitDecision:

# After
def review_exit(ticker: str, entry_price: float, current_price: float,
                days_held: int, research: ResearchReport | None = None) -> ExitDecision:
```

`recent_headlines` is removed. When `research` is provided, the formatted research block replaces it in the prompt, giving Claude fundamentals + momentum + up to 8 headlines instead of just 5 raw headlines.

---

## `bot/scheduler.py`

### `run_morning_pipeline()`

After `filter_disclosures()`, import `gather_research` from `bot.researcher` and call it once per qualified ticker before `score_entry()`:

```python
from bot.researcher import gather_research

# inside the loop:
research = gather_research(disc["ticker"])  # None on failure — degraded gracefully
score = score_entry(disc, committees=committees, sector=sector,
                    lag_days=lag, estimated_cost_pct=_ESTIMATED_COST_PCT,
                    research=research)
```

### `run_exit_review()`

Similarly, call `gather_research(pos["ticker"])` and pass the result to `review_exit()`:

```python
research = gather_research(pos["ticker"])
decision = review_exit(
    pos["ticker"], pos["entry_price"], current_price, days_held,
    research=research
)
```

The old `headlines` extraction line (`yf.Ticker(...).news[:5]`) is removed — headlines are now sourced inside `researcher.py`.

---

## `trading bot/.env.example`

Add:

```
FINCEPT_SCRIPTS_PATH=/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics
```

---

## Error handling

| Failure scenario | Behaviour |
|-----------------|-----------|
| FinceptTerminal path wrong at startup | `ImportError` raised at `import bot.researcher` with message pointing to `FINCEPT_SCRIPTS_PATH` |
| `YahooFinanceProvider` call fails (network, bad ticker) | Log `WARNING`, return `None`; pipeline continues without research |
| Price history fetch fails | Momentum fields are `None`; rest of report still returned |
| All fields are `None` | `gather_research` returns `None`; Claude receives no research block |

---

## Testing (`tests/test_researcher.py`)

- `test_gather_research_returns_report` — mock `YahooFinanceProvider` and `yf.Ticker`, assert `ResearchReport` fields populated correctly
- `test_gather_research_returns_none_on_error` — mock provider to raise, assert `None` returned and no exception propagated
- `test_format_research_for_prompt_complete` — assert formatted string contains all key fields
- `test_format_research_for_prompt_none_fields` — assert `None` fields render as `n/a`, not crash
- `test_score_entry_with_research` — mock Claude client, assert research block appears in the user message sent to the API
- `test_review_exit_with_research` — mock Claude client, assert headlines from research appear in prompt

---

## Out of scope for this spec

- Macro/sector overlay from `economics/` scripts (Option C — deferred)
- Caching research results across pipeline runs
- Research for manually-specified tickers outside the congressional pipeline
- Web search for real-time news (yfinance headlines are sufficient for v1)
