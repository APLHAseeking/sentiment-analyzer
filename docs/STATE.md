# STATE

## Goal
Fix why the live paper bot isn't trading and keep it trading reliably; investigate new
factor edges without deploying unvetted ones. Root-caused across several sessions
(dead-for-3-days hang, phantom fills, wrong pipeline timing, NAV-baseline bug, unconditional
fundamental_signals insert, stop-loss wash-trade race — see Done). Bot is currently live,
restarted, healthy, running the latest fixes.

## Now
Session closing (2026-07-15, ~19:00 CEST). Final live check before close: bot process PID
11292 healthy (`caffeinate -i -s` wrapped, up ~41 min), 11 open positions (VICI/PFE from
07-14 + HIG/FSLR/VZ/AFL/LVS + hedges SH/PSQ/RWM/EFZ all opened 07-15), no `RISK_LOCKOUT`,
git status clean (only the pre-existing unrelated `MIGRATION-LOG.md` drift, not touched).
An OS-detached watcher script (survives this session closing — confirmed reparented to
launchd, PPID=1) is running: sleeps until 2026-07-16 15:45 CEST, then writes a status check
(process alive? `job_runs` row for that date? did catch-up have to fire, meaning the 15:40
cron itself still failed?) to `trading bot/tomorrow_1540_check.log`. Read that file directly,
or just ask a future session to check — either works.

Earlier the same day: implemented the user-approved "widen screener review" design (top-N
12→30 via new `UniverseConfig.screener_top_n`, daily cap 3→5) — commit `7a185ce`. Found the
bot wedged 3 separate times that day; root-caused to real macOS sleep events (not a code
bug — see Done). Also found and fixed a real, severe latent bug (`sqlite3.Row` has no
`.get()`, 5 call sites, only reachable once real positions exist) that crashed the
catch-up pipeline and would have crashed the deleverage circuit-breaker's force-close
path — commit `e7b15fa`. Bot now runs `nohup caffeinate -i -s python3 run_bot.py` instead
of bare `nohup python3`.

## Next
- Check `trading bot/tomorrow_1540_check.log` after 2026-07-16 15:45 CEST (written by an
  OS-detached watcher, PPID=1, started 2026-07-15) — confirms whether the caffeinate fix
  held overnight without needing a manual restart. If the file's missing, the watcher died;
  just check live (`ps aux | grep run_bot.py` + `job_runs` for that date) instead.
- Once user adds `FMP_API_KEY` to trading bot/.env: live-test FMP's `russell1000_constituent`
  endpoint (existence unconfirmed), wire up if it works. 4 free/no-signup alternates tried
  across sessions (iShares, FTSE Russell, stockanalysis.com, SlickCharts) — none viable.
- requirements.txt pinning/lockfile: still not started.
- Sleep-induced wedges: `caffeinate -i -s` (in place 2026-07-15) covers idle/AC sleep but NOT
  lid-closed (clamshell) sleep — verified via research, not fixable by caffeinate at all. If
  it recurs specifically from a lid-close, next step is user's call between `sudo pmset -a
  disablesleep 1` (same laptop, real battery/heat tradeoff) or migrating off the laptop
  entirely (Oracle Cloud Always Free / a small VPS / a home always-on device) — see
  `docs/RUNBOOK.md#sleep-wedges` for the full writeup. Not actioned without the user choosing.

