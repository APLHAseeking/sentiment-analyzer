# Trading Bot — Full Review (2026-07-20)

Consolidated review of the live paper-trading bot: how it works end to end, everything that
has gone wrong since launch and how it was fixed, what's still open, and what to do next to
make it run consistently and profitably. Sources: `trading bot/CLAUDE.md` banner,
`docs/CLAUDE-REFERENCE.md#history`, `docs/STATE.md` (root + the now-merged trading-bot copy),
and a direct read of `orchestration/main_loop.py`, `system/config.py`, `risk/risk_manager.py`,
`bot/broker.py`, `bot/portfolio.py`.

Live paper trading since **2026-07-06**. As of this review: 1055 tests passing, branch
`feature/profitable-strategies-lowvol-residmom-insider`, short-selling capability merged in
but disabled by default.

---

## 1. How it works

**Purpose**: a regime-aware, paper-only systematic equity trading bot combining a fundamental
factor screener (primary signal) with two supplementary signals (congressional trades,
corporate-insider filings) and an HMM market-regime overlay, gated by an independent risk
manager. Real-money execution is not implemented — Alpaca paper API or a fully offline
simulated broker only.

**The pipeline, in order** (`orchestration/main_loop.py`, class `RegimeAwareOrchestrator`):

1. **Scheduler** — `APScheduler` `BlockingScheduler`, single-thread executor (jobs run
   sequentially, never concurrently, to protect SQLite/portfolio state), Amsterdam timezone.
   Cron jobs: weekly universe refresh (Mon 07:00), two screener prefetches (13:00, 17:00), two
   entry-signal passes (**15:40** primary, 18:00 secondary — 10 min after NYSE open and again
   later for faster reaction), three intraday stop/circuit-breaker checks (15:45, 17:00,
   20:00), an exit-review pass (16:00), end-of-day snapshot (22:30), weekly report (Fri
   22:45). A DB-backed catch-up mechanism re-runs the entry pass on process restart if today's
   window already passed and no `job_runs` row exists yet — the in-memory scheduler otherwise
   loses whole trading days across restarts (this was a real, repeated incident — see §2).
2. **Signal generation**, each phase independently gated and capped:
   - **Fundamental factor screener** (primary) — `screener/factor_scorer.py`. Composite score
     per S&P 500 ticker from 5 sector-normalized sleeves: value, momentum (residual momentum
     0.40 + 12m momentum 0.25 + SUE 0.15 + 6m momentum 0.12 + 52w-high ratio 0.08), quality,
     low-vol/BAB, short-term reversal. Sleeve weights shift by market regime (momentum
     upweighted in bull/euphoria, low-vol/quality upweighted in bear/crash). Short-interest
     >20% of float excludes a name entirely. Top-N (30) go to AI scoring.
   - **Congressional disclosures** (supplementary) — Capitol Trades scrape, capped at 3% of
     book, 1 new entry/day.
   - **Corporate insider (SEC Form 4)** (supplementary) — EDGAR daily-index parse, open-market
     purchases only, capped via `InsiderConfig`.
   - **Inverse-ETF hedge** — only opens when regime is bear/crash/deep-bear.
3. **AI conviction scoring** (`bot/ai_analyst.py`) — every candidate from all three signal
   sources goes through an LLM (OpenAI `gpt-5.4` default, or Anthropic `claude-sonnet-4-6` via
   `LLM_PROVIDER=anthropic`) for a 1-10 conviction score, rationale, and risk flags.
   **Position size is not LLM-driven** — the model's suggested size is logged for diagnostics
   only and ignored.
4. **Deterministic sizing** (`risk/position_sizing.py`) — ATR-based vol-target formula
   (`per_trade_risk_pct / atr_pct`, clamped to `max_position_pct`), then a ±20% tilt band from
   the AI conviction score.
