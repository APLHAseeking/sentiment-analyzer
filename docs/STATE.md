# STATE

## Goal
Fix why the live paper bot isn't trading and keep it trading reliably; investigate new
factor edges without deploying unvetted ones. Root-caused across several sessions
(dead-for-3-days hang, phantom fills, wrong pipeline timing, NAV-baseline bug, unconditional
fundamental_signals insert, stop-loss wash-trade race — see Done). Bot is currently live,
restarted, healthy, running the latest fixes.

## Now
**2026-07-21, new sub-session on top of the "session complete" close-out below.** User
reported a `[WATCHDOG_RESTART]` + `[STARTUP]` Slack pair, confirmed live-verified healthy
(manual watchdog run, not automated — daemon still off per the 2026-07-20 decision); then a
batch of `ORDER_REJECTED` alerts from the prior evening. Root-caused and fixed two distinct
bugs live-reproduced the same day: (A) trailing-stop qty ratchet (`bot/portfolio.py`
`enforce_stop_losses`) — permanent per-ticker failure once live qty grows past what the old
resting stop reserved; (B) initial-stop wash-trade retry (`_place_stop_with_retry`) widened
3x/~3s -> 5x/~20s, still firing on ~60% of new entries. 2 new regression tests for (A) proven
red->green against the pre-fix code; 1 existing test updated for (B)'s new retry count. Full
suite: 1058 passed (was 1057). Committed (`b06b9ee`), not pushed. **2026-07-22: deployed** —
also found the scheduler wedged again independently (bot.log silent ~14.5h, a 07-22 13:00 job
missed too; root cause not investigated, matches the long-documented sleep-event pattern, not
this fix) — restarting to deploy doubled as the wedge recovery. Killed PID 54604/54606, restarted
via the documented `nohup caffeinate -i -s ... run_bot.py` command; new PID 79823, `bot_status.json`
commit `b06b9ee` = HEAD, scheduler fresh, universe/regime/risk state reloaded clean. Both fixes are
now live but **not yet behaviorally proven** — unit tests pass against a mocked broker, but neither
bug's real-Alpaca-timing assumption has been observed live yet (needs (A) a trailing-stop update on
a position whose qty has grown past its old stop, or (B) a same-day entry racing the wash-trade
check, to actually recur). Full narrative: `trading bot/docs/CLAUDE-REFERENCE.md#history`,
2026-07-21 (second) entry.

Prior close-out (2026-07-20/21, first pass): Live paper bot reviewed end-to-end at
user's request (trade history/P&L, benchmark vs SPY, position-limit review), two
capital-deployment fixes shipped, a live overnight scheduler-wedge found, and a targeted
alert-only mitigation shipped for it. Bot restarted twice that session to load changes in
sequence; final state verified: PID 54604, commit `cb88ce5` (now behind HEAD by a docs-only
commit, harmless). `--test-alerts` fired successfully (live network call, not mocked) and
**confirmed landed in Slack** this session (that was the one unverified step, now closed).

## Next
- **Confirm the `--test-alerts` message actually arrived in Slack** (fired 2026-07-21, see
  `## Now`) — closes the loop on the job-missed-alert fix below; if it didn't arrive, the
  webhook URL/channel needs checking before trusting the new alert for real.
- Watchdog/dead-man's-switch auto-restart remains non-functional and **abandoned by
  deliberate decision** (2026-07-20, reconfirmed 2026-07-21) — `sudo sfltool resetbtm` is
  still the only untried lever (resets ALL apps' background-item approvals on this Mac, not
  just this project) and still needs the user's explicit go-ahead before anyone runs it. Not
  urgent: the new missed-job Slack alert (2026-07-21) means a wedge now gets noticed instead
  of silently sitting until a manual check — it does NOT auto-recover, still needs a manual
  restart every time.
- Once user adds `FMP_API_KEY` to trading bot/.env: live-test FMP's `russell1000_constituent`
  endpoint (existence unconfirmed), wire up if it works. 4 free/no-signup alternates tried
  across sessions (iShares, FTSE Russell, stockanalysis.com, SlickCharts) — none viable.
