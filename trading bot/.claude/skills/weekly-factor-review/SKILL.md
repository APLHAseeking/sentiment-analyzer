---
name: weekly-factor-review
description: Use when reviewing whether this trading bot's factor sleeve weights (value/momentum/quality/low-vol/reversal) and regime classification are still tracking real markets, on a weekly or ad-hoc cadence, or when asked to check the bot's factor weights, momentum overweight, regime stability, or run a factor health check.
---

# Weekly Factor Review

## Overview

On-demand research procedure for `trading bot/`'s factor screener (`screener/factor_scorer.py`). Produces a dated findings report; **never edits weights or config**. The user reads the report and decides whether any change is warranted as a separate follow-up.

## When to Use

- User asks to review factor weights, check for momentum/value/quality overweight, or "run the weekly review."
- Roughly weekly, or whenever the live regime has been unstable (see step 1).

Not for: actually changing `_REGIME_WEIGHTS`/`_MOMENTUM_WEIGHTS` — that's a separate, explicit task the user requests after reading a report from this skill.

## Procedure

1. **Weight snapshot + regime stability.** Read `_REGIME_WEIGHTS` and `_MOMENTUM_WEIGHTS` in `screener/factor_scorer.py` (search the file — line numbers drift). Query `trading.db.regime_log`, most recent 10-14 days:
   ```
   sqlite3 trading.db "SELECT date, regime_label, confidence, created_at FROM regime_log ORDER BY rowid DESC LIMIT 20"
   ```
   Flag any regime flip within a 2-3 day span, especially between opposite-extreme regimes (e.g. `deep-bear` <-> `melt-up`/`euphoria`) — that's a whipsaw, not a settled trend, and the sleeve weights swing hardest exactly there.

2. **Backtest refresh.** Run `python backtesting/backtest_price_factors.py` from `trading bot/` (no CLI args). Record per-sleeve Sharpe/alpha/drawdown. If a prior report exists in `docs/factor-reviews/`, diff against its numbers.

3. **Live signal check (scoped).** Query `fundamental_signals`, `positions`, `closed_positions` for the trailing week: trade count per regime, win rate, composite-score-level realized P&L.
   **Known limitation — state, don't fix:** `fundamental_signals.composite_score` is the only persisted score; there is no per-sleeve live attribution in the schema. Live sleeve-level performance can only be inferred via the backtest refresh (step 2), not measured directly from real trades. Note this in the report every time rather than silently omitting it.

4. **External cross-check.** WebSearch current factor-ETF performance (momentum/MTUM, value, quality, low-vol proxies) for the trailing week/month and any regime-relevant market news. Compare against the bot's live regime label and sleeve weights from step 1.

5. **Doc/code drift check.** Compare `_MOMENTUM_WEIGHTS`'/`_REGIME_WEIGHTS`' docstrings in `factor_scorer.py` against the actual constant values, and against `docs/FACTOR_BACKTEST_2026-06-28.md`'s stated rationale. These have drifted before (docstring described pre-SUE weights after SUE was added) — check every time.

6. **Write the report.** New file `docs/factor-reviews/<YYYY-MM-DD>.md`:
   - Current weights table (both `_REGIME_WEIGHTS` and `_MOMENTUM_WEIGHTS`)
   - Regime stability read (current regime, days in it, any whipsaw flagged)
   - Backtest refresh numbers + delta vs. last report
   - External market cross-check with sources
   - Doc/code drift findings
   - Numbered options at the end ("consider X because Y") — **not a recommendation to act unprompted, and no code edits in this step**

7. Stop. Any weight change is a separate task the user asks for explicitly after reading the report.

## Common Mistakes

- Editing `factor_scorer.py` weights as part of "the review" — don't. This skill only produces a report.
- Skipping the live-attribution limitation note because "it was said last time" — restate it each report; the schema hasn't changed.
- Treating a single day's regime confidence as settled — always look at the trailing window for flips.