5. **Regime allocation scaling** (`regime/allocation_engine.py`) — multiplies base size by a
   regime multiplier (0.3 crash → 1.0 bull), a confidence multiplier (zeroed below 40%
   confidence), a stability penalty (0.5× in unstable regimes), and a small seasonal overlay.
6. **Correlation dampening** (`risk/correlation.py`) — reduces size for candidates >0.7
   correlated with existing holdings over 60 days.
7. **Portfolio-vol gate** — scales all new entries down if realized 20-day NAV vol exceeds the
   15% target.
8. **Risk veto** (`risk/risk_manager.py`, "always has final authority") — hard rejects on
   stale data, an active circuit breaker/lockout, per-position cap (8% long), sector cap
   (30%), aggregate invested-capital cap (80%), or ADV liquidity (5%, missing data = reject).
9. **Circuit breakers**, independent of the above: daily loss ≥3% halves new sizes, ≥4% halts
   new entries, ≥6% force-closes everything; weekly loss ≥8% halts entries for the week;
   drawdown from peak ≥15% writes a lock file requiring manual deletion to resume. All
   baselines are restored from `portfolio_log` on startup so breakers survive restarts.
10. **Execution** (`bot/broker.py` → `AlpacaBroker`, or `execution/paper_broker.py` →
    `SimulatedBroker`) — market order, then polls up to ~14s for a confirmed fill.
    `Portfolio.open_position` only books a position if the fill actually confirms
    `OrderStatus.FILLED`; a timeout cancels the dangling order instead of guessing. Same
    fill-gating applies symmetrically on the exit side.

**Config** (`system/config.py`) is one frozen dataclass tree with a `validate()` method
enforcing cross-field invariants (breaker thresholds strictly ordered, sizing caps
consistent, etc.) — everything above is a named, typed field there, not a magic number
buried in code.

**Test suite**: 1055 tests across 40+ modules, run offline (yfinance/Alpaca/scraper/LLM all
mocked via `tests/conftest.py`), covering every component above plus point-in-time backtest
infrastructure.

---

## 2. The logbook — incident history by theme

Full verbatim narrative lives in `docs/CLAUDE-REFERENCE.md#history` and `trading bot/CLAUDE.md`'s
banner; this is the condensed, thematically-grouped version.

### Fill-confirmation / phantom-position bugs (the costliest class — real money-adjacent)
- **2026-07-07 — CF/VTRS phantom positions.** First two live trades ever were booked into the
  local DB from an unconfirmed (timed-out) order poll — neither had actually filled at
  Alpaca. Root cause was 4 compounding bugs: `open_position` didn't require `FILLED` status;
  `place_stop_order` always sent GTC (Alpaca rejects GTC for fractional qty), so booked
  positions had zero resting stop; SEC EDGAR fetch had no backoff, tripping 403s; the
  Capitol Trades scraper treated HTTP 429 the same as 404 and gave up. All fixed same day.
- **2026-07-09.** Root cause of *why* fills kept timing out: `_poll_order_fill` only polled
  ~0.4s total (3×0.2s). Widened to ~14s (15×1s).
- **2026-07-10.** All 4 `open_position()` call sites ignored its boolean return — a failed
  fill still logged "Opened," silently able to exhaust the daily entry quota (one real run:
  8/8 timeouts, all logged as success). Fixed, 4 regression tests. Same bug class found and
  fixed on the **exit** side too (4 more call sites) on 2026-07-13.
- **2026-07-14.** Both stops on the bot's first-ever real fundamental fills (VICI, PFE) were
  rejected by Alpaca's wash-trade check (fill-state propagation lag) — left naked ~2h until
  `enforce_stop_losses()` caught it as designed. Fixed: initial stop placement now retries 3x
  with backoff.

### Scheduler / process-reliability bugs
- **2026-07-10 — zero signals for 4 days.** `BlockingScheduler` is in-memory only; 5 process
  restarts in 3.5 days each silently dropped the rest of that day's cron windows. Fixed with a
  DB-backed catch-up-on-restart mechanism.
