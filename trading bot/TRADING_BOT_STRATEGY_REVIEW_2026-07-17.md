# Trading Bot Strategy, Code & Profitability Review — 2026-07-17

CONSTRAINT CHECK: trading bot/TRADING_BOT_STRATEGY_REVIEW_2026-07-17.md — matches
"report findings for sign-off first, no fixes applied during the review itself"
(docs/STATE.md `## Constraints`) — report-only, no code/config edits made.

**Scope:** trading logic, code quality, strategy/profitability, and process — explicitly
excluding reliability/uptime (scheduler wedges, watchdog, sleep issues), which is a separate
concurrent session's active thread. No fixes applied in this pass; this is a findings report
for sign-off. Full suite was not re-run as part of this review (read-only + one new file;
no source touched) — reliability session's last recorded run was 975 passed, 0 known failures.

Findings are tagged **CONFIRMED** (traced the real code path or ran real data/commands) or
**PLAUSIBLE** (reasoned but not fully traced/run). Ranked by estimated profitability/risk
impact, not discovery order.

---

## Top findings, ranked

### 1. CONFIRMED — The live entry hurdle is 4.5% expected return, not the documented "3x cost / 1.0% absolute," and the cost input is ~15x the real modeled cost

`bot/ai_analyst.py:30`'s LLM prompt schema states the rule as "buy only if `expected_return_pct`
≥ 3× `estimated_cost_pct` AND ≥ 1.0% absolute." The live orchestrator
(`orchestration/main_loop.py:105`) sets `_ESTIMATED_COST_PCT = 1.5`. Since `3 × 1.5% = 4.5%`
dominates the 1.0% floor by 3.5x, **the floor never actually binds** — the real rule the LLM
is told to enforce is "expected return ≥ 4.5%," not the ~1.0-1.5% the 2026-07-10 loosening
intended to reach. Git history (`0d93f8b`) confirms that change only edited prompt text
(5x/1.5%→3x/1.0%) and never touched this constant.

The real modeled round-trip cost is far lower: `system/config.py` `ExecutionConfig.slippage_bps
= 5.0`, `commission_per_share = 0.0` (Alpaca genuinely charges no per-share commission on US
equities — this default is correct, not a bug) → round-trip cost ≈ 0.10%. `_ESTIMATED_COST_PCT
= 1.5` overstates real cost roughly 15x.

**Live evidence this is actually binding:** `trading.db.fundamental_signals` has only 19 rows
logged 2026-07-07→07-16 (9 trading days); all 17 rows with non-zero `expected_return_pct`
cluster tightly at 4.9-7.8% — consistent with the LLM anchoring just above the real 4.5% wall
rather than reporting a genuinely dispersed estimate.

**Recommendation:** lower `_ESTIMATED_COST_PCT` to ~0.3-0.5% (a reasonable buffer over the
~0.10% modeled cost for spread/impact beyond the explicit slippage model). This drops the
effective floor to 0.9-1.5%, letting the intended 1.0% absolute floor actually govern —
closing the gap between what was decided on 2026-07-10 and what the code has done since.
Also worth cleaning up: `bot/scheduler.py:29` has a stale, now-orphaned copy of this constant
(dead file per Track A/B, see #6) with a comment claiming it's "the same knob as
`bot/scheduler.py`'s exit-review constant" — no longer true either way.

### 2. CONFIRMED (sample-limited) — First real backtest read on the congressional signal: excess returns are negative, not positive, in the only cached data available

`docs/CONGRESSIONAL_EDGE.md` has said "cannot finalize without real data" since inception.
`backtesting/analyze_congressional_edge.py`/`analyze_hedge_drag.py` cannot run against real
data as shipped — both `main()` entry points only build **synthetic** fixtures, explicitly
printing "NOTE: All numbers above are from SYNTHETIC data." Real data does exist on disk
though (`capitol_trades_merged.json`: 5,406 real disclosed trades, 2025-10-10→2026-05-15,
462/954 tickers matched against the cached `pit_cache/prices.parquet`), so this review computed
forward excess returns directly (entry = disclosure date, benchmark = equal-weight return of
the cached universe):

```
[1m] buy   n=1933  mean_excess=-0.636%  t=-2.57
[1m] sell  n=1905  mean_excess=-0.491%  t=-1.96
[3m] buy   n=1586  mean_excess=-2.538%  t=-4.93
[3m] sell  n=1671  mean_excess=-0.140%  t=-0.27
```

Congressional **buys** show a significantly *negative* excess return at both 1- and 3-month
horizons in this sample — the opposite of the edge the signal is designed to capture.
**Caveats, taken seriously:** t-stats are likely overstated (overlapping windows, many trades
clustered same-day/ticker violate the independence assumption); single ~7-month window, one
market regime; this is not a substitute for a proper PIT backtest once Phase 0 data lands.
Treat the *sign* (no positive edge found in real data) as the actionable signal, not the exact
magnitude.

**Recommendation:** do NOT loosen congressional sizing/caps — this finding supports keeping
`_CONGRESSIONAL_MAX_PCT=3.0%`/1-per-day exactly where they are, and is a reason to be more
skeptical of the signal generally, not less. Consider down-weighting the cluster/amount
conviction boosts in `bot/ai_analyst.py`'s congressional scoring rules until more (and better,
PIT-correct) data accumulates. This is the first real empirical evidence on this signal since
launch — worth treating as a genuine (if provisional) input to position sizing, not filed away
as "still unknown."

