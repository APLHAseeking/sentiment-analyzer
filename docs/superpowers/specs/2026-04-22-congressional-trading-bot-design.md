# Congressional Trading Bot — Design Spec
_Date: 2026-04-22_

## Overview

A Python trading bot that exploits the informational edge of US Congress members trading stocks in industries their own committees oversee. Signals are sourced from Capitol Trades (free), filtered by committee jurisdiction, scored by Claude AI, and executed via Alpaca (paper trading now, IBKR later).

---

## Strategy

**Core thesis:** Congress members who sit on committees overseeing a specific industry have privileged access to non-public regulatory and legislative information about companies in that industry. Purchases in those stocks carry a statistically significant informational edge.

**Signal filters (all three must pass):**

1. **Trade type:** Purchases only (not sales, not options)
2. **Committee jurisdiction match:** The congressperson must sit on a committee whose oversight domain covers the stock's GICS sector (see committee map below)
3. **Universe:** Stock must be in the S&P 500 or Russell 1000

**Disclosure lag decay:**
- 0–14 days since transaction date: full signal strength
- 15–30 days: Claude penalizes conviction score by ~2 points
- 31–45 days: Claude penalizes by ~3 points, position capped at 2% of portfolio
- 45+ days: signal discarded entirely

---

## Architecture

```
bot/
├── scraper.py        # Scrapes Capitol Trades daily for new disclosures
├── committee.py      # Maps GICS sector → committee jurisdiction
├── signal_engine.py  # Applies all three signal filters
├── ai_analyst.py     # Claude scores conviction, sizes position, manages exits
├── portfolio.py      # Tracks positions, cash, P&L; triggers daily exit review
├── broker.py         # Alpaca API wrapper (paper now, IBKR later)
├── scheduler.py      # Runs the daily pipeline
└── db.py             # SQLite: disclosures, signals, positions, portfolio_log
```

---

## Data Layer

**Source:** Capitol Trades (capitoltrades.com) — scraped once daily at market open. Scraper stores `last_seen_id` to process only new filings.

**Stock universe:** S&P 500 + Russell 1000, maintained as a CSV refreshed weekly (iShares ETF holdings or Wikipedia).

**SQLite tables:**
- `disclosures` — raw Capitol Trades data, deduped by disclosure ID
- `signals` — qualified signals post-filter with Claude's conviction score and rationale
- `positions` — open positions (ticker, entry price, size %, entry date, rationale)
- `portfolio_log` — daily snapshot of cash, positions value, total NAV

---

## Committee Jurisdiction Map (initial)

| Committee | GICS Sectors |
|-----------|-------------|
| Senate Banking | Financials, REITs |
| House Financial Services | Financials, REITs |
| Senate Commerce | Consumer Discretionary, Telecom, Technology |
| House Energy & Commerce | Energy, Utilities, Healthcare |
| Senate Armed Services | Industrials (Aerospace & Defense) |
| House Armed Services | Industrials (Aerospace & Defense) |
| Senate Agriculture | Consumer Staples (Food & Beverages), Materials |
| House Agriculture | Consumer Staples (Food & Beverages), Materials |
| Senate Finance | All sectors (tax/trade jurisdiction) |
| Senate HELP | Healthcare, Pharmaceuticals |
| House Ways & Means | All sectors (tax jurisdiction) |

---

## AI Analyst (Claude Integration)

**Model:** Claude Sonnet 4.6 with prompt caching on system prompt.

**Entry analysis — input per signal:**
- Congressperson: name, party, state, committees held
- Stock: ticker, company, GICS sector
- Trade: date, disclosure date, lag days, transaction size bracket
- Market context: recent headlines (Yahoo Finance RSS), 30-day momentum, avg daily volume
- Cost estimate: round-trip trading cost for the position

**Entry analysis — Claude JSON output:**
```json
{
  "conviction": 7,
  "position_pct": 4.5,
  "rationale": "...",
  "entry": "buy",
  "risk_flags": ["small transaction size", "delayed disclosure (38 days)"]
}
```

**Conviction → position size:**
- 1–4: Skip (expected return does not clear transaction costs)
- 5–6: 1–2% of portfolio
- 7–8: 3–5% of portfolio
- 9–10: 6–8% of portfolio (hard cap)

**Cost hurdle:** Claude is given the estimated round-trip cost and must only recommend entry if expected return exceeds cost by at least 2×.

**Daily exit review — Claude returns:** `hold`, `exit`, or `reduce` for each open position, based on current P&L, days held, recent news, and market conditions.

---

## Portfolio & Risk Rules

- Max position size: 8% of portfolio
- Max open positions: 20
- Max new positions per day: 3
- Hard stop-loss: -15% from entry (automatic, overrides Claude)
- All orders: market orders at next market open

**Exit execution:**
- `exit` → full market sell at next open
- `reduce` → sell 50% of position at next open

---

## Broker Layer

`broker.py` provides a clean interface: `place_order(ticker, side, pct_of_portfolio)`, `get_positions()`, `get_cash()`. Alpaca SDK used now. Swapping to IBKR (`ib_insync`) requires only changing `broker.py` internals.

**Alpaca setup:** Paper trading account at alpaca.markets. API key + secret stored in `.env` (never committed).

---

## Daily Schedule

| Time (ET) | Action |
|-----------|--------|
| 08:00 | Scrape Capitol Trades for new disclosures |
| 08:15 | Run signal filter + committee check |
| 08:30 | Send qualifying signals to Claude for entry scoring |
| 08:45 | Place new position orders (market open at 09:30) |
| 09:00 | Send open positions to Claude for exit review |
| 09:15 | Place exit/reduce orders if instructed |
| 16:30 | Log daily portfolio snapshot to `portfolio_log` |

---

## Configuration (.env)

```
ANTHROPIC_API_KEY=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

---

## Out of Scope (v1)

- Options trading
- Short selling
- Intraday trading
- Multi-broker simultaneous execution
- Web dashboard (can be added later using the existing Flask app pattern)