- **2026-07-13 — bot found dead ~3 days.** No timeouts anywhere meant one stalled
  yfinance/Alpaca call could wedge the single-thread scheduler forever. Fixed via a shared
  session with explicit timeouts (`market_data/yf_session.py`). Same day: `run_morning_pipeline`
  was scheduled 1.5h before NYSE open; a same-night restart placed 7 real orders into a closed
  market, all 7 timed out. Fixed with `_nyse_is_open_now()` and moving the entry window to
  15:40/18:00.
- **2026-07-14.** Monday NAV-baseline collision fixed (`week_start_nav` vs `day_start_nav`).
  A ~2h+ scheduler idle wedge occurred with zero error logged — **root cause never
  identified** (sleep and job-hang both ruled out).
- **2026-07-15/16/17 — sleep-induced wedges.** 3 wedges in 3 days traced to real macOS sleep
  events; fixed with `caffeinate -i -s`. Wedges recurred anyway (2×) — traced further to macOS
  Power Nap ("Sleep Service Back to Sleep") cycling, which `caffeinate -i -s` doesn't cover.
  Mitigated with `sudo pmset -a powernap 0`; a full `disablesleep` or migrating off the laptop
  remain documented, un-actioned options if it recurs.
- **2026-07-17 — reliability watchdog built.** After ~10 cumulative "not trading" incidents
  traced to ~15 structurally distinct bug classes (not one recurring bug), built
  `monitoring/watchdog.py` (15-min liveness/staleness/deploy-freshness auto-restart) and
  `monitoring/dead_mans_switch.py`. The watchdog itself then shipped two of its own live bugs
  same week: a bare `"python3"` string resolving to the wrong interpreter under a minimal PATH
  (fixed with `sys.executable`), and `nohup` failing outright under a LaunchDaemon with no
  controlling terminal to detach from (fixed by dropping it for `start_new_session=True`).
- **2026-07-14 through 2026-07-20 — launchd/BTM saga, ultimately abandoned.** Extensive
  multi-session effort to get the watchdog/dead-man's-switch auto-restarting via launchd
  (LaunchAgent → LaunchDaemon → BTM reset → cron fallback) never reached a durable fix — see
  §3 for the full root-cause detail and current status. **Closed 2026-07-20 per explicit user
  decision**: both scripts are permanently manual-only; do not re-propose launchd/cron
  automation without materially new information. The main bot process itself was never
  affected by this — it has always run via manual `nohup`/`caffeinate` regardless.
- **Stale-deploy pattern, recurring 3+ times independently** (2026-07-14, 2026-07-15,
  2026-07-17): a running bot process found to predate that session's own fix commits by
  minutes to hours, meaning it was serving stale code until manually restarted. No systemic
  fix beyond the watchdog's own deploy-freshness check (`bot_status.json`, commit hash).

### Test-hygiene / data-integrity bugs
- **2026-07-10.** `tests/test_orchestrator.py` was writing fake signal rows directly into the
  **live production `trading.db`** on every pytest run (unmocked insert) — 287 fake rows
  accumulated before being caught and deleted.
- **2026-07-15.** `sqlite3.Row` has no `.get()` — 5 call sites in `main_loop.py`, only
  reachable once real positions exist. Two sat inside the deleverage circuit-breaker's
  force-close path: a real DELEVERAGE event would have closed zero positions. Root cause of
  it surviving this long: existing tests mocked positions as dict literals, which support
  `.get()` fine, never exercising the real `sqlite3.Row` type.
- **2026-07-17 (recurring class, 3 separate hits).** New watchdog tests repeatedly didn't mock
  writes to real state files (`watchdog_restart_history.json`, `bot_status.json`) — caught and
  fixed each time before merging, but the same test-isolation mistake recurred 3 times in one
  week.