### 3. CONFIRMED — An unguarded scraper call can silently zero out the entire day's PRIMARY signal source

`orchestration/main_loop.py:521`: `new_disclosures = run_scraper()` in `run_morning_pipeline`
has no `try/except` around it, and sits *before* the block containing Phase 1 (fundamental
screener — the primary signal source), Phase 2 (congressional), Phase 2.5 (insider), and
Phase 3 (hedge). Phase 1 and Phase 2.5 each wrap their own bodies in `try/except Exception`,
but the scraper call that precedes all of them does not. Any exception in `run_scraper()`
(the Capitol Trades scraper has broken this way twice before in this project's history — a
403-burst bug and a 429-vs-404 bug, both since fixed, but the *pattern* of it throwing is
real) propagates out of `run_morning_pipeline` entirely: Phase 1/2/2.5/3 never execute and
`record_job_run` never fires for that day — i.e. **zero fundamental candidates scored for a
failure in a signal source explicitly documented as merely "supplementary."** This is exactly
the failure shape ("bot not trading") this project has repeatedly root-caused this month, just
via a different mechanism than the ones already fixed.

**Recommendation:** wrap the scraper+filter call in `run_morning_pipeline` in its own
try/except, matching the pattern Phase 2.5 already uses — a congressional-feed outage should
degrade to "zero congressional signals today," not "zero signals of any kind today."

### 4. PLAUSIBLE — Regime classification can silently use a stale (prior-day) feature row with no staleness flag

`regime/hmm_engine.py:460-463` (`update_single`): `last_row_df =
feat_df[available].dropna().iloc[-1:]` drops any row with a NaN in the selected feature
columns, then takes whatever row is now last. The only guard is raising if the *entire* tail
is NaN. If only today's bar has a NaN in one feature column (e.g. a rolling-window feature
straddling a data gap) while an earlier cached day does not, the function silently returns an
older feature row, runs it through the HMM, and stamps the result with **today's** date — with
no warning and no distinction from a genuinely fresh classification. Since `_regime_state`
drives `AllocationEngine.compute()` for every position sized that day (confirmed live-wired,
see Track A below), a silently stale regime read would mis-scale every entry's size that
morning without anyone knowing it happened.

**Recommendation:** assert the returned row's date actually matches the intended "as of" date
before using it (raise or flag `is_stable=False` instead of silently falling back). Real-world
trigger frequency not verified in this pass — recommend a targeted unit test with an
intentionally-gapped feature column to confirm the failure mode before deciding priority.

### 5. CONFIRMED — Refactor opportunity: ~70-80% of the four signal-processing methods in `main_loop.py` is byte-for-byte duplicated, which is exactly the shape that produced finding #3

`_process_signal`, `_process_insider_signal`, `_process_fundamental_candidate`, and the hedge
entry path all apply the same 10-gate pipeline (event gate → AI score → price fetch →
`_size_position` → regime scaling → source-specific cap → correlation filter → portfolio-vol
gate → risk veto → `open_position`) in the same order — that consistency is real and good; no
case was found where one path silently skips a gate the others enforce. But most of each
method's body (price fetch, correlation filter, vol gate, invested-pct computation, risk veto,
logging) is identical across the three, differing only in the disclosure-shaped input, the cap
constant, and the final persistence call. This is not a style complaint — three copies of the
same pipeline is exactly the shape where a fix or new gate lands in one copy and is silently
forgotten in the other two (this repo's history already contains three separate instances of
that exact bug class: `open_position`/`close_position`/`reduce_position` bool-return-ignored,
found and fixed on three different dates). `main_loop.py`'s 1,642-line size is a symptom of
this duplication, not an independent problem.

