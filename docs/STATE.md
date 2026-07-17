# STATE

## Goal
Fix why the live paper bot isn't trading and keep it trading reliably; investigate new
factor edges without deploying unvetted ones. Root-caused across several sessions
(dead-for-3-days hang, phantom fills, wrong pipeline timing, NAV-baseline bug, unconditional
fundamental_signals insert, stop-loss wash-trade race — see Done). Bot is currently live,
restarted, healthy, running the latest fixes.

## Now
Strategy-review thread (this session, 2026-07-17): **CLOSED OUT**, report + all remediation
done, suite green, nothing committed yet — see Done entry below for the full result.

Reliability thread (concurrent, separate session) — session closing (2026-07-17, ~11:05 CEST). Independently re-verified everything below rather
than trusting the docs as found (this work landed from a concurrent session, not this one):
bot process PID 31860 alive and healthy, `bot_status.json` commit matches HEAD (`c43bd66`),
watchdog LaunchAgent loaded (`launchctl list`) with real log evidence of a correct
`healthy:recent_activity` cycle, full suite freshly re-run at 975 passed, `RISK_LOCKOUT`
absent, 12 open positions (the 11 from 07-15 plus `ACGL` opened 07-16). Found and fixed one
real inaccuracy: `docs/RUNBOOK.md` claimed the `sudo pmset -a powernap 0` mitigation was
"applied 2026-07-17" — `pmset -g custom | grep powernap` still reads `1` on both Battery and
AC, so that claim was false/premature. Corrected the doc; the action is still outstanding
and needs the user to run it interactively (agent can't `sudo`). Read
`monitoring/watchdog.py` in full — restart logic is sound (PID-reuse-safe kill, 10-min quiet
gate, fires an alert either way); one minor non-blocking gap noted: no SIGKILL fallback if
SIGTERM doesn't land within 10s.

**Update, same day, later (this session):** `sudo pmset -a powernap 0` is now actually
applied — user asked for a faster option than opening Terminal; ran
`osascript -e 'do shell script "pmset -a powernap 0" with administrator privileges'`, which
pops the native macOS auth dialog (password/Touch ID) instead of requiring an interactive
`sudo` in a real TTY (the `!`-prefix path tried first failed: no TTY available in that
session either). Verified via `pmset -g custom | grep powernap` — both lines now read `0`.
`docs/RUNBOOK.md` updated accordingly.

Earlier the same day (concurrent session, not this one): built and deployed the reliability
watchdog after the bot wedged twice more in 24h (07-16 ~22:30→18:51, and again overnight
07-16 20:00→07-17 10:44) — see Decisions/Done below.

Earlier the same day: implemented the user-approved "widen screener review" design (top-N
12→30 via new `UniverseConfig.screener_top_n`, daily cap 3→5) — commit `7a185ce`. Found the
bot wedged 3 separate times that day; root-caused to real macOS sleep events (not a code
bug — see Done). Also found and fixed a real, severe latent bug (`sqlite3.Row` has no
`.get()`, 5 call sites, only reachable once real positions exist) that crashed the
catch-up pipeline and would have crashed the deleverage circuit-breaker's force-close
path — commit `e7b15fa`. Bot now runs `nohup caffeinate -i -s python3 run_bot.py` instead
of bare `nohup python3`.

## Next
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
- User 2026-07-17 (this review thread): report findings for sign-off first, no fixes applied
  during the review itself; bounded new empirical work OK (re-run existing backtest scripts
  against already-cached PIT data) but no new scraping/data collection (Phase 0 gate still
  blocked); do not touch reliability/scheduler/watchdog code — that's the other session's
  active thread; any eventual remediation edit to `orchestration/main_loop.py` must check
  `git status`/`git log` on that file first to avoid colliding with in-flight reliability fixes.
- User 2026-07-10: add launchd auto-restart supervision (KeepAlive) alongside the code-level catch-up fix. [CLOSED 2026-07-14 — root cause is KeepAlive itself being blocked by macOS Background Task Management for an unsigned binary, not fixable; user accepted manual nohup + dead-man's-switch instead. SUPERSEDED 2026-07-16 — user asked for a permanent fix after a ~20h undetected wedge, reversing the "manual restart is acceptable" premise; see the 2026-07-16 DECISION under `## Decisions`. The auto-restart watchdog built then uses StartInterval, not KeepAlive, so the original technical blocker doesn't apply to it.]
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
- DECISION: main bot's launchd auto-restart (KeepAlive) closed permanently — root cause is KeepAlive blocked by macOS Background Task Management for an unsigned Homebrew binary, not fixable via plist/Settings. Bot still runs via manual nohup. (Does NOT apply to the StartInterval watchdog below — StartInterval isn't KeepAlive and isn't blocked by this gate, confirmed by the dead-man's-switch already running fine as a StartInterval LaunchAgent.)
- DECISION (2026-07-16, reverses the 2026-07-14 StartInterval-supervisor decline recorded under `## Constraints`): built an active auto-restart watchdog (`monitoring/watchdog.py`) after a wedge sat undetected ~20h under the alert-only model — the 2026-07-14 reasoning ("manual restart is an acceptable tradeoff... dead-man's switch already detects a stale bot") assumed a human would notice within hours; it didn't bound downtime the way that assumed. Every restart gated on 10 min of `bot.log` quiet so it never kills a legitimately running pipeline. See `trading bot/docs/RUNBOOK.md#watchdog`.
- DECISION (2026-07-17): sleep mitigation — disable Power Nap only (`sudo pmset -a powernap 0`), not full `disablesleep` or a laptop migration. Targeted at the specific `pmset -g log` symptom observed (`Sleep Service Back to Sleep` cycling, `powernap=1` on both Battery/AC); the heavier options remain documented in `docs/RUNBOOK.md#sleep-wedges` if this proves insufficient. Not urgent now that the watchdog bounds downtime regardless.

