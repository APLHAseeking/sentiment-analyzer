# STATE

## Goal
Fix why the live paper bot isn't trading and keep it trading reliably; investigate new
factor edges without deploying unvetted ones. Bot is live, restarted, healthy.

## Now
2026-07-24 session closed (both sessions this date, folded together). Bot live and healthy,
PID 7179, commit `a06f5fb`. Reboot recovery, the naked-stop-restore fix, and the pytest
alert-leak fix are all done and committed (`5772877`) — see `## Done`. Nothing in flight.

## Next
- Live-verify the naked-stop-restore fix (commit `a06f5fb`, built on `9701bb3`) against a
  real recurrence — nothing has retried it since it landed.
- `docs/BOT_REVIEW_2026-07-20.md` follow-up — status re-checked 2026-07-24, 3/6 closed, see
  `## Open items` for the 3 still open/in-progress.
- Read `trading bot/bot_threaddump.log` the next time a scheduler wedge is reported
  (overwritten every 5 min) — don't just restart without reading it first.
- Scheduler wedge root cause partially open: `disablesleep` (2026-07-23) should remove sleep
  as a cause going forward; if wedges recur with it on, the remaining ~14h portion of the
  07-22/23 gap is still unexplained.
- BTM re-approval checklist (human-only, from the 2026-07-23 accidental `sfltool resetbtm`):
  System Settings → Login Items & Extensions — Google Updater/Keystone, Microsoft
  OneDrive/Office/Teams/Defender, Zoom updater, Steam clean, XQuartz. Bot's own 3 plists
  already known-inert.
- `tests/test_heartbeat.py` flaky under concurrent full-suite load only (3/3 pass isolated) —
  next attempt should make it event-based instead of widening the timeout again.
- requirements.txt pinning/lockfile: still not started.

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
- DECISION: real-time Slack-alert-to-Claude-Code integration (so a "rejected order" alert
  gets auto-evaluated against live bot state) explored and dropped, user's call, 2026-07-24 —
  the `RemoteTrigger` routine tool only supports cron (min 1h) or one-time triggers, no
  inbound-webhook trigger type; and cloud routines can't see the local Mac at all
  (`trading.db`/`bot.log`/`ps aux` don't exist in a cloud git checkout) regardless. The only
  real local option (`CronCreate` polling inside this Claude Code session) is session-bound
  (dies on exit, 7-day cap) — not worth the tradeoff since the actual leak causing false
  alerts is now fixed. Don't re-propose without a materially different mechanism.

## Facts
- Repo root: /Users/thomasvromen/Documents/Claude code test; bot in "trading bot/" (space —
  quote it).
- Test command: `cd "trading bot" && pytest` — 1142+ tests green as of 2026-07-23.
- Branch: feature/profitable-strategies-lowvol-residmom-insider; 136+ commits ahead of
  origin, not pushed.
- Bot process: `ps aux | grep run_bot.py`; restart via `nohup caffeinate -i -s
  /opt/homebrew/bin/python3 run_bot.py > bot.log 2>&1 & disown` from inside "trading bot/"
  (see `docs/RUNBOOK.md#safe-restart`). As of 2026-07-24 10:29 CEST: PID 7179, commit
  `a06f5fb`. A reboot kills this process outright (no watchdog) — check `ps aux`/
  `bot_status.json` first thing after any Mac restart.
- Live health check: process alive + start time vs latest commit + `sqlite3 trading.db
  "SELECT * FROM job_runs ORDER BY rowid DESC LIMIT 3;"` + `ls RISK_LOCKOUT` (should not exist).
- docs/guardrails/MIGRATION-LOG.md has pre-existing uncommitted changes, not this project's —
  don't commit blindly.

## Done
Full narrative for every entry: `trading bot/docs/CLAUDE-REFERENCE.md#history` (permanent
changelog — pointers only here per SESSION.md S8).
- 2026-07-24: fixed `pytest` leaking real Slack alerts (`tests/conftest.py` missing
  `ALERT_WEBHOOK_URL` stub) — 1180 tests green, no bot code changed.
- 2026-07-24: post-reboot recovery (bot down ~8h, restarted PID 7179/`a06f5fb`, no data
  loss) + `docs/BOT_REVIEW_2026-07-20.md` status re-check (read-only, no code change) — of
  its 6 recommendations, items 1-3 (congressional decision, Phase 0 gate, Russell 1000)
  confirmed closed; items 4/6 confirmed still open, item 5 confirmed in-progress
  (insufficient data). Detail in `## Open items`.
- 2026-07-23: naked-stop-restore fix (commit `a06f5fb`, builds on `9701bb3`), 1169 tests
  green; Phase 0 PIT backtest complete — gate FAILS (t=-1.75, IR=-0.78), no code change
  (`docs/PHASE0_BACKTEST_2026-07-23.md`); short-selling prerequisite fixes (5/5); Russell
  1000 closed; LLM model/provider attribution added.
- 2026-07-20/22: full bot review (`docs/BOT_REVIEW_2026-07-20.md`, 6 recommendations — see
  `## Open items` for current status) + live P&L review, `max_positions` 20→30,
  capital-deployment fix; two ORDER_REJECTED bugs fixed; missed-job Slack alerting added;
  congressional signal disabled for trading.
- 2026-07-06 through 2026-07-17: Phase 1-3 build-out + launch-week fixes (phantom fills,
  NYSE-hours guard, NAV-baseline bug, sleep-induced wedges, dead-man's-switch, SUE PIT
  backtest, reliability watchdog built then closed out — BTM blocks legacy launchd, see
  `## Failed attempts`). Full narrative: `trading bot/docs/CLAUDE-REFERENCE.md#history`.

## Open items
- `docs/BOT_REVIEW_2026-07-20.md` rec #4: short-selling flag stays off until the Alpaca
  negative-qty sign convention is live-verified against a real paper account (only a
  mismatch-alert self-check exists today). Full design:
  `docs/superpowers/specs/2026-07-17-short-selling-design.md`.
- `docs/BOT_REVIEW_2026-07-20.md` rec #5: monitor books at `per_trade_risk_pct=0.20`/
  `max_positions=30` for 1-2 weeks before any further loosening — re-checked 2026-07-24,
  only 4 days of `portfolio_log` data (NAV flat, +0.29%), zero closed trades and zero
  `risk_events` in the window (no evidence either way yet), and position count is already
  saturated at 30/30 — same binding-cap pattern that triggered the 20→30 raise. Revisit once
  more `portfolio_log` rows/closed trades accumulate.
- `docs/BOT_REVIEW_2026-07-20.md` rec #6: no supervisor restarts the bot after a reboot —
  flagged as "worth an explicit decision," never decided; 2026-07-24 turned it from
  hypothetical to real (bot down ~8h after the overnight reboot, manual restart required).
  Needs an explicit call: accept as a standing manual-check habit, or revisit supervision
  (last attempt closed 2026-07-20/21, see Decisions/Failed attempts — don't re-propose
  launchd/cron without materially new information).
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