**Recommendation:** extract a shared `_finalize_and_open(ticker, sector, base_pct, signal_type,
cap_pct, research, persist_fn)` helper once a fix or new gate needs to touch this logic again —
flagging now as a real opportunity, not proposing a refactor-for-its-own-sake in this pass.

### 6. CONFIRMED — Dead code: `bot/scheduler.py` (247 lines + its full test file) has zero non-test references anywhere

`run_bot.py` only imports `bot.db`, `bot.universe`, `monitoring.logger`, `system.config` — no
path to `bot/scheduler.py` in production. `grep -rln "bot\.scheduler\|from bot import
scheduler\|import scheduler"` (excluding `test_*.py`) returns zero hits repo-wide. The module
is explicitly self-labeled `DEPRECATED — superseded by orchestration/main_loop.py. ... Do not
run or import this file in production.` It still consumes `EntryScore.position_pct` in a way
that would confuse a reader into thinking the LLM's position-size output is used for live
sizing (it isn't — the live path ignores it by design, logged explicitly at
`main_loop.py:836`).

**Recommendation:** low priority (no profitability impact, pure maintenance/confusion cost) —
delete `bot/scheduler.py` and `tests/test_scheduler.py` (confirm via the standard dead-code
triple-grep before deleting: bare name, quoted-string dynamic dispatch, `__all__`/entry-point
registration — not done as part of this report-only pass).

### 7. NOTED (not done) — `performance/tracker.py`'s live-vs-backtest comparison tool is never actually invoked

`PerformanceTracker` (built specifically so live `trading.db` performance can be compared
against backtest expectations using the same `compute_all` metrics) is defined and exported
from `performance/__init__.py` but never instantiated anywhere outside its own test file — no
CLI script, cron job, or dashboard view calls it. The capability to answer "is live performance
tracking the backtested edge?" exists in code but nobody is actually checking it on any
cadence.

**Recommendation:** wire it into a periodic check (weekly, alongside the existing
`weekly-factor-review` skill) or the Streamlit dashboard — otherwise this remains built and
unused.