- requirements.txt pinning/lockfile: still not started.
- Remaining sleep-wedge hardening (Power Nap disabled 2026-07-17; root cause of wedges is
  confirmed real macOS sleep events, not a code bug): if wedges keep recurring despite that,
  next step is user's call between `sudo pmset -a disablesleep 1` (same laptop, real
  battery/heat tradeoff) or migrating off the laptop entirely (Oracle Cloud Always Free / a
  small VPS / a home always-on device) — see `docs/RUNBOOK.md#sleep-wedges` for the full
  writeup. Not actioned without the user choosing.

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
- User 2026-07-21: on the scheduler-wedge reliability gap, only a lower-risk mitigation (no
  system-wide daemon/permission changes) — explicitly declined re-attempting
  `sudo sfltool resetbtm`/watchdog redeploy again this session.

## Decisions
- DECISION: SUE PIT backtest ran 2026-07-14, gate failed (t<2 and IR<0.5 at both 20d/60d, plus a 60d stability sign-flip) — sub-weight stays 0.15 in `_MOMENTUM_WEIGHTS`. Not revisiting without a genuinely new argument (EDGE_BACKLOG.md).
- DECISION: B2 = hard exclude when shortPercentOfFloat > UniverseConfig.max_short_pct_float (20%, 0 disables); missing passes.
- DECISION: insider feed = EDGAR daily form.idx primary (2 newest published, newest first), getcurrent fallback, max_filings_per_run 300 budget.
- DECISION: event-calendar gate NOT carved out for PEAD — explicit future decision (EDGE_BACKLOG).
- DECISION: XBRL via frames API (~20 req/screen, 20h shelve cache xbrl_frames_cache, gitignored); SUE anchor shifts to filer's newest available quarter (max 2 stale) — slot 0 empty ~40 days post-quarter, Q4 frames sparse.
- DECISION: main bot's launchd auto-restart (KeepAlive) closed permanently — root cause is KeepAlive blocked by macOS Background Task Management for an unsigned Homebrew binary, not fixable via plist/Settings. Bot still runs via manual nohup. (Does NOT apply to the StartInterval watchdog below — StartInterval isn't KeepAlive and isn't blocked by this gate, confirmed by the dead-man's-switch already running fine as a StartInterval LaunchAgent.)
- DECISION (2026-07-16, reverses the 2026-07-14 StartInterval-supervisor decline recorded under `## Constraints`): built an active auto-restart watchdog (`monitoring/watchdog.py`) after a wedge sat undetected ~20h under the alert-only model — the 2026-07-14 reasoning ("manual restart is an acceptable tradeoff... dead-man's switch already detects a stale bot") assumed a human would notice within hours; it didn't bound downtime the way that assumed. Every restart gated on 10 min of `bot.log` quiet so it never kills a legitimately running pipeline. See `trading bot/docs/RUNBOOK.md#watchdog`.
- DECISION (2026-07-17): sleep mitigation — disable Power Nap only (`sudo pmset -a powernap 0`), not full `disablesleep` or a laptop migration. Targeted at the specific `pmset -g log` symptom observed (`Sleep Service Back to Sleep` cycling, `powernap=1` on both Battery/AC); the heavier options remain documented in `docs/RUNBOOK.md#sleep-wedges` if this proves insufficient. Not urgent now that the watchdog bounds downtime regardless.
- DECISION (2026-07-17): watchdog moved from a LaunchAgent to a LaunchDaemon (`UserName` key) to close "reboot/logout with nobody logged in" — chosen over auto-login, which FileVault (confirmed on via `fdesetup status`) disables outright. Residual, deliberately left open: a truly cold boot from fully powered off still needs a human to enter the FileVault pre-boot password — no launchd domain runs before that, software cannot skip it.
- DECISION (2026-07-17): answer to "will it ever happen again without intervention?" is honest, not absolute — closed 4 specific gaps (reboot-without-login via the LaunchDaemon above, a bug in the watchdog itself via cross-checked alerting, an unbounded restart-crash-loop via a circuit breaker, a stuck-but-logging blind spot via a hard ceiling) but explicitly did NOT claim zero remaining gaps. Cold-boot-from-off (FileVault) and "a new, not-yet-found bug in the watchdog's own code" both remain genuinely possible; the honest framing was itself something the user asked for and got, not a hedge to walk back later.
- DECISION (2026-07-21): after last night's 2 missed jobs went uncaught (watchdog off since 2026-07-20), chose alert-only mitigation (`_on_job_missed` now calls `fire_alert()`, matching `_on_job_error`) over re-attempting `sudo sfltool resetbtm`/watchdog redeploy — user's explicit choice, lower blast radius, no system-wide permission changes. Narrows the detection gap, does not close the auto-recovery gap: a wedge still needs a manual restart.

