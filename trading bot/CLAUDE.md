# CLAUDE.md

> **⚠️ PHASE STATUS — READ FIRST** (current status only; full review/change history lives in
> `docs/CLAUDE-REFERENCE.md#history` — append new entries THERE, keep this banner short)
> **2026-07-20: watchdog/dead-man's-switch automated supervision is OFF, abandoned — do not
> re-attempt launchd or cron for these without new information.** See the dated entry at the
> end of this banner and `docs/RUNBOOK.md#after-a-reboot` for the full story. Both scripts
> are manual-only (`python -m monitoring.watchdog` / `monitoring.dead_mans_switch`); the main
> bot itself is unaffected and still needs to be checked manually, same as before.
> **2026-07-21: a missed scheduler job now fires the Slack alert webhook** (was log-only) —
> narrows this gap, does not close it: you get told, nothing auto-restarts.
> **2026-07-21: fixed a permanent per-ticker trailing-stop failure loop** (qty grows past what
> the old resting stop reserved -> new stop can never get enough "available" qty from Alpaca)
> and widened the initial-stop wash-trade retry (3x/~3s -> 5x/~20s, still firing on ~60% of new
> entries). See the dated entry at the end of this banner.
> Phase 0 gate: **BLOCKED ON DATA** — real point-in-time data not yet acquired; all historical
> performance numbers are look-ahead biased until then. See `docs/PHASE0_FINDINGS.md` for gate
> decision rules and required datasets.
> Phases 1–3 fully implemented; paper trading operational; live (paper-money) Alpaca launch
> started 2026-07-06. First live run (2026-07-06) hit and fixed a Critical bug in
> `_llm_call`'s OpenAI retry path. 2026-07-07: full review remediation (8 findings, incl.
> restart-safe circuit-breaker baselines + full-day insider feed) and new zero-LLM-cost
> signals (XBRL SUE/accruals/net-payout, short-interest screen). Later same day: first two
> "opened" trades (CF, VTRS) turned out to be **phantom positions** — `open_position` booked
> them from an unconfirmed (fill-poll-timeout) order status, but neither ever actually filled
> at Alpaca; a restart's reconcile caught them as ghosts. Root-caused to 4 bugs, all fixed:
> (1) `open_position` now requires `OrderStatus.FILLED` before booking, cancelling the
> dangling order otherwise; (2) `place_stop_order` always sent GTC, which Alpaca rejects for
> fractional qty; (3) SEC EDGAR daily-index fetch had no delay between consecutive misses,
> bursting into 403s; (4) the Capitol Trades JSON path treated 429 (rate limit) the same as a
> genuine 404 and gave up instead of backing off. See `docs/CLAUDE-REFERENCE.md#history` for
> detail; deferred edge ideas live in `docs/EDGE_BACKLOG.md`. 2026-07-09: the underlying
> cause of the CF/VTRS fill-poll timeout — `AlpacaBroker._poll_order_fill` only allowed
> ~0.4s (3 attempts × 0.2s) to confirm a fill — is fixed; widened to ~14s (15 × 1s). Later
> same day: entry-scan pipeline now runs twice daily (13:00/14:00 and 17:00/18:00 CEST)
> instead of once, per user request to react faster in volatile markets — position sizing
> multipliers unchanged. 2026-07-10: the in-memory scheduler was silently losing entire
> trading days across process restarts (zero signals generated 07-07 through 07-10) —
> fixed with a DB-backed catch-up-on-restart check, an unchecked initial-stop-placement
> gap, and a loosened AI entry hurdle (5x/1.5% → 3x/1.0%, now logged via
> `EntryScore.expected_return_pct`). **Two items attempted but NOT resolved, live-verified
> broken:** Russell 1000 universe coverage (iShares now serves bot-protection HTML on
> both its CSV endpoints regardless of headers — universe stays S&P-500-only; see
> `#data-caveats`) and macOS launchd auto-restart supervision (plist is correct but the
> spawned process exits immediately with code 78 on every attempt — likely needs manual
> Background Task Management approval in System Settings; bot runs via manual `nohup` for
> now, see `docs/RUNBOOK.md`). Later same day: found and fixed a test-DB-pollution bug —
> `tests/test_orchestrator.py` was writing fake signals into the live `trading.db` on every
> pytest run (unmocked `insert_fundamental_signal`); 287 fake rows deleted; real
> `fundamental_signals` history is just CF/VTRS (07-07) — no real candidate generated since,
> consistent with the scheduler bug above. Added an on-demand `weekly-factor-review` skill
> (report-only, never auto-edits weights) and `Settings.sizing.enable_cross_model_debate`
> (default off). First 14:00 CEST run post-scheduler-fix then exposed a Critical bug: all 4
> `open_position()` call sites in `main_loop.py` ignored its bool return, so 8/8 fill-timeouts
> logged/alerted as successful opens and could silently exhaust the daily entry quota —
> fixed, 4 regression tests. See `docs/CLAUDE-REFERENCE.md#history` for detail. Test count:
> **886** (full suite green; one pre-existing unrelated date-dependent test failure — history).
> 2026-07-13: bot found dead for ~3 days (missing-timeout hang, not the fd-leak fba2143 already
> fixed) — restarted; every reachable `yf.Ticker(...)` call and the Alpaca client now carry an
> explicit timeout (new `market_data/yf_session.py`) so a stalled network call can no longer
> wedge the single-thread scheduler forever. Also found, not fixed: `get_nav_baselines` NAV
> baseline collision on Mondays (see `docs/STATE.md#open-items`). Test count: **899** (full
> suite green; 2 known pre-existing unrelated failures — history).
> Later same session: root-caused *why* every fill attempt times out in the first place (not
> just why the process itself went silent) — `run_morning_pipeline` was cron-scheduled at
> 14:00 Amsterdam, 1.5h *before* NYSE's 15:30 CEST open, and the catch-up-on-restart trigger
> had no check for whether NYSE was *currently* open (only that today is a trading day),
> so tonight's 22:09 restart (9 min after the 22:00 close) placed 7 real buy orders into a
> closed market — all 7 timed out on fill confirmation, as every prior fill-timeout incident
> this month has. Fixed: new `_nyse_is_open_now()` intraday guard in `run_morning_pipeline`,
> first entry window moved 14:00→15:40 (10 min after open), catch-up threshold 14→16. Sweep
> found the same open_position-bool-ignored bug class on the **exit** side: `close_position`/
> `reduce_position`'s bool returns were ignored at all 4 exit call sites (hedge exit, AI exit,
> reduce, deleverage force-close) — a no-fill sell would still log "Closed"/"Force-closed"/
> mark take-profit-taken as if it succeeded. Fixed all 4, 6 new regression tests total (proven
> red/green). Test count: **903** (full suite green; same 2 pre-existing failures — history).
> 2026-07-14: both known pre-existing failures resolved. A concurrent session fixed the
> Monday NAV-baseline collision (`bot/db.py::get_nav_baselines`, commit 9a82022) — week_start_nav
> now prefers the last NAV strictly before week_start instead of "on/after", so it no longer
> collides with day_start_nav on Mondays. `test_db.py::test_insert_and_get_disclosure`'s
> hardcoded date (drifted outside `get_existing_ids()`'s 90-day window) made relative to
> `date.today()`. Added a dead-man's-switch (`monitoring/dead_mans_switch.py` + a second,
> separate LaunchAgent, `com.thomasvromen.tradingbot-deadmansswitch.plist`) — alerts if no
> `job_runs` row exists for the most recently completed NYSE session, since nothing inside the
> bot's own process can detect its own death (the exact 07-10→07-13 gap). Confirmed via
> `log show`/`launchctl` that macOS Background Task Management, not a plist bug, is what's
> blocking both LaunchAgents — needs approval in System Settings, see `docs/RUNBOOK.md`.
> Russell 1000 still blocked: no `FMP_API_KEY` yet; tried three more free/no-signup sources
> (FTSE Russell redirects, stockanalysis.com is a JS-rendered shell with no exposed API,
> SlickCharts 403s) — no viable alternative found, confirms the prior conclusion. Test count:
> **909** (full suite green, zero known failures).
> Later same day: dead-man's-switch confirmed **active** (ran successfully via launchd on
> first bootstrap). Main bot's launchd auto-restart investigation closed, not active: user
> enabled the System Settings toggle, but the block turned out to be `KeepAlive` itself
> (persistent restart-forever daemons), not the toggle — isolated with a throwaway
> no-`KeepAlive` test plist (same binary, ran fine). A `StartInterval`-based supervisor
> workaround was proposed and declined by the user — bot stays on manual `nohup`
> indefinitely; see `docs/RUNBOOK.md`'s launchd status note and `docs/STATE.md#open-items`.
> 2026-07-14 (independent verification session): full suite re-confirmed 909/909 green.
> Live-health check caught a real deploy gap: the running bot process pre-dated the
> NYSE-hours fix (commit b4938bb) by 37 minutes, so it was still on the pre-fix in-memory
> schedule and would have repeated the prior night's all-orders-timeout incident at 14:00
> CEST today. Restarted (user-approved) to load the fix; new process confirmed on the
> correct 15:40 schedule with the guard active. See `docs/CLAUDE-REFERENCE.md#history` for
> detail.
> Later same day: SUE PIT backtest complete (`docs/SUE_PIT_BACKTEST_2026-07-14.md`,
> `docs/EDGE_BACKLOG.md`) — the pre-committed gate failed on both horizons and on time-
> stability at 60d, so the SUE sub-weight stays at 0.15 (no code change). Two real bugs
> found and fixed while building it: `original_quarterly_eps`'s calendar-quarter bucketing
> (SEC buckets by nearest quarter-end boundary to a fact's `end` date, not end/start month;
> needed a collision-exclusion rule for 52/53-week retail fiscal calendars), and a history-
> truncation bug that starved the SUE seasonal-random-walk denominator and produced absurd
> outlier values. Honesty check (PIT vs. naive T+0 anchor) confirmed no residual look-ahead.
> 2026-07-14 (live dig-in session): user reported the bot "not trading" again; found two
> distinct bugs. (1) `_process_fundamental_candidate` persisted `fundamental_signals` rows
> unconditionally, before checking `open_position`'s return value — every candidate scored
> outside NYSE hours (the closed-market timing bug from earlier the same day) still landed
> a row with a real conviction score despite never filling, which is why days of "candidates"
> showed zero positions; also silently inflated `run_bot.py --backtest`'s signal set. Fixed:
> insert now gated on `opened`. (2) The live process (PID 51755, running the correct 15:40
> code) had its `BlockingScheduler` wedge — both worker and main threads idle, zero jobs
> dispatched for 2h+, no error logged (root cause not fully identified; `pmset`/`sample`
> ruled out system sleep and job hangs). Restarted (new PID 62191) — cleared it, and the
> catch-up path fired immediately, producing the bot's **first-ever real fundamental
> fills**: VICI and PFE. That surfaced a third, live bug: both stops were rejected by
> Alpaca's wash-trade check (40310000, "opposite side market/stop order exists" — its
> fill-state propagation lags our own poll confirmation), leaving both positions naked for
> ~2h until the next `enforce_stop_losses()` poll (20:00 CEST) placed them as designed.
> Fixed: `open_position`'s initial stop placement now retries 3x with backoff, mirroring
> the existing `_place_sell_with_retry` pattern (`enforce_stop_losses`' own trail-up call
> is a separate, untouched path). Both fixes proven via stash-and-rerun red/green. Test
> count: **942** (full suite green, zero known failures; count moved from 932 mid-session
> as a concurrent SUE-PIT-backtest session landed its own test files on disk).
> 2026-07-16/17: two more scheduler wedges (07-16 ~22:30→18:51 the next day, then again
> 07-16 20:00→07-17 10:44) despite the 07-15 `caffeinate -i -s` fix — `pmset -g log` showed
> Power Nap ("Sleep Service Back to Sleep") cycling active on battery the whole time,
> bypassing that assertion. User asked for a permanent fix after ~10 cumulative
> "not trading"/downtime incidents rather than another one-off patch. Research found the
> incidents are ~15 structurally distinct bug classes, not one recurring bug, so the fix
> targets bounded auto-recovery regardless of cause instead of chasing another single bug:
> `monitoring/watchdog.py`, a new 15-min `StartInterval` LaunchAgent that checks process
> liveness, per-job staleness (`job_runs` coverage extended from just
> `run_morning_pipeline` to all three core cron jobs), and deploy freshness (a new
> `bot_status.json` written at `initialize()`, closing the stale-running-process gap from
> 07-14/07-15), and auto-restarts on any of them — gated on 10 min of `bot.log` quiet so a
> legitimate long catch-up pipeline is never mistaken for a wedge. This reverses the
> 2026-07-14 decision to stay alert-only (`docs/STATE.md` Decisions/Constraints). Caught one
> real bug pre-ship: the orchestrator test fixtures would have had the real
> `write_status_file()` clobber the live bot's actual status file on every test run — mocked
> at all 4 construction sites. Live-verified end-to-end (not just unit-tested): the second
> 07-16/17 wedge was caught live during this work, the restart deployed the new code, and a
> forced watchdog cycle correctly read the fresh status file and reported
> `healthy:recent_activity`. See `docs/RUNBOOK.md#watchdog` and
> `docs/CLAUDE-REFERENCE.md#history` for detail. Test count: **975** (full suite green, zero
> known failures).
> 2026-07-17 (strategy/profitability review + remediation, concurrent with the above,
> deliberately independent of it): first full review of the bot's trading LOGIC since launch
> (not reliability/uptime) — see `docs/CLAUDE-REFERENCE.md#history` for full detail (the
> standalone review doc was retired once fully remediated, per convention). Found the real live
> entry hurdle was 4.5%, not the documented 1.0% floor (`_ESTIMATED_COST_PCT=1.5` in
> `main_loop.py` never updated after the 07-10 prompt-text loosening) — fixed to 0.4 (~1.2%
> floor). Ran the congressional signal against real cached data for the first time (prior
> analysis scripts only ever ran on synthetic fixtures): shows a significantly *negative*
> excess return at 1mo/3mo, not positive — left `_CONGRESSIONAL_MAX_PCT`/caps unchanged (repo
> convention: never silently auto-edit a signal weight; this is a report finding for a future
> session's explicit call, not applied). Confirmed `AllocationEngine`'s regime-based sizing is
> genuinely wired into all 3 signal-processing sites and live — ruled out a suspected critical
> "regime-awareness is dead code" bug. Fixed 3 more real bugs: unguarded `run_scraper()` in
> `run_morning_pipeline` could zero the entire day's primary (fundamental) signal on any
> scraper exception, same failure shape as several already-fixed reliability incidents, now
> wrapped in try/except with a `DEAD_FEED` alert; `regime/hmm_engine.py`'s `update_single`
> could silently classify off a stale feature row when only one column had a NaN (masked by
> `dropna()`), now raises instead (its only caller already has a graceful fallback to a fresh
> classification); `bot/portfolio.py`'s `open_position` didn't check `cancel_order`'s return
> value on a non-fill, so a failed cancel looked identical to a successful one — now alerts
> distinctly. Deleted confirmed-dead `bot/scheduler.py` (247 lines, zero non-test references)
> and its two now-redundant test files — `tests/test_integration.py`'s 3 tests exercised only
> the deprecated module and were fully superseded by `test_orchestrator.py`'s current coverage
> of `run_eod`/`run_morning_pipeline` (the original review's dead-code grep had excluded all
> test files, missing this dependency on first pass — caught and verified before deleting).
> Also: `screener/factor_scorer.py` truthy-zero fix (`fcf_yield`/`pe_inv`/`pb_inv`/
> `evebitda_inv` now use explicit `is not None` checks); wired the existing-but-never-invoked
> `performance/tracker.py`'s `PerformanceTracker` into `log_weekly_report()` so live-vs-backtest
> comparison actually runs weekly; expanded `test_run_bot.py` coverage for `main()`'s CLI
> dispatch and `run_paper()`'s call sequencing; two stale-doc fixes (`system/config.py`'s
> `target_portfolio_vol_pct` comment, `FACTOR_BACKTEST_2026-06-28.md`'s momentum-weight table).
> Deliberately not done: `main_loop.py`'s 4 near-duplicate signal-processing methods (a real
> refactor opportunity the review flagged, too large/risky to fold into a blanket fix pass).
> Work done via 7 parallel subagents plus some fixes applied directly; several subagents
> stalled waiting on a self-invented "background pytest monitor" that doesn't exist and had to
> be explicitly resumed — a pattern worth watching for in future fan-out dispatches. Commit
> `e9e0ee7`. Test count: **972** (full suite green, zero known failures; net −3 from the three
> deleted dead-code test files).
> 2026-07-17 (watchdog residual gaps, commits `d5d2526`/`d7db50b`/`ac70595`/`859b134`): a
> "run sudo pmset -a powernap 0" follow-up caught a live outage — the watchdog's first real
> auto-restart attempt (11:00:09) crashed on a bare `"python3"` string resolving to the wrong
> interpreter under the LaunchAgent's minimal PATH, leaving the bot down ~13 min until an
> "is it done?" check caught it. Fixed with `sys.executable`, proven via a real (unmocked)
> `restart_bot()` call against the live process (commit `95ec69f`). That prompted the user to
> ask "will it ever happen again without intervention?" — answered with 4 honest residual
> gaps rather than false certainty, then asked for and got a plan to close them: (1) watchdog
> moved from a per-login LaunchAgent to a `/Library/LaunchDaemons/` LaunchDaemon (`UserName`
> key) so it survives reboot/logout without a login session — chosen over auto-login, which
> `fdesetup status` confirmed FileVault disables outright; a truly cold boot from fully
> powered off still needs a human for FileVault's pre-boot password, unavoidably, left
> documented not solved; installed via the same `osascript ... with administrator privileges`
> pattern used for the powernap fix, live-verified when its `RunAtLoad` cycle immediately
> caught and correctly auto-restarted a real stale deploy end-to-end; (2) `main()` now alerts
> on any unhandled exception instead of dying silently, and the independent
> `dead_mans_switch.py` now also checks `watchdog.log` freshness so a bug IN the watchdog
> still pages a human; (3) a restart-history-backed circuit breaker suppresses auto-restart
> and alerts distinctly after 3+ restarts in 60 min, since a persistent code bug can't be
> fixed by retrying; (4) a 120-min hard ceiling now bypasses the quiet-gate, closing the
> "still logging but never finishing a real job" blind spot. Caught the same test-isolation
> bug class twice more mid-build (unmocked writes to the real `watchdog_restart_history.json`
> in 3 existing tests; 2 existing `check_and_recover` tests whose bare mock would have
> collided with the new dual-grace call pattern) — both flagged in the plan's self-review
> before implementation. See `docs/RUNBOOK.md#watchdog` and `docs/STATE.md` Decisions for the
> full honest framing of what's closed vs. still open. Test count: **985** (full suite green,
> zero known failures).
> 2026-07-17 (second live outage, repo-wide close-out for a planned reboot, commits
> `a9a7313`/`79f9b0b`/`c0ad57a`): a follow-up "update everything, confirm the reboot will
> work" pass caught a second real watchdog bug live — `nohup` needs a controlling terminal to
> detach from, which a genuine `LaunchDaemon` invocation has none of; it failed outright
> (`can't detach from console`) before ever launching python, silently through two full
> restart cycles. Fixed by dropping `nohup` in favor of `start_new_session=True` (already did
> the same job); verified via the very next natural cycle launching cleanly in production, not
> just a unit test. The resulting process then hit transient DB-lock/too-many-open-files/DNS
> errors from the day's restart churn — confirmed resolved within minutes, expected to clear
> fully on reboot regardless. `monitoring/dead_mans_switch.py` also converted to a
> LaunchDaemon (was still a LaunchAgent — an inconsistency that would have left the
> watchdog's own backstop unable to survive reboot). Caught the same test-isolation bug class
> a third time (unmocked `_recent_restart_count` reading real production data). Repo-wide doc
> sweep: corrected a stale `ALERT_WEBHOOK_URL` claim in `docs/guardrails/PROJECT.md`, added
> `docs/RUNBOOK.md#after-a-reboot`, documented the 4 new alert types. Test count: **985**
> (unchanged — fixes and docs, not new features).
> 2026-07-17 (short-selling capability): real short-selling support added behind
> `Settings.strategy.enable_short_selling` (default `False`, zero live behavior change).
> Five recorded open questions must be revisited before ever turning it on: regime-aware
> short sizing, hedge-mechanism overlap, aggregate exposure cap, short borrow fees not
> modeled in the AI's cost hurdle, and `SimulatedBroker` cannot execute a short order at
> all (short-selling is Alpaca-only). See
> `docs/superpowers/specs/2026-07-17-short-selling-design.md` for the full design and these
> questions. Test count: **1045** (full suite green, zero known failures).
> 2026-07-20 (watchdog/dead-man's-switch supervision closed out, not fixed): live-health
> check found both LaunchDaemons (and their gui-domain LaunchAgent counterparts, an
> undocumented leftover duplicate from the 07-17 migration that was never cleaned up) had
> been failing every attempt since the 07-17 16:29 reboot — exit 78 (`EX_CONFIG`), zero bytes
> ever written to their log files. Live-reproduced via `launchctl kickstart`: confirmed
> launchd-layer spawn failure, not a script bug (same command run manually, or with a fully
> stripped launchd-like environment, exits 0 clean both times). Ruled out binary corruption
> (python3 unchanged since April) and a stale registration (`bootout`+`bootstrap` reload
> didn't help). Root cause: `sfltool dumpbtm` reports the items as "enabled, allowed,
> notified," but actual enforcement disagrees — Background Task Management's approval cache
> desyncs from reality after a reboot for unsigned/adhoc-signed launch items, and the only
> known fix is the System Settings -> Login Items toggle (off/on) or `sudo sfltool resetbtm`
> (system-wide blast radius, resets every app's approvals, not just ours). User declined both.
> Built and live-verified a `cron`-based fallback instead (cron's own daemon is Apple-signed,
> pre-approved, and sidesteps the gate entirely) — proved twice, including a real write into
> the TCC-protected `~/Documents/.../trading bot/` folder with zero prompt. **Then reverted at
> the user's explicit request**: this same problem (launchd, then cron proposed as the
> workaround) has now been hit or attempted across at least three sessions
> (2026-07-14/2026-07-17/2026-07-20) without a durable fix, and the user chose to stop paying
> the cost of re-litigating it. Final state: both gui-domain LaunchAgents unloaded, crontab
> cleared back to empty, `/Library/LaunchDaemons/` copies left on disk but inert (harmless,
> needs `sudo rm` to fully clean up, not urgent). Decision, not a bug to re-open: both scripts
> are manual-only going forward, run on demand exactly like the main bot's own accepted
> manual-`nohup` pattern. **Do not re-propose launchd or cron automation for these two without
> materially new information** — see `docs/RUNBOOK.md#after-a-reboot` for the full
> investigation detail. Main bot process itself unaffected throughout, confirmed healthy and
> on current commit.
> 2026-07-20 (holistic branch review + remediation): first whole-diff review of the completed
> short-selling branch found and fixed 5 issues (sector-cap netting masked short exposure,
> short candidates never persisted a `fundamental_signals` row, a misleading reconcile alert,
> `RiskManager`'s per-position cap not short-aware, and the never-live-verified NAV
> sign-convention assumption converted to a runtime self-check) — none flag-off-behavior-
> changing. See `docs/CLAUDE-REFERENCE.md#history` for detail. Test count: **1055** (full
> suite green, zero known failures).
> 2026-07-20/21: live P&L reviewed at user's request — no closed trades yet, unrealized
> +$445.22 on $37.2k deployed; bot ≈-2.75% vs. SPY -0.75% since 07-07 inception (too early to
> call an edge). Found `max_positions` (20/20) was binding while only ~39% of NAV was
> deployed — raised to 30, and `per_trade_risk_pct` 0.15→0.20, plus fixed an independent bug
> where the backtest simulator's own `max_positions` default was decoupled from live config.
> Also wired missed-scheduler-job alerting (see banner top). See `docs/CLAUDE-REFERENCE.md
> #history` for full detail. Test count: **1057** (full suite green, zero known failures).
> 2026-07-21 (two ORDER_REJECTED bugs found and fixed, live-reproduced): a batch of Slack
> alerts from the prior evening's trailing-stop poll led to finding (A) a permanent per-ticker
> stop-update failure once a position's live qty grows past what its still-resting old stop
> reserved, and (B) initial-stop wash-trade rejections still occurring despite the existing
> 3x retry. Both fixed; see `docs/CLAUDE-REFERENCE.md#history` for full detail. Test count:
> **1058** (full suite green, zero known failures).

**Purpose:** a regime-aware, paper-only systematic equity trading bot. It combines a fundamental factor screener (primary signal), congressional-disclosure trades (supplementary signal), an HMM market-regime overlay, and an independent risk manager. Built as research/paper-trading for a finance thesis. **Live (real-money) order execution is intentionally disabled — paper and simulated only.**

## Stack at a glance

- **Python** (3.11+; uses `from __future__ import annotations`, `zoneinfo`, `datetime.UTC`).
- **No web framework** for the bot itself; a separate **Streamlit** dashboard (`dashboard/app.py`) reads a JSON state file.
- **Data:** `yfinance` (prices, fundamentals, VIX), `requests`/`beautifulsoup4` (Capitol Trades scraper, universe lists).
- **Broker:** `alpaca-py` paper API (`bot/broker.py`) or a fully offline `SimulatedBroker` (`execution/paper_broker.py`).
- **AI:** OpenAI (`gpt-5.4`) is the default provider for entry/exit/technical scoring (`bot/ai_analyst.py`); switch back to Anthropic Claude (`claude-sonnet-4-6`, with prompt caching) via `Settings.llm_provider = "anthropic"` (env: `LLM_PROVIDER=anthropic`). OpenAI is also used separately for news sentiment in `bot/researcher.py` (`gpt-4o-mini`, unrelated to this switch).
- **Regime model:** pure-NumPy Gaussian HMM (`regime/gaussian_hmm.py`) + `scikit-learn` `StandardScaler`, persisted with `joblib`. No `hmmlearn`.
- **Scheduling:** `APScheduler` (`BlockingScheduler`) with an `exchange_calendars` NYSE guard, Amsterdam timezone.
- **Persistence:** SQLite (`trading.db`), WAL mode, versioned migrations.
- Dependencies in `requirements.txt`. Keep this shape; do not add a frontend framework, ORM, or build tooling, and do not add a headless browser or `statsmodels`/`hmmlearn` without flagging it first.

## Running

**For the live unattended bot specifically** (starting it for real, restarting it, checking
if it's healthy, anything after a reboot): stop here and read `docs/RUNBOOK.md` instead —
it's the operational guide, kept current, and covers the watchdog/dead-man's-switch
LaunchDaemons that manage the bot's uptime. The commands below are for running the code
directly (dev/debugging/backtests), not how the bot is actually kept running day to day.

```bash
pip install -r requirements.txt

python run_bot.py              # live paper mode (Alpaca paper API; needs ALPACA_* keys)
python run_bot.py --simulated  # fully offline SimulatedBroker (no broker keys needed)
python run_bot.py --backtest   # walk-forward backtest from DB signals, then exit
python run_bot.py --test-alerts # fire one test alert via the configured sender, then exit

python run_1year_backtest.py   # focused congressional-only backtest off cached JSON (see caveats)
python run_backtest_nokey.py   # backtest variant requiring no API keys

streamlit run dashboard/app.py # dashboard (reads dashboard_state.json)
```

First run creates `trading.db` (`bot.db.init_db`) and fetches the universe. The HMM model is cached to `regime_model.joblib` and reloaded on restart.

Secrets come from environment / `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `PROPUBLICA_API_KEY`, optional `ALERT_WEBHOOK_URL`, `DB_PATH`, `LOG_LEVEL`, `LLM_PROVIDER` (`openai` default, or `anthropic`). `OPENAI_API_KEY` is required by default now (entry/exit/technical scoring); `ANTHROPIC_API_KEY` is only required if `LLM_PROVIDER=anthropic`. `--simulated` mode runs without broker/LLM keys for the parts that don't call out.

## Verifying changes

```bash
pytest                                 # keep green (run from inside trading bot/) -- current count in this file's banner above, don't hardcode it here, it drifts every session
python backtesting/backtest_price_factors.py  # PIT backtest of low-vol/BAB + residual momentum
pytest tests/test_simulation.py -q    # example: a single module
```

- Tests must run **offline** — mock yfinance / Alpaca / scraper / LLM calls (see `tests/conftest.py`). New code needs offline unit tests.
- Set `temperature=0` on any LLM call you add or touch (reproducibility).

## Reference (on-demand — Read the anchor before touching the named area)

Before changing orchestration, signal flow, regime/risk/portfolio/config/DB semantics, or any subsystem wiring -> Read docs/CLAUDE-REFERENCE.md#architecture
Before editing stops, sizing, the technical gate, the paper-only guard, or the dashboard paths -> Read docs/CLAUDE-REFERENCE.md#gotchas
Before touching scraper, committee, universe, yfinance paths, or interpreting backtest attribution -> Read docs/CLAUDE-REFERENCE.md#data-caveats
Before changing scheduled jobs or their times -> Read docs/CLAUDE-REFERENCE.md#scheduler
Looking for the analysis docs (Phase 0 gate, data sources, backtests, hedge, congressional edge) -> Read docs/CLAUDE-REFERENCE.md#key-documents
Starting, restarting, or checking on the live unattended bot (including after a reboot), or touching `monitoring/watchdog.py` / `monitoring/dead_mans_switch.py` -> Read docs/RUNBOOK.md first — it is the operational source of truth, this file is architecture only. Start at docs/RUNBOOK.md#after-a-reboot if you're picking this up after the Mac restarted.
After completing a review/remediation/strategy change worth recording -> append it to docs/CLAUDE-REFERENCE.md#history and update this banner's status line

## Security & data

- API keys live in environment / `.env` only — never commit `.env`, `trading.db`, `regime_model.joblib`, the cached JSON/shelve data, `dashboard_state.json`, `bot_status.json`, or `watchdog_restart_history.json` (all gitignored — the last two are runtime state written by `monitoring/status_file.py` and `monitoring/watchdog.py`, added 2026-07-17). Never log secrets.