### 8. Minor findings (low severity, included for completeness)
- **`docs/STATE.md` (repo root) exists and is actively maintained** (confirmed during this
  review) — an earlier scoping pass found `trading bot/docs/STATE.md` does not exist despite
  ~10 cross-references from `CLAUDE.md`/`CLAUDE-REFERENCE.md`/`RUNBOOK.md` expecting a
  trading-bot-local one. Those cross-references should either point at the repo-root
  `docs/STATE.md` (if that's now the intended canonical doc) or a trading-bot-local one should
  be created — currently they're dangling.
- `system/config.py:195` — `target_portfolio_vol_pct`'s inline comment says "(informational)"
  but `main_loop.py:491` actively multiplies every trade's size by it (`_port_vol_mult =
  target_vol / realized_vol`). Comment is stale, code is fine; fix the comment so a future
  editor doesn't assume it's inert.
- `screener/factor_scorer.py:258,282,285` — `fcf and mcap and mcap > 0` truthy-checks treat an
  exact `fcf == 0.0` the same as missing data (drops a real zero-FCF observation to "neutral"
  instead of scoring it). Opposite direction from the usual yfinance-0.0-means-failure trap;
  low blast radius (a handful of edge-case tickers per screen).
- `bot/portfolio.py:67` — `cancel_order`'s return value isn't checked on the non-fill path; if
  cancellation itself fails (not just the fill), a stray resting order could become invisible
  to both the position-reconcile check and the DB. No capital at risk (never filled), just an
  orphaned order with no cleanup path.
- `docs/FACTOR_BACKTEST_2026-06-28.md`'s example `_MOMENTUM_WEIGHTS` table is stale (shows the
  pre-SUE 4-component weights: 0.45/0.30/0.15/0.10); current code has 5 components since SUE
  was folded in (0.40/0.25/0.15/0.12/0.08). Doc, not code — code is correct.
- `test_run_bot.py` is 29 lines for `run_bot.py`'s 184 — the one test-coverage gap worth
  caring about among those flagged, since `run_bot.py` contains the broker-mode decision and
  the paper-only guard's caller (a real-vs-simulated-broker mixup would be exactly the kind of
  bug thin coverage there could hide). The other flagged gaps (`analyze_*.py`, `benchmarks.py`,
  `dashboard/app.py`, `monitoring/logger.py`) are reporting/tooling paths that don't touch live
  order placement — lower priority.

---

## Verification / sanity checks that came back clean (worth recording so they aren't re-asked)

- **Track A — is regime-aware sizing actually live?** CONFIRMED yes.
  `AllocationEngine.compute()` (`regime/allocation_engine.py`) is called from all three
  signal-processing sites (`main_loop.py:860,991,1119`) and its `final_position_pct` genuinely
  flows into `Portfolio.open_position`'s `position_pct` argument. This was the single biggest
  suspected bug going into this review and it is NOT a bug — regime-based position scaling is
  real and live.
- No new instances of the "bool return value silently ignored" bug class (previously found
  3x in `open_position`/`close_position`/`reduce_position`) were found elsewhere in
  `main_loop.py` or `bot/portfolio.py` — all four call sites correctly check the return value,
  and `sqlite3.Row` results are correctly `dict()`-converted before any `.get()` call.
- `bot/ai_analyst.py`'s JSON parsing uses explicit `is not None`/`.get()` checks throughout,
  including a correct hard-skip on a degenerate `invalidation_price=0.0` — no truthiness bug.
- **PEAD/earnings-blackout conflict does not exist** — `utils/event_calendar.py:55-94`'s gate
  is pre-print only (`0 <= days_until <= window_days`); a past earnings date always yields a
  negative `days_until` and is never blocked. No carve-out is needed; the "unresolved decision"
  in `EDGE_BACKLOG.md` can be closed as a non-issue.
- **Fresh backtest reconfirms no drift**: `python3 backtesting/backtest_price_factors.py`
  today reproduces residual momentum's edge over the equal-weight baseline at ~3.33%/yr
  (5.307% − 1.980%), matching the documented "~3-4%/yr net of baseline, pre-cost" figure
  exactly. Low-Vol/BAB (0.257% alpha, below baseline) and Mean Reversion (-1.218% alpha) remain
  weak/negative standalone, consistent with `FACTOR_BACKTEST_2026-06-28.md`'s own framing that
  they're defensive overlays, not standalone return engines — current regime weighting
  (small/gated allocations to both) is the right call, not a bug.
- **Regime/momentum weights are internally consistent** with the rationale documented in
  `FACTOR_BACKTEST_2026-06-28.md` — reversal is 0.05 in trending regimes and 0.15 in bear/
  neutral as claimed; low-vol is 0.15-0.20 in bear/crash and 0.05-0.10 in rallies as claimed.
- **`commission_per_share=0.0` is correct, not a bug** — Alpaca genuinely charges no per-share
  commission on US equities; the real cost channel is the explicitly modeled
  `slippage_bps=5.0`. Realized turnover at this backtest's monthly-rebalance cadence keeps
  annualized cost drag well under 1%/yr even under conservative assumptions — reasoned, not
  simulated end-to-end; recommend running the actually-costed `run_strategy_backtest.py` +
  `simulation.py` path (which does apply slippage/commission) to replace this estimate with a
  real number if greater precision is wanted.

## Explicitly not re-litigated (already correctly settled, no new evidence changes them)

- SUE signal PIT-backtest gate failure (t<2, IR<0.5 both horizons, 60d sign-flip) — 0.15
  sub-weight stays, correctly conservative.
- Phase 0 data gate (BLOCKED ON DATA) — all backtest numbers in this report inherit that
  caveat; the congressional-edge read in finding #2 is a first real (if provisional) data
  point, not a Phase-0-clearing result.
- Russell 1000 universe blocker — data-source problem, not a strategy question.
- Reliability/uptime work (scheduler wedges, watchdog, sleep mitigation) — separate session's
  active thread, untouched by this review.

---

## Recommended next steps (for sign-off — nothing below has been applied)

Ordered by profitability/risk impact:
1. Lower `_ESTIMATED_COST_PCT` (finding #1) — closes the gap between the 2026-07-10 decision
   and what the code has actually done since; directly increases trade frequency at the
   *intended* quality bar, not a lowered one.
2. Wrap `run_scraper()` in `run_morning_pipeline` in a try/except (finding #3) — closes a real
   "entire day's primary signal goes dark" failure mode, same class as several already-fixed
   incidents this month.
3. Treat the congressional-edge negative-excess-return read (finding #2) as a reason to hold
   or reduce that signal's influence, not loosen it, pending a proper PIT backtest once Phase 0
   data lands.
4. Add a staleness assertion to `regime/hmm_engine.py`'s `update_single` (finding #4) — verify
   the trigger condition with a targeted test first.
5. Lower-priority cleanup: extract the shared signal-processing pipeline (#5), delete
   `bot/scheduler.py` (#6), wire up `PerformanceTracker` on a cadence (#7), and the minor
   items in #8.