### Strategy / signal-logic findings
- **2026-06-23 re-review.** 5 pre-existing Critical bugs found in a second-pass audit after
  the first pass missed them: wrong stop-order cancelled on trail-up, an enum
  string-comparison bug that made Alpaca stop lookups never match, weekly-loss breaker able to
  suppress same-week DELEVERAGE detection, HMM transition-matrix update done in linear instead
  of log scale (degrading regime persistence), and an HTML scraper fallback that never
  normalized buy/sell direction, silently losing the entire congressional signal during a
  JSON-API outage.
- **2026-07-14 — SUE (earnings-surprise) PIT backtest.** Full point-in-time-correct backtest
  built with a decision rule pre-committed before seeing results. Gate **failed** on both
  horizons (20d t=0.87/IR=0.24; 60d t=1.41/IR=0.30) and on 60d time-stability (sign flip
  between halves) — sub-weight stays at 0.15, no code change. A clean, honest null result.
- **2026-07-17 — first review of trading logic itself (not reliability).** Found the real live
  entry hurdle was 4.5%, not the documented 1.0% floor (a cost constant never updated after an
  earlier prompt-text-only loosening) — fixed. **Congressional signal tested against real
  cached data for the first time: significantly negative excess return** (1mo -0.64%
  t=-2.57; 3mo -2.54% t=-4.93) — left unchanged pending an explicit decision (repo convention:
  never silently auto-edit a signal weight off one finding).
- **2026-07-15 — widened screener review.** Real bottleneck for "too few trades" was
  `screener_top_n=12` (only top 12 of 503 scored tickers reached AI scoring) and
  `max_positions_per_day=3`. Raised to 30 and 5.
- **2026-07-20 — risk-limit widening.** `per_trade_risk_pct` 0.15→0.20 and `max_positions`
  20→30, both raised the same day as this review because the book was running ~39% invested
  against an 80% cap — idle capital, not a risk problem being loosened defensively. No live
  track record yet at the new limits.

### Data-source breakage
- Russell 1000 universe fetch has been attempted and failed across essentially every session
  since 2026-07-06: iShares CSV (bot-protection HTML), FTSE Russell (redirects),
  stockanalysis.com (JS-rendered shell, no API), SlickCharts (403s). Universe remains
  S&P-500-only in practice. Blocked on obtaining `FMP_API_KEY` (or another paid source).
- `fja05680/sp500`'s PIT-constituents CSV URL changed format mid-project (2026-07-15), broke
  silently until caught and fixed.

---

## 3. Currently open / unresolved