## Constraints
- User 2026-07-10: add launchd auto-restart supervision (KeepAlive) alongside the code-level catch-up fix. [CLOSED 2026-07-14 — root cause is KeepAlive itself being blocked by macOS Background Task Management for an unsigned binary, not fixable; user accepted manual nohup + dead-man's-switch instead.]
- User 2026-07-10: include an entry-hurdle loosening proposal now (3x cost / 1.0% absolute), evaluated via observability not a pre-hoc backtest (not feasible — no stored expected_return field).
- User 2026-07-14: SUE PIT backtest — reuse the exact SUE formula unmodified, only new SEC call is companyfacts, zero new LLM calls, cache to parquet, do not touch the live 0.15 weight or enable anything (recommendation only). [Honored throughout — screener/factor_scorer.py never touched.]
- Original task: keep test suite green; follow repo CLAUDE.md/CODE.md conventions; log changes in docs/CLAUDE-REFERENCE.md#history
- Repo: tests offline; temperature=0 on LLM calls; no new deps/frameworks without flagging
- Global: never git push unless asked; commit per meaningful unit

## Decisions
- DECISION: SUE PIT backtest ran 2026-07-14, gate failed (t<2 and IR<0.5 at both 20d/60d, plus a 60d stability sign-flip) — sub-weight stays 0.15 in `_MOMENTUM_WEIGHTS`. Not revisiting without a genuinely new argument (EDGE_BACKLOG.md).
- DECISION: B2 = hard exclude when shortPercentOfFloat > UniverseConfig.max_short_pct_float (20%, 0 disables); missing passes.
- DECISION: insider feed = EDGAR daily form.idx primary (2 newest published, newest first), getcurrent fallback, max_filings_per_run 300 budget.
- DECISION: event-calendar gate NOT carved out for PEAD — explicit future decision (EDGE_BACKLOG).
- DECISION: XBRL via frames API (~20 req/screen, 20h shelve cache xbrl_frames_cache, gitignored); SUE anchor shifts to filer's newest available quarter (max 2 stale) — slot 0 empty ~40 days post-quarter, Q4 frames sparse.
- DECISION: main bot's launchd auto-restart closed permanently — root cause is KeepAlive blocked by macOS Background Task Management for an unsigned Homebrew binary; a StartInterval-supervisor workaround was proposed and declined. Bot runs via manual nohup indefinitely.

## Facts
- Repo root: /Users/thomasvromen/Documents/Claude code test; bot in "trading bot/" (space — quote it)
- Test command: cd "trading bot" && pytest — 947 tests green as of 2026-07-15 (later same day), 0 known failures
- Branch: feature/profitable-strategies-lowvol-residmom-insider; 45+ commits ahead of origin, not pushed
- SUE PIT backtest modules: screener/xbrl_pit_sue.py (companyfacts fetch/cache, PIT quarterly EPS, PIT SUE), backtesting/pit_constituents.py (PIT S&P 500 membership), backtesting/backtest_sue_pit.py (drift/HAC/gate). Report: trading bot/docs/SUE_PIT_BACKTEST_2026-07-14.md. Cache dir trading bot/pit_cache/ (gitignored).
- Bot process: check via `ps aux | grep run_bot.py`; started via `nohup caffeinate -i -s python3 run_bot.py > bot.log 2>&1 &` from inside "trading bot/" (caffeinate wrapper added 2026-07-15, see RUNBOOK.md#sleep-wedges; note `python`, not `python3`, does not exist in this shell — use python3). Dead-man's-switch: `launchctl list | grep tradingbot`.
- Live health check command sequence: bot process alive + its start time vs latest commit timestamps (stale-process-running-old-code is a recurring real failure mode, caught 3x this session) + `sqlite3 trading.db "SELECT * FROM job_runs ORDER BY rowid DESC LIMIT 3;"` + `ls RISK_LOCKOUT` (should not exist) + `launchctl list | grep tradingbot`.
- docs/guardrails/MIGRATION-LOG.md has PRE-EXISTING uncommitted changes (not this task's, predates this session) — do not commit blindly.

## Done
Full narrative for every entry below: trading bot/docs/CLAUDE-REFERENCE.md#history (this
project's permanent changelog — pointers only here per SESSION.md S3/S8).
- 2026-07-15 (session close-out verification): re-verified live bot health fresh rather than
  trusting prior claims — found the running process (PID 62191) pre-dated that morning's two
  fix commits (a0cd1c4, 91607a5) by ~17 hours, same stale-process pattern as 2026-07-14.
  User approved a restart; new PID 71495 confirmed to postdate both fixes. Also found and
  fixed a real, separate bug: `backtesting/pit_constituents.py`'s hardcoded download URL
  404s — fja05680/sp500 renamed its CSV (added a space before the parens) sometime after
  2026-07-14; fixed to the new URL, verified live (old 404s, new returns 200, same schema).
  Full suite 942 passed, 0 known failures.
- 2026-07-14/15 (live dig-in session, commits a0cd1c4 + 91607a5, concurrent with/after the
  SUE PIT backtest below): root-caused why fundamental candidates weren't converting to
  trades — `_process_fundamental_candidate` persisted `fundamental_signals` rows even when
  `open_position` failed. Fixed: insert now gated on `opened`. Separately found the live
  process (PID 51755) had gone idle for 2h+ with zero cron dispatch, no error logged — no
  definitive code cause found (see Next); restarted (new PID 62191), producing the bot's
  first-ever real fills (VICI, PFE). That surfaced a third bug: both stops rejected by
  Alpaca's wash-trade check (transient fill-state propagation lag), leaving both positions
  naked until `enforce_stop_losses()` caught it ~2h later as designed. Fixed: initial stop
  placement now retries 3x with backoff. RESULT: full suite 942 passed.
- 2026-07-14 (SUE PIT backtest): built a full point-in-time-correct backtest of the SUE
  signal per a plan confirmed with the user before building (PIT date semantics, per-horizon
  HAC gate not pooled, real PIT universe, decision rule stated before results) — plan at
  `docs/superpowers/plans/2026-07-14-sue-pit-backtest.md`, subagent-driven dev with spec +
  code-quality review per task. Found and fixed two real bugs against real data:
  `original_quarterly_eps`'s calendar-quarter bucketing (SEC buckets by nearest quarter-end
  boundary to `end`, not end/start month — 3 iterations, verified against 7 tickers + a full
  Costco history scan for the 52/53-week-fiscal-calendar collision case) and a history-
  truncation bug that starved the SUE denominator (absurd outliers, e.g. 7.2e15 on one real
  event). RESULT: gate failed both horizons (20d t=0.87/IR=0.24, 60d t=1.41/IR=0.30) and
  60d stability (sign flip); honesty check confirmed no residual look-ahead. SUE sub-weight
  stays 0.15. Full report: `docs/SUE_PIT_BACKTEST_2026-07-14.md`.
- 2026-07-14 (independent verification session): live-verified bot health rather than
  trusting the banner — found the running process (PID 38576) pre-dated the NYSE-hours fix
  (b4938bb) by 37 minutes; restarted (new PID 51755) to deploy it. Dead-man's-switch
  confirmed active, RISK_LOCKOUT absent, job_runs current. RESULT: full suite 909 passed.
- 2026-07-13/14: found bot dead ~3 days, fixed a missing-timeout hang across every
  yf.Ticker/Alpaca call site; fixed a Monday NAV-baseline collision; root-caused why the bot
  had made zero real trades ever (entry orders placed outside NYSE hours) with a
  `_nyse_is_open_now()` guard + corrected schedule; fixed the same open_position-bool-ignored
  bug class on the exit side; built the dead-man's-switch; closed out the launchd
  auto-restart investigation (root cause: KeepAlive blocked for an unsigned binary).
- 2026-07-06/10: Phase 1 review (A1-A8, B1-B3 XBRL/short-interest/insider signals,
  commits 6c77981..868aa2d); test-DB-pollution fix; cross-model debate feature;
  scheduler catch-up-on-restart; iShares hardening.
- 2026-07-15 (widen screener review, commit `7a185ce`): user wanted more trades — found the
  real bottleneck was `_SCREENER_TOP_N=12` (only top 12 of 503 factor-scored tickers reached
  AI scoring) and `max_positions_per_day=3`. Brainstormed with user (spec at
  `docs/superpowers/specs/2026-07-15-expand-screener-review-design.md`), raised both to 30
  and 5, promoted the top-N cutoff to a proper `UniverseConfig.screener_top_n` field matching
  the codebase's existing config pattern. 3 new/updated tests, full suite 945 passed.
- 2026-07-15 (sqlite3.Row.get() bug, commit `e7b15fa`): live-verifying the widen-review deploy
  found the catch-up pipeline had crashed (`AttributeError: 'sqlite3.Row' object has no
  attribute 'get'`) — 5 call sites in `orchestration/main_loop.py`, all only reachable once
  real open positions exist (first happened 2026-07-14). Two sit inside the deleverage
  circuit-breaker's force-close path — a real DELEVERAGE event would have closed zero
  positions. Root cause of the bug surviving this long: existing tests mocked
  `get_open_positions()` with dict literals (which support `.get()` fine), never exercising
  the real `sqlite3.Row` return type. Fixed all 5 sites, converted 2 tests + added 2 new ones
  to use real rows via the `db` fixture (proven red/green). Full suite 947 passed.
- 2026-07-15 (sleep-induced scheduler wedges, commit `0c229e1`): the bot wedged 3 times in 3
  days (silent, zero cron dispatch, no error) — previously assumed an unexplained APScheduler
  bug (2026-07-14 STATE.md said "ruled out sleep," which was wrong — a re-check with a
  corrected `pmset -g log` grep found real Sleep events in that exact window too). All 3
  wedges directly correlate with a genuine `Entering Sleep state` log line within minutes of
  onset; a process `sample` taken while wedged each time showed genuinely idle threads, not a
  hang. Fixed: `caffeinate -i -s` now wraps every launch. Researched and documented (verified
  via web search, not assumed) that this doesn't cover lid-closed/clamshell sleep — no
  `caffeinate` assertion can override the hardware lid sensor without an external display.
  Wrote up the full escalation path (`sudo pmset -a disablesleep 1` vs. migrating off the
  laptop entirely) in `docs/RUNBOOK.md#sleep-wedges` — a decision left to the user, not
  actioned.
- 2026-07-16 (scheduler wedge recurred despite caffeinate fix): user asked to check the bot;
  found it wedged again — process (PID 11292) alive since 07-15 18:20 with the `caffeinate`
  assertion active 24h+, but zero `job_runs` since the 07-15 22:30 EOD snapshot, the 15:40
  entry window and 20:00 intraday check both silently missed, and catch-up-on-restart never
  fired on its own (confirmed via the OS-detached watcher's `tomorrow_1540_check.log`).
  `pmset -g log` showed the laptop repeatedly cycling `Sleep`/`DarkWake` ("Sleep Service Back
  to Sleep") on battery all afternoon — the documented clamshell/Power-Nap gap in
  `caffeinate -i -s` coverage, not a new bug. User approved a restart (killed PID 11292 by
  PID, relaunched via the same `nohup caffeinate -i -s python3 run_bot.py` command per
  `docs/RUNBOOK.md#safe-restart`). Live-verified, not just log-watched: catch-up fired
  immediately, ran the full screener+AI pipeline, and opened a real position (ACGL, 0.9%,
  conv=7) — confirmed via `job_runs` and `positions` rows for 2026-07-16, not just log text.
  Scheduler now live on a fresh 22:30 EOD job.

## Open items
- eps_trend daily snapshot collection (estimate revisions) — recorded in EDGE_BACKLOG.md, not built
- Insider routine-buyer filter — recorded in EDGE_BACKLOG.md, viable after ~1yr of insider history (feed started 2026-07-07)
- docs/guardrails/MIGRATION-LOG.md still shows as modified in `git status` — pre-existing uncommitted drift, predates this session, not touched
- Russell 1000 universe unresolved — genuinely blocked on the user obtaining `FMP_API_KEY` (or a paid data source); no free/no-signup alternative found after 4 sources tried across sessions
- Scheduler wedges (2026-07-14 x2, 2026-07-15 x1, 2026-07-16 x1) — root cause confirmed real
  macOS sleep events (`pmset -g log`), not a code bug, but `caffeinate -i -s` does NOT fully
  prevent recurrence: 07-16's wedge happened with the caffeinate assertion active the whole
  time, correlating with repeated "Sleep Service Back to Sleep"/DarkWake cycling on battery —
  the documented clamshell/Power-Nap gap, not yet mitigated. Manual restart is still required
  when it happens; the user has not yet chosen between `sudo pmset -a disablesleep 1` or
  migrating off the laptop (see `docs/RUNBOOK.md#sleep-wedges`).

## Failed attempts
(none this session — every fix attempt that failed a first pass was corrected same-turn, e.g. two wrong SUE quarter-bucketing hypotheses before the verified-correct one, documented in the SUE PIT backtest commit history rather than repeated here)