## Facts
- Repo root: /Users/thomasvromen/Documents/Claude code test; bot in "trading bot/" (space — quote it)
- Test command: cd "trading bot" && pytest — 975 tests green as of 2026-07-17 (freshly re-run), 0 known failures
- Branch: feature/profitable-strategies-lowvol-residmom-insider; 45+ commits ahead of origin, not pushed
- SUE PIT backtest modules: screener/xbrl_pit_sue.py (companyfacts fetch/cache, PIT quarterly EPS, PIT SUE), backtesting/pit_constituents.py (PIT S&P 500 membership), backtesting/backtest_sue_pit.py (drift/HAC/gate). Report: trading bot/docs/SUE_PIT_BACKTEST_2026-07-14.md. Cache dir trading bot/pit_cache/ (gitignored).
- Bot process: check via `ps aux | grep run_bot.py`; started via `nohup caffeinate -i -s python3 run_bot.py > bot.log 2>&1 &` from inside "trading bot/" (caffeinate wrapper added 2026-07-15, see RUNBOOK.md#sleep-wedges; note `python`, not `python3`, does not exist in this shell — use python3). Dead-man's-switch: `launchctl list | grep tradingbot`.
- Live health check command sequence: bot process alive + its start time vs latest commit timestamps (stale-process-running-old-code is a recurring real failure mode, caught 3x this session) + `sqlite3 trading.db "SELECT * FROM job_runs ORDER BY rowid DESC LIMIT 3;"` + `ls RISK_LOCKOUT` (should not exist) + `launchctl list | grep tradingbot`.
- docs/guardrails/MIGRATION-LOG.md has PRE-EXISTING uncommitted changes (not this task's, predates this session) — do not commit blindly.

## Done
Full narrative for every entry below: trading bot/docs/CLAUDE-REFERENCE.md#history (this
project's permanent changelog — pointers only here per SESSION.md S3/S8).
- 2026-07-17 (strategy/profitability review + full remediation, this session): full
  trading-logic review (not reliability/uptime) + all findings fixed same session. RESULT:
  972 passed, 0 failed. Full narrative: `trading bot/docs/CLAUDE-REFERENCE.md#history`
  (2026-07-17 entry) and `trading bot/CLAUDE.md`'s status banner. Commit `e9e0ee7` (code) +
  a follow-up docs-only commit folding the standalone review doc into the two files above per
  this repo's own convention (retire `TRADING_BOT_REVIEW_*.md` once fully remediated).
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
- 2026-07-17 (reliability watchdog, commits `654eb49`/`41691ae`/`c43bd66`): user asked for a
  permanent fix after ~10 cumulative "not trading"/downtime incidents, wanting a "never happen
  again" guarantee rather than another one-off patch. Research (delegated to an Explore
  subagent) found the incidents collapse into ~15 structurally distinct bug classes, not one
  recurring bug — so the plan targeted bounded auto-recovery regardless of root cause, not
  another single-bug fix. Built: (1) `job_runs` coverage extended from just
  `run_morning_pipeline` to all three core cron jobs (`run_intraday_check`, `run_eod`) — only
  the first was ever recorded, so a wedge occurring after the morning pipeline already
  succeeded was previously invisible to any freshness check; (2) `monitoring/status_file.py`
  — writes `bot_status.json` (pid, git commit, started_at) at every `initialize()` call, the
  only way an external process can know the bot's PID/commit without any in-process
  self-monitoring; (3) `monitoring/watchdog.py` — a new `StartInterval` LaunchAgent (15 min),
  checks process liveness, per-job staleness, and deploy freshness, and auto-restarts on any
  of them, gated on 10 minutes of `bot.log` quiet so a legitimate long-running catch-up
  pipeline (observed to take ~10 min) is never mistaken for a wedge; kill is verified via
  `ps -p <pid> -o command=` containing `run_bot.py` first, never by PID alone (PID-reuse
  safety). Caught a real bug before it shipped: the `orch`/`orch_fitted` test fixtures and 2
  direct-construction tests in `test_event_calendar.py` would have called the real
  `write_status_file()` on every test run, clobbering the live bot's actual `bot_status.json`
  with a fake test PID — fixed by mocking it at all 4 construction sites before any test ran
  against the new code. Live-verified end-to-end, not just unit-tested: caught the bot wedged
  *again* (overnight 07-16 20:00 CEST -> 07-17 10:44, a live real-time recurrence found during
  this exact work), restarted it to deploy the new code, and confirmed the watchdog correctly
  read the fresh status file and reported `healthy:recent_activity` on a forced cycle. Full
  suite: 975 passed (was 947 at session start).

- 2026-07-17 (session close-out verification): independently re-verified the concurrent
  session's watchdog work rather than trusting its docs — confirmed live (process, status
  file, launchd registration, a real `healthy:recent_activity` log line, fresh 975-pass full
  suite run) and read `monitoring/watchdog.py` in full (restart logic sound). Found and
  corrected one real inaccuracy: `docs/RUNBOOK.md` claimed `sudo pmset -a powernap 0` was
  "applied" — verified still `1` on both power sources, action remains outstanding for the
  user. RESULT: all claims now backed by fresh evidence, one doc correction committed.

