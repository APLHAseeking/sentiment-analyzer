# STATE

## Goal
Fix why the live paper bot isn't trading and keep it trading reliably; investigate new
factor edges without deploying unvetted ones. Bot is live, restarted, healthy.

## Now
2026-07-23: bot live and healthy, PID 21508, commit `3590db2` (includes the LVS stop-cancel
fix, `9701bb3`). Deployed via safe restart at 18:26 CEST; last job before restart completed
cleanly, nothing mid-run. Not yet behaviorally proven against a real order rejection — needs
a natural close/reduce event on a position with a resting stop (see `## Next`).

## Next
- Live-verify the LVS/`close_position` stop-cancel fix (commit `9701bb3`) against a real
  recurrence — next `run_exit_review` (16:00 CEST) or any stop-triggered close. LVS itself
  still has an untouched resting stop; nothing has retried it since the fix landed.
- Read `trading bot/bot_threaddump.log` the next time a scheduler wedge is reported
  (overwritten every 5 min, shows what every thread was doing when it stalled) — don't just
  restart without reading it first.
- Scheduler wedge root cause still partially open: `disablesleep` (2026-07-23) should remove
  sleep as a cause going forward; if wedges keep recurring with it on, the remaining ~14h
  portion of the 07-22/23 gap is still unexplained.
- Watchdog/dead-man's-switch auto-restart: abandoned by deliberate decision (2026-07-20/21) —
  manual-only going forward, don't re-propose launchd/cron without materially new information.
- BTM re-approval checklist (human-only, from the 2026-07-23 accidental `sfltool resetbtm`):
  System Settings → Login Items & Extensions — Google Updater/Keystone, Microsoft
  OneDrive/Office/Teams/Defender, Zoom updater, Steam clean, XQuartz. Bot's own 3 plists
  already known-inert.
- `tests/test_heartbeat.py` flaky under concurrent full-suite load only (3/3 pass isolated) —
  real-time polling under CPU contention, not a heartbeat.py bug; next attempt should make it
  event-based instead of widening the timeout again.
- Congressional signal: measured negative real-data excess return (1mo -0.64% t=-2.57, 3mo
  -2.54% t=-4.93) but still scraped daily for DB logging only (trading use disabled
  2026-07-22) — no decision yet on whether to reduce/disable the scrape-only path itself.
- requirements.txt pinning/lockfile: still not started.
- Short-selling: 5/5 design-spec prerequisite code fixes done; flag stays off
  (`enable_short_selling=False`) — Alpaca sign-convention still not live-verified against a
  real paper account before ever enabling.

## Constraints
- Global: never git push unless asked; commit per meaningful unit.
- Repo: tests offline; temperature=0 on LLM calls; no new deps/frameworks without flagging;
  keep test suite green; log changes in docs/CLAUDE-REFERENCE.md#history.
- User 2026-07-14: SUE PIT backtest formula/weight untouched — recommendation only, honored.
- User 2026-07-21: scheduler-wedge mitigation stays lower-risk only (no system-wide
  daemon/permission changes) this thread.

## Decisions
- DECISION: SUE sub-weight stays 0.15 (2026-07-14 PIT gate failed); Phase 0 composite-factor
  gate also FAILS (2026-07-23 full PIT backtest, t=-1.75/IR=-0.78) — no code change to
  `screener/factor_scorer.py` from either result.
- DECISION: launchd/cron auto-restart supervision closed permanently (2026-07-20/21) — macOS
  Background Task Management blocks unsigned legacy launchd items categorically (see
  `## Failed attempts`); bot and its two helper scripts are manual-only (`nohup`/on-demand).
- DECISION: congressional signal disabled for trading decisions (2026-07-22,
  `Settings.congressional.enabled=False`) — negative real-data excess return; scraping
  continues for DB logging only.
- DECISION: Russell 1000 closed as accepted S&P-500-only scope (2026-07-23) — no viable free
  data source found across 3 sessions.

## Facts
- Repo root: /Users/thomasvromen/Documents/Claude code test; bot in "trading bot/" (space —
  quote it).
- Test command: `cd "trading bot" && pytest` — 1142+ tests green as of 2026-07-23.
- Branch: feature/profitable-strategies-lowvol-residmom-insider; 136+ commits ahead of
  origin, not pushed.
- Bot process: `ps aux | grep run_bot.py`; restart via `nohup caffeinate -i -s
  /opt/homebrew/bin/python3 run_bot.py > bot.log 2>&1 & disown` from inside "trading bot/"
  (see `docs/RUNBOOK.md#safe-restart`). As of 2026-07-23 18:26 CEST: PID 21508, commit
  `3590db2`.
- Live health check: process alive + start time vs latest commit + `sqlite3 trading.db
  "SELECT * FROM job_runs ORDER BY rowid DESC LIMIT 3;"` + `ls RISK_LOCKOUT` (should not exist).
- docs/guardrails/MIGRATION-LOG.md has pre-existing uncommitted changes, not this project's —
  don't commit blindly.

## Done
Full narrative for every entry: `trading bot/docs/CLAUDE-REFERENCE.md#history` (permanent
changelog — pointers only here per SESSION.md S8).
- 2026-07-23: fixed `close_position`/`reduce_position` not cancelling a resting stop before
  selling (live LVS ORDER_REJECTED) — commit `9701bb3`, 101/101 `test_portfolio.py` green,
  deployed (PID 21508).
- 2026-07-23: Phase 0 PIT backtest complete, all 6 review items closed — gate FAILS
  (t=-1.75, IR=-0.78), no code change. `docs/PHASE0_BACKTEST_2026-07-23.md`.
- 2026-07-22/23: short-selling prerequisite fixes (5/5), congressional signal disabled,
  Russell 1000 closed, LLM model/provider attribution added. Full suite 1142 passed.
- 2026-07-21/22: two ORDER_REJECTED bugs fixed (trailing-stop qty ratchet, wash-trade retry
  widened); missed-job Slack alerting added.
- 2026-07-20/21: live P&L review, `max_positions` 20→30, capital-deployment fix.
- 2026-07-17: reliability watchdog built then closed out (BTM blocks all legacy launchd
  items, see `## Failed attempts`); full strategy/profitability review + remediation.
- 2026-07-06 through 2026-07-16: Phase 1-3 build-out, launch-week fixes (phantom fills,
  NYSE-hours guard, NAV-baseline bug, sleep-induced wedges, dead-man's-switch), SUE PIT
  backtest.

## Open items
- Short-selling: flag stays off until the Alpaca sign-convention is live-verified against a
  real paper account (only a mismatch-alert self-check exists today). Full design:
  `docs/superpowers/specs/2026-07-17-short-selling-design.md`.
- eps_trend daily snapshot / insider routine-buyer filter — both recorded in
  `EDGE_BACKLOG.md`, not built.
- docs/guardrails/MIGRATION-LOG.md pre-existing uncommitted drift — not this project's, not
  touched.

## Failed attempts
- 2026-07-17 (launchd/watchdog auto-restart, 4 attempts, L5 — closed, not pursuing
  further): every hand-installed unsigned launchd item (any domain, any user, post-BTM-reset)
  hit an identical `posix_spawn ... error 0x1 - Operation not permitted`. Root cause: macOS
  26.5.1 categorically blocks legacy plist-based launchd items with no code identity — only
  an `.app`-bundled, `SMAppService`-registered daemon would work (real fix, not a config
  tweak, not started). Full ledger: `trading bot/docs/CLAUDE-REFERENCE.md#history` (2026-07-17
  entries).
