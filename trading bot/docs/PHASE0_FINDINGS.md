# Phase 0 Findings

## Gate Status: TESTED — GATE FAILS (2026-07-23)

The Phase 0 gate has been evaluated against real point-in-time data for the first
time. **It does not clear.** Full report: `docs/PHASE0_BACKTEST_2026-07-23.md`.

t-stat = -1.75, IR = -0.78 (rule needs t>2 AND IR>0.5) — the bot's live fundamental
factor composite has not demonstrated a statistically positive edge over SPY in a
real, point-in-time-correct ~3.75-year sample (2021-09-01 to 2025-06-30, bounded by
SimFin's free-tier fundamentals history). The point estimate is negative, not merely
insignificant, and consistent across both halves of the sample (no stability-condition
sign flip). Per the decision rule below: **do not proceed to add complexity.**

Historical performance numbers quoted elsewhere in this repo's design docs (e.g.
`docs/FACTOR_BACKTEST_2026-06-28.md`'s residual-momentum Sharpe ~0.88) were computed
on non-PIT, survivor-biased data and are not confirmed by this result — treat them
as unvalidated, per the original warning below.

---

## Data Required Before Gate Could Open (historical — now acquired, see above)

See `docs/PIT_DATA_REQUIREMENTS.md` for full schemas and vendor sources.

| Dataset | Status | Source used |
|---------|--------|-----------------|
| PIT constituent snapshots | ACQUIRED | github.com/fja05680/sp500 (free) — `backtesting/pit_constituents.py` |
| PIT fundamental snapshots | ACQUIRED (partial — SimFin free tier's 5yr window, ~80% ticker coverage) | SimFin free tier — `screener/simfin_fundamentals.py` |
| Delisted price history | ACQUIRED (partial — 97.2% ticker coverage, 2.8% gap disclosed) | yfinance first, Tiingo fallback — `market_data/pit_prices.py` |
| Ken French factor returns | ACQUIRED | Free — `screener/ff_factors.py` |

---

## Gate Decision Rules (from TRADING_BOT_REVIEW_PLAN.md)

Once real data is supplied, run:

```bash
cd "trading bot"
python -m backtesting.run_strategy_backtest   # or analyze_congressional_edge.py
```

Then apply these rules:

- **If factor-adjusted alpha is not statistically positive net of realistic costs → STOP.**
  Do not add more complexity. Recommend simplification instead. Write findings here.
- **If alpha is positive and robust (t-stat > 2, IR > 0.5, stable across periods) → proceed to Phase 1.**
  Phase 1 is already implemented and can be enabled immediately.

---

## Current Interpretation

Phase 1, 2, and 3 of the plan are fully implemented and the bot has been live paper
trading since 2026-07-06. The Phase 0 gate has now been run against real PIT data
(see status above) and **fails** — no statistically positive edge over SPY was found
in the real sample tested. This does not necessarily mean every individual factor
sleeve is worthless (see `docs/PHASE0_BACKTEST_2026-07-23.md`'s full caveats — the
sample is one ~3.75-year window including a rate-hiking bear market, and the
strategy's low realized market beta plausibly explains a meaningful share of the
raw underperformance), but it does mean the bot's previously-quoted backtest
numbers are not independently confirmed by this real test.

No code change follows from this finding — `screener/factor_scorer.py` is
untouched. A longer real PIT history (beyond SimFin's free-tier 5-year cap) would
be a more decisive re-test either way, if it ever becomes available.