## Open items
- eps_trend daily snapshot collection (estimate revisions) — recorded in EDGE_BACKLOG.md, not built
- Insider routine-buyer filter — recorded in EDGE_BACKLOG.md, viable after ~1yr of insider history (feed started 2026-07-07)
- docs/guardrails/MIGRATION-LOG.md still shows as modified in `git status` — pre-existing uncommitted drift, predates this session, not touched
- Russell 1000 universe unresolved — genuinely blocked on the user obtaining `FMP_API_KEY` (or a paid data source); no free/no-signup alternative found after 4 sources tried across sessions
- Scheduler wedges (2026-07-14 x2, 2026-07-15 x1, 2026-07-16 x2 incl. one caught live
  2026-07-17 morning) — root cause confirmed real macOS sleep events (`pmset -g log`), not a
  code bug; `caffeinate -i -s` does not fully prevent recurrence (Power Nap/"Sleep Service"
  cycling bypasses it). Two changes as of 2026-07-16/17: (1) `sudo pmset -a powernap 0`
  targets the specific symptom (user to run/confirm — agent cannot run sudo); (2) an active
  watchdog (`monitoring/watchdog.py`) now auto-restarts within ~15-30 min regardless of
  whether the sleep mitigation fully holds, so this no longer requires a human to notice or
  manually restart. Full `disablesleep`/laptop migration remain optional further hardening,
  documented but not urgent (see `docs/RUNBOOK.md#sleep-wedges`).

## Failed attempts
(none this session — every fix attempt that failed a first pass was corrected same-turn, e.g. two wrong SUE quarter-bucketing hypotheses before the verified-correct one, documented in the SUE PIT backtest commit history rather than repeated here)