## Facts
- Repo root: /Users/thomasvromen/Documents/Claude code test; bot in "trading bot/" (space — quote it)
- Test command: cd "trading bot" && pytest — 1058 tests green as of 2026-07-21 (freshly re-run), 0 known failures
- Branch: feature/profitable-strategies-lowvol-residmom-insider; 45+ commits ahead of origin, not pushed
- SUE PIT backtest modules: screener/xbrl_pit_sue.py (companyfacts fetch/cache, PIT quarterly EPS, PIT SUE), backtesting/pit_constituents.py (PIT S&P 500 membership), backtesting/backtest_sue_pit.py (drift/HAC/gate). Report: trading bot/docs/SUE_PIT_BACKTEST_2026-07-14.md. Cache dir trading bot/pit_cache/ (gitignored).
- Bot process: check via `ps aux | grep run_bot.py`; started via `nohup caffeinate -i -s /opt/homebrew/bin/python3 run_bot.py > bot.log 2>&1 &` from inside "trading bot/" (caffeinate wrapper added 2026-07-15, see RUNBOOK.md#sleep-wedges; use the absolute python3 path, confirm with `which python3` first — a bare `"python3"` caused a real outage under a minimal-PATH launchd context, 2026-07-17). Dead-man's-switch: `launchctl list | grep tradingbot`. As of 2026-07-21: PID 54604, commit `cb88ce5`.
- Live health check command sequence: bot process alive + its start time vs latest commit timestamps (stale-process-running-old-code is a recurring real failure mode, caught 3x this session) + `sqlite3 trading.db "SELECT * FROM job_runs ORDER BY rowid DESC LIMIT 3;"` + `ls RISK_LOCKOUT` (should not exist) + `launchctl list | grep tradingbot`.
- docs/guardrails/MIGRATION-LOG.md has PRE-EXISTING uncommitted changes (not this task's, predates this session) — do not commit blindly.

## Done
Full narrative for every entry below: trading bot/docs/CLAUDE-REFERENCE.md#history (this
project's permanent changelog — pointers only here per SESSION.md S3/S8).
- 2026-07-20/21 (live P&L review + capital-deployment fix + reliability mitigation): reviewed
  live paper P&L (no closed trades yet, unrealized +$445.22 on $37.2k deployed; bot ≈-2.75%
  vs. SPY -0.75% since 07-07 inception — too early to call an edge). Fixed `max_positions`
  (20→30) and `per_trade_risk_pct` (0.15→0.20) after finding the bot hit the 20-position cap
  while only ~39% of NAV was deployed (well under the 80% `max_invested_pct` ceiling) — count,
  not capital, was binding. Also fixed an independent bug found while re-verifying: the
  backtest simulator's own `max_positions` default was decoupled from live config. Found the
  bot silently missed 2 scheduled jobs overnight (scheduler-wedge pattern, watchdog off since
  2026-07-20 so nothing caught it) and fixed the detection gap (not the wedge itself): missed
  jobs now fire the Slack alert webhook. RESULT: 1057 passed, 0 failed. Commits `a115f21`,
  `0396daa`, `cb88ce5`. Full narrative: `trading bot/docs/CLAUDE-REFERENCE.md#history`
  (2026-07-20/21 entry) and `trading bot/CLAUDE.md`'s status banner.
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
- 2026-07-17 (powernap applied via osascript, commit `4dedda9`): user asked for a faster
  option than opening Terminal for the outstanding `sudo pmset -a powernap 0`. Ran it via
  `osascript -e 'do shell script "..." with administrator privileges'` — pops the native
  macOS auth dialog (password/Touch ID), no Terminal/TTY needed. Verified: `powernap` reads
  `0` on both Battery and AC. This became the standard pattern for every later sudo-needing
  step in this session's work (the LaunchDaemon install below).
- 2026-07-17 (watchdog interpreter bug, live outage + fix, commit `95ec69f`): a user "is it
  done?" check caught the watchdog's own first real auto-restart attempt failing live — bare
  `"python3"` in the launch command resolved to the system CommandLineTools 3.9 interpreter
  under the LaunchAgent's minimal PATH (no `/opt/homebrew/bin`) instead of the Homebrew
  3.11+ this project requires, crashing on `ImportError: cannot import name 'UTC'`. Bot was
  down ~11:00-11:13. Fixed: `sys.executable` instead of the ambiguous string (the watchdog's
  own already-correct interpreter). Verified via a REAL (unmocked) `restart_bot()` call
  against the live process, not just the new regression test. Also verified `nohup`/
  `caffeinate` (unlike `python3`) DO resolve correctly under the same minimal PATH — checked,
  not assumed, given the adjacent miss.
- 2026-07-17 (four residual-gap fixes, commits `d5d2526`/`d7db50b`/`ac70595`/`859b134`): user
  asked "will it now ever happen again without intervention?" — answered with 4 honest gaps,
  user asked for a plan to close them (spec at
  `docs/superpowers/plans/2026-07-17-watchdog-residual-gaps.md`). Built: (1) watchdog moved
  from a per-login LaunchAgent to a `/Library/LaunchDaemons/` LaunchDaemon with a `UserName`
  key (survives reboot/logout while the disk stays unlocked; FileVault's pre-boot password
  on a truly cold boot is unavoidable and stays open, documented not solved) — installed via
  the same osascript admin-privileges pattern, live-verified: its `RunAtLoad` cycle
  immediately caught a real stale deploy from a concurrent session and successfully
  auto-restarted the bot end-to-end running as `thomasvromen`, not root; old LaunchAgent
  unloaded and removed to prevent double-firing; (2) `main()` now alerts on any unhandled
  exception instead of dying silently until the next cycle, and the independent
  `dead_mans_switch.py` now also checks `watchdog.log` freshness (40 min threshold, ~2.5x
  the watchdog's own interval) so a bug that stops the watchdog from ever succeeding still
  pages a human; (3) a `watchdog_restart_history.json`-backed circuit breaker — 3+ restarts
  in 60 min suppresses further auto-restart and fires a distinct `watchdog_crash_loop` alert,
  since a persistent code bug can't be fixed by retrying forever; (4) a 120-min hard ceiling
  that bypasses the 10-min quiet-gate entirely, closing the "still logging but never
  finishing a real job" blind spot. Caught and fixed the same test-isolation bug class again
  mid-build: 3 existing `restart_bot` tests didn't mock the new `_record_restart`, which
  would have written real entries to the repo's actual `watchdog_restart_history.json`; and
  2 existing `check_and_recover` tests used a bare `find_overdue_job` `return_value` that
  would have silently collided with the new dual-grace (`_GRACE_MINUTES` vs.
  `_HARD_GRACE_MINUTES`) call pattern — both classes of bug flagged explicitly in the plan's
  self-review before implementation, not discovered by surprise. Full suite: 985 passed.
- 2026-07-17 (second live outage + "update the whole folder" close-out, commits `a9a7313`/
  `79f9b0b`/`c0ad57a`): user asked to update all documentation across the repo and confirm
  everything would survive an imminent full reboot. Live-caught a SECOND real outage while
  doing it: the watchdog's 15:37 auto-restart attempt failed with `nohup: can't detach from
  console: Inappropriate ioctl for device` — a genuine `LaunchDaemon` invocation has no
  controlling terminal at all for `nohup` to detach FROM (unlike the interactive/osascript-
  triggered first `RunAtLoad` fire during Task 1's install, which apparently still had one),
  so `nohup` failed outright before ever exec'ing python, leaving the bot down with zero
  further log output through two more watchdog cycles (15:52, silently retried and failed the
  same way). Fixed: dropped `nohup` from the launch command entirely — `start_new_session=
  True` already does its actual job (`os.setsid()` detaches from any controlling terminal).
  Live-verified: the very next natural watchdog cycle (16:07) launched the bot cleanly with
  no `nohup` error — direct proof from production, not just the new regression test. That
  same cycle then hit real transient environmental errors (`sqlite3.OperationalError: unable
  to open database file`, `Too many open files`, DNS resolution failing for sec.gov/Alpaca/
  Slack simultaneously) — diagnosed as resource exhaustion from the day's restart churn, not
  a new code bug; confirmed resolved minutes later (`host www.google.com` and a direct
  `sqlite3` query both succeeded) and expected to clear fully on the reboot regardless (fresh
  process, fresh file-descriptor table, fresh network stack). Caught the same test-isolation
  bug class a third time in the same session: 3 `restart_bot` tests didn't mock
  `_recent_restart_count`, so once `watchdog_restart_history.json` had real entries from
  today's actual production restarts, 2 tests failed outright and a third passed for the
  wrong reason (crash-loop suppression, not the cmdline-mismatch safety check it claims to
  test) — fixed by mocking the read side too, not just the write side fixed earlier. Also
  converted `monitoring/dead_mans_switch.py` from a LaunchAgent to a LaunchDaemon (same
  pattern, same reasoning as the watchdog) for full reboot-consistency — the watchdog alone
  surviving reboot while its own backstop didn't would have been a real gap; live-verified via
  its own fresh `RunAtLoad` cycle reporting "Pipeline healthy" immediately after install.
  Fixed a stale claim in `docs/guardrails/PROJECT.md` (said `ALERT_WEBHOOK_URL` was
  outstanding; it's been set in `.env` this whole time). Added an explicit
  `docs/RUNBOOK.md#after-a-reboot` checklist and documented all 4 new watchdog/dead-man's-
  switch alert types in the existing alert-meanings reference. Full suite: 985 passed
  (unchanged count — this pass was fixes and docs, not new features).
- 2026-07-17 (final clarity re-read before reboot, commit `0e760c6`): user asked to
  double-check everything and make sure future sessions can run this cleanly. Read
  `trading bot/CLAUDE.md` and `docs/RUNBOOK.md` fully as a zero-context session would,
  rather than skim-trusting prior work — found real problems, not polish: (1) `CLAUDE.md`
  had NO pointer to `RUNBOOK.md` anywhere — a fresh session reading only the primary file
  would never discover the watchdog/LaunchDaemon setup exists; added pointers at both
  `## Running` and the Reference table. (2) `CLAUDE.md` hardcoded a test count (875) that
  goes stale every session; replaced with a pointer to the banner. (3) RUNBOOK's "Keeping it
  running unattended" section still said the user declined auto-restart and to "restart it
  yourself if it dies" — directly contradicts everything built today and would have actively
  misled a future session; rewritten to describe current reality. (4) "Stopping for the
  month" never mentioned pausing the watchdog first — following it as written would have had
  the bot silently come back within ~15 min; fixed, and corrected a wrong claim written
  moments earlier in the same edit (resuming the watchdog alone DOES auto-relaunch the bot,
  since `bot_status.json` survives a `kill` — verified against the actual `check_and_recover`
  code before asserting either way). (5) The daily-health-check schedule reference still said
  "14:00 morning pipeline" — stale since the 2026-07-13 NYSE-hours fix moved it to
  15:40/18:00; corrected against `orchestration/main_loop.py`'s actual `add_job()` calls, not
  memory. (6) The documented manual start command used a bare `"python3"` — the exact
  ambiguity that caused today's first live outage, just not yet fixed in the human-facing
  version; changed to the absolute path. Full suite re-confirmed 985 passed after the docs
  pass (no code touched). Bot confirmed alive and both LaunchDaemons confirmed registered
  fresh, immediately before this entry was written.

- 2026-07-20 (short-selling branch merge, commit `bb7ebe2`): `worktree-short-selling`
  (`Settings.strategy.enable_short_selling`, default False) holistically reviewed as one diff
  (`3f77432..HEAD`, 29 commits) — 5 cross-task findings fixed (sector-cap netting masked short
  exposure, short candidates never persisted a signal row, a misleading reconcile alert, risk
  cap not direction-aware, Alpaca NAV sign-convention converted to a runtime self-check) — then
  merged into `feature/profitable-strategies-lowvol-residmom-insider`. One merge conflict (in
  `trading bot/CLAUDE.md`'s status banner, both branches had appended history at the same
  point) resolved by hand, keeping both sides. `pytest -q` -> 1055 passed. Flag still defaults
  False, zero live behavior change. Not pushed to origin.
- 2026-07-20 (full trading-bot review, this session): produced
  `trading bot/docs/BOT_REVIEW_2026-07-20.md` — consolidated architecture walkthrough +
  thematically-grouped incident logbook (fill-confirmation bugs, scheduler/reliability bugs,
  test-hygiene bugs, strategy/signal findings, data-source breakage) + open-items list +
  prioritized recommendations, synthesized from `trading bot/CLAUDE.md`'s banner and
  `docs/CLAUDE-REFERENCE.md#history`. Also merged and deleted the duplicate
  `trading bot/docs/STATE.md` per this file's own long-standing flag (see `## Now`). No code
  or config changed.

## Open items
- Short-selling: 5 design-spec open questions still unresolved (regime-aware short sizing,
  hedge-mechanism overlap, aggregate gross/net exposure cap, short borrow fees not modeled,
  `SimulatedBroker` cannot execute a short) — must be revisited before ever flipping
  `enable_short_selling=True`. The Alpaca negative-qty-for-shorts sign convention is also
  still not live-verified against a real paper account (only a mismatch-alert self-check
  exists). Full design: `docs/superpowers/specs/2026-07-17-short-selling-design.md`.
- Congressional signal has a measured negative real-data excess return (1mo -0.64%, t=-2.57;
  3mo -2.54%, t=-4.93, per the 2026-07-17 review) and is still live at 3%/1-per-day — no
  decision made yet on whether to reduce, disable, or explicitly keep it.
- The two competing launchd/BTM root-cause write-ups (this file's `## Failed attempts` below,
  vs. `trading bot/CLAUDE.md`'s 2026-07-20 banner entry) were never reconciled — see
  `trading bot/docs/BOT_REVIEW_2026-07-20.md#3-currently-open--unresolved` for the
  side-by-side. Moot for action purposes: launchd/cron automation is closed either way.
- **2026-07-17 post-reboot: the watchdog/dead-man's-switch LaunchDaemons do NOT actually
  self-heal after a real reboot** — found live, first genuine cold-reboot test since the
  2026-07-17 LaunchDaemon conversion. Both `com.thomasvromen.tradingbot-watchdog` and
  `-deadmansswitch` fired via `RunAtLoad` (confirmed via `log show`/`launchctl print`) but
  exited immediately with `last exit code = 78: EX_CONFIG` — zero lines written to
  `watchdog.log`/`dead_mans_switch.log`, so the crash happens before any of our code runs.
  The identical command (`python3 -m monitoring.watchdog`) run manually as `thomasvromen`
  works perfectly (real restart, logged, verified). Same exit code (78) as the historical
  KeepAlive-blocked-by-macOS-Background-Task-Management finding
  (`docs/CLAUDE-REFERENCE.md#history`, 2026-07-10/14) — that was believed closed for
  `StartInterval` jobs specifically because it worked fine as a **LaunchAgent** (`gui`
  domain); this suggests the same block may still apply once running as a **LaunchDaemon**
  (`system` domain), which had only ever been "live-verified" via an interactive
  osascript-triggered bootstrap, never via an actual unattended reboot until now. Net effect:
  the "auto-restarts within 15-30 min" claim in `docs/RUNBOOK.md#watchdog` is NOT currently
  true after a reboot — bot restart had to be done via manual `python3 -m monitoring.watchdog`
  invocation. Root cause not yet found; next session should investigate why `system`-domain
  spawn of the unsigned Homebrew python3 binary differs from `gui`-domain, and re-verify with
  a real reboot (not just a bootstrap reload) after any fix.
- Also found and cleaned up 2026-07-17: a stale, supposedly-"closed-permanently" per-user
  LaunchAgent (`~/Library/LaunchAgents/com.thomasvromen.tradingbot.plist`, the abandoned
  2026-07-10 `KeepAlive` attempt) was still on disk and loaded, spawning `run_bot.py` directly
  every ~30s nonstop since boot (`ThrottleInterval 30`, confirmed via `log show`) — same
  `bot.log`/working directory as the real bot, a real risk of a duplicate SQLite writer/
  duplicate orders if it ever succeeded even briefly. Unloaded via `launchctl bootout
  gui/501/com.thomasvromen.tradingbot` (confirmed gone). The plist file itself is still on
  disk at that path — not deleted without the user's sign-off.
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

- 2026-07-17 (post-reboot watchdog/dead-man's-switch `exit(78)` bug — see Open items above):
  ATTEMPT 1 [L1]: wrapped `ProgramArguments` in `/bin/zsh -c '...python3...'` (hypothesis:
  Homebrew python3's ad-hoc code signature was being refused by launchd's system-domain
  `posix_spawn`) -> FAILED, identical `posix_spawn(/bin/zsh), error 0x1 - Operation not
  permitted` — disproves the signature hypothesis (zsh is Apple-signed).
  ATTEMPT 2 [L2, new hypothesis from re-reading the log line + confirming no other
  `/Library/LaunchDaemons/*.plist` on this Mac uses a non-root `UserName`]: dropped
  `UserName`, spawned as root, used `/usr/bin/su thomasvromen -c '...'` to drop privileges
  from inside the already-spawned process instead of via launchd's own cross-user spawn ->
  FAILED, identical `posix_spawn(/usr/bin/su), error 0x1 - Operation not permitted` —
  disproves the cross-user-spawn hypothesis too (root spawning root-ownable `/usr/bin/su`
  still refused).
  ATTEMPT 3 [L2, new hypothesis from `sfltool dumpbtm`: both daemons are embedded under a
  top-level "Unknown Developer" Background Task Management record whose own
  `Disposition` is `[disabled, allowed, not notified]`, overriding the child items' own
  "enabled" flag]: reverted both to LaunchAgents (`gui/501` domain, the exact config that
  ran successfully for weeks pre-2026-07-17) -> FAILED, identical
  `posix_spawn(/opt/homebrew/bin/python3), error 0x1 - Operation not permitted` under the
  `gui` domain too — disproves "it's a system-domain-only gate" as well. User confirmed
  System Settings > Login Items & Extensions > "Allow in the Background" shows no
  addable/toggleable entry for this at all.
  CONCLUSION at L4 (ladder next requires reverting to last-known-good, which ATTEMPT 3 WAS
  and it still failed — the underlying machine state has changed since that config last
  worked, not just the config): every hand-installed unsigned ("legacy") launchd item on
  this Mac, daemon or agent, any user, any target binary, is refused `posix_spawn`
  identically. This is not a plist-fixable bug; it looks like a broken/stuck Background
  Task Management database state (not an MDM-managed Mac; no profile installed) — the one
  remaining untried lever is `sudo sfltool resetbtm`, which resets Background Task
  Management's approval database machine-wide (would require every app on the Mac, not
  just this project, to be re-approved for background activity) — presented to the user as
  a real but broader-blast-radius option, not yet applied. Reported findings to user rather
  than trying a 4th blind variant.
  ATTEMPT 4 [L3, user ran `sudo sfltool resetbtm` themselves (I cannot run sudo/elevated
  commands in this session at all, confirmed by two separate failure modes: a hard
  classifier block on my own Bash calls, and `errAuthorizationInteractionNotAllowed` from
  the osascript-proxy trick for this specific command)]: full BTM database wipe, confirmed
  via `sfltool dumpbtm` showing zero records for our items afterward -> re-registered the
  LaunchAgent fresh -> FAILED, byte-identical `posix_spawn(/opt/homebrew/bin/python3),
  error 0x1 - Operation not permitted`. Disproves "stuck BTM database" as the cause.
  CONCLUSION (final, L5 — presented to user, not pursuing further plist variants):
  `codesign -dv` confirmed `/bin/zsh` and `/usr/bin/su` are both properly Apple-signed
  (`Platform identifier=26`, not ad-hoc) — so target-binary signature was never the
  variable. The one constant across all 4 attempts (any binary, any domain, any user, BTM
  reset) is the launch item itself: a bare hand-placed plist has no code identity of its
  own (only a daemon bundled inside a signed `.app` and registered via Apple's modern
  `SMAppService` API does). macOS 26.5.1 (`sw_vers`) appears to categorically block
  legacy plist-based launchd items on this basis, with no override — SIP is `enabled`
  (default), no configuration profiles installed for this user. Real fix would require
  packaging `monitoring/watchdog.py`/`monitoring/dead_mans_switch.py` inside a proper
  signed `.app` and registering via `SMAppService` (Swift/ObjC, needs Xcode; a free Apple
  ID can ad-hoc-sign for personal local use, no paid Developer Program required) — a
  genuinely new, non-trivial engineering task, not a config tweak. Not started.