- **Phase 0 gate: BLOCKED ON DATA.** All historical backtest numbers (including the residual
  momentum/low-vol Sharpe figures cited in the factor scorer's design rationale) are
  look-ahead biased until real point-in-time data is acquired. This is the single biggest
  gap between "backtested well" and "actually validated" for the bot's core edge.
- **Congressional signal has a real, measured negative edge and is still live**, capped at 3%
  of book / 1 entry per day. This is the one signal source with an actual negative-return
  finding against real data, not just an unproven one — see §2, 2026-07-17.
- **Russell 1000 universe unresolved** — bot trades the S&P 500 only, despite the branch/
  design intent implying broader coverage. No free path found after ~7 sources tried; needs
  `FMP_API_KEY` or another paid source, or an explicit decision to accept S&P-500-only scope.
- **Short-selling merged but gated off** (`enable_short_selling=False`). Five design questions
  from the original spec are still open and must be resolved before ever flipping it on:
  regime-aware short sizing, hedge-mechanism overlap, aggregate gross/net exposure cap, short
  borrow fees (not modeled in the AI's cost hurdle), and `SimulatedBroker` cannot execute a
  short at all. Additionally, **the Alpaca negative-qty sign convention for shorts has never
  been live-verified against a real paper account** — only a runtime self-check exists that
  alerts if it's ever wrong.
- **Two just-raised risk parameters with no track record**: `per_trade_risk_pct` (0.15→0.20)
  and `max_positions` (20→30), both changed 2026-07-20. Worth watching closely over the next
  several trading sessions rather than assuming they're correctly calibrated.
- **launchd/BTM root cause — two competing write-ups that were never reconciled.** Root
  `docs/STATE.md` traced the failure (via `codesign -dv`, a full `sfltool resetbtm`, and 4
  systematically eliminated hypotheses) to a fundamental macOS block on unsigned,
  hand-placed plist launch items — concluding a real fix would require packaging the scripts
  inside a signed `.app` registered via `SMAppService`, a non-trivial new engineering task,
  not attempted. `trading bot/CLAUDE.md`'s 2026-07-20 entry frames the same failure as a
  Background Task Management **approval-cache desync** after reboot, fixable in principle via
  the System Settings toggle or `sudo sfltool resetbtm` (both declined by the user). These
  read as two sessions independently hitting the same wall at different depths — the
  `SMAppService` conclusion is the more thoroughly evidenced of the two (it's the one that
  actually ran `resetbtm` and still failed), but this was never explicitly cross-checked
  against the later write-up. **Moot for now**: the decision to stop pursuing
  launchd/cron automation stands regardless of which root cause is correct.
- **2h+ unexplained scheduler idle wedge (2026-07-14)** — sleep and job-hangs both ruled out,
  no root cause ever found. Superseded in practice by the watchdog's bounded auto-recovery,
  but the underlying cause is still unknown.
- **`main_loop.py`'s 4 near-duplicate signal-processing methods** — flagged 2026-07-17 as a
  real refactor opportunity, deliberately deferred as too large/risky to fold into a bug-fix
  pass.
- **Watchdog/dead-man's-switch: permanently manual-only** (closed decision, not a bug) — the
  main bot has no OS-level supervisor; an unattended crash or reboot needs a human to notice
  and run `python -m monitoring.watchdog` by hand.
- **Doc hygiene**: this review folds the duplicate `trading bot/docs/STATE.md` into the
  canonical root `docs/STATE.md` (see that file's own `## Constraints` note flagging it) and
  removes the duplicate, per this repo's single-STATE.md convention.

---

## 4. Recommendations, prioritized

**Profitability-blocking (affects whether the strategy is actually good, not just running):**
1. Decide the congressional signal's fate explicitly — it's the only signal with a measured
   negative real-data result and is still live at 3%/1-per-day. Reduce further, gate it off
   entirely, or document a specific reason to keep it as-is; "left unchanged pending decision"
   has now persisted across at least one full review cycle.
2. Acquire real point-in-time data to clear the Phase 0 gate — every backtest number quoted in
   design docs (residual momentum Sharpe ~0.88, low-vol beta ~0.6, etc.) is provisional until
   this happens. This is the highest-leverage unblock for trusting the strategy at all.
3. Resolve the Russell 1000 gap one way or the other — either get `FMP_API_KEY` and wire it
   up, or explicitly narrow the documented scope to S&P 500 so it stops reading as an open bug.

**Risk-blocking (affects capital safety before scaling up):**
4. Live-verify the Alpaca short-qty sign convention against a real paper account before ever
   setting `enable_short_selling=True` — currently only alarmed-on-mismatch, not confirmed.
5. Monitor the books at the new `per_trade_risk_pct=0.20`/`max_positions=30` limits for at
   least a week or two of live sessions before considering any further loosening.

**Reliability (lower priority — bounded, not blocking):**
6. The watchdog already bounds downtime to ~15-30 min when it's running, but nothing currently
   restarts the watchdog itself after a reboot — worth an explicit decision on whether that
   residual gap is acceptable long-term, separate from re-litigating launchd/cron.

**Housekeeping (done as part of this review):**
7. Duplicate `STATE.md` merged/removed.
8. Two conflicting launchd postmortems cross-referenced in §3 rather than left silently
   contradictory.
