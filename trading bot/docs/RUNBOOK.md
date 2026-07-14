# Runbook — Running the Bot Unattended

Quick reference for running the paper-trading bot for a month without expert
babysitting. See `CLAUDE.md` for full architecture; this doc is just the
operational how-to.

## Starting

```bash
cd "trading bot"
python run_bot.py              # live paper mode
```

Needs `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`/`ALPACA_BASE_URL` and either
`OPENAI_API_KEY` (default) or `ANTHROPIC_API_KEY` (`LLM_PROVIDER=anthropic`)
set — see `.env.example` for the full list, including the optional
`ALERT_WEBHOOK_URL`.

**Dry run first, no keys needed:**

```bash
python run_bot.py --simulated
```

**Verify alerting before walking away:**

```bash
python run_bot.py --test-alerts
```

**Keeping it running unattended (macOS):**

- **launchd (auto-restarts on crash — currently NOT active, see status below):**
  a plist lives at `~/Library/LaunchAgents/com.thomasvromen.tradingbot.plist`
  (outside the repo, in your home directory — not version-controlled).
  ```bash
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thomasvromen.tradingbot.plist  # start + enable auto-restart
  launchctl list | grep tradingbot                                                           # confirm it's running (shows a PID, not "-")
  launchctl bootout gui/$(id -u)/com.thomasvromen.tradingbot                                  # stop for real — killing the PID alone just gets restarted
  ```
  `KeepAlive` restarts the process on any non-zero/crash exit (with a 30s
  throttle so a crash-loop doesn't spin). Combined with the scheduler's
  catch-up-on-restart logic (`orchestration/main_loop.py::start()`), a crash
  mid-day no longer loses that day's remaining candidate-generation windows.
  Logs still go to `bot.log` (same file, same tailing workflow as below).

  **Status (2026-07-14): still not active — confirmed the exact cause.**
  `launchctl bootstrap` now succeeds (previously returned "Bad request");
  the job registers and shows `state = spawn scheduled`, but the spawned
  process still exits immediately with `last exit code = 78` and zero log
  output. Confirmed directly via
  `log show --predicate 'eventMessage contains "tradingbot"'`: macOS's
  `backgroundtaskmanagementd` recorded this exact LaunchAgent's identifier
  the moment it was bootstrapped, and `launchd` logs "service inactive"
  for it every ~30s (matching `ThrottleInterval`) — i.e. launchd keeps
  trying and macOS Background Task Management keeps blocking it. This is
  the same mechanism behind **System Settings → General → Login Items &
  Extensions → Allow in the Background** — approve `tradingbot` (and
  `tradingbot-deadmansswitch`, see below) there, then no further action is
  needed; both are already loaded and waiting. Manually re-checking
  `sfltool dumpbtm`'s per-user disposition needs an admin authorization
  prompt — did not do that without asking first. Until approved, the bot
  runs via the manual `nohup` path below (no auto-restart on crash —
  restart it yourself if it dies).
- `tmux` (manual alternative — lets you reattach and watch live output):
  ```bash
  tmux new -s bot
  python run_bot.py
  # detach: Ctrl-b d
  # reattach later: tmux attach -t bot
  ```
- Or `nohup` + `disown` (manual, no auto-restart on crash):
  ```bash
  nohup python run_bot.py > bot.log 2>&1 &
  disown
  ```

<a id="dead-mans-switch"></a>
## Dead-man's switch (added 2026-07-14)

The bot went completely dead for 3 days (2026-07-10 → 07-13, zero `job_runs`
rows, zero `dashboard.log` activity) with no alert at all — only a human
happened to check. Nothing running *inside* the bot process can ever detect
its own death, so `monitoring/dead_mans_switch.py` runs as a **separate**
process/LaunchAgent and fires the same webhook alert
(`monitoring/alerts.py`) if no `job_runs` row exists for the most recently
completed NYSE session.

```bash
python -m monitoring.dead_mans_switch    # one-off manual check, exit 0 = healthy
```

**Enable (once — separate approval from the main bot's LaunchAgent):**
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thomasvromen.tradingbot-deadmansswitch.plist
launchctl list | grep deadmansswitch   # confirm registered
```
Runs every 4h (`StartInterval`) plus once immediately on load. Same
Background Task Management approval gate as the main bot applies here too
(see the launchd status note above) — approve both in the same System
Settings visit. Logs to `dead_mans_switch.log` (separate from `bot.log`).

## Daily health check

Once a day, check:

1. **Dashboard:** `streamlit run dashboard/app.py` (reads `dashboard_state.json`).
   Confirm its timestamp is recent — it's updated by the scheduler jobs (see
   `CLAUDE.md`'s Scheduler section: 07:00 universe refresh, 13:00 prefetch,
   14:00 morning pipeline, 15:45/17:00/20:00 intraday checks, 16:00 exit
   review, 22:30 EOD). A stale timestamp means a job stopped running.
2. **Log file:** tail it and scan for `ERROR`/`WARNING` lines.
3. **`RISK_LOCKOUT` file:** confirm it has NOT appeared at the repo root
   (`trading bot/RISK_LOCKOUT`). If it has, see below.

## What each alert means

- **RISK_LOCKOUT** — max-drawdown circuit breaker (15%) tripped. Trading
  **halts and does not auto-resume**. Investigate the drawdown before doing
  anything. Only resume by manually deleting the `RISK_LOCKOUT` file once
  you're satisfied it's safe to keep trading.
- **Circuit breaker trips (daily reduce/halt/deleverage, weekly halt)** —
  informational; the bot self-manages exposure. No action needed unless it
  escalates to deleverage or the RISK_LOCKOUT above.
- **DEAD_FEED** — a data source (yfinance, Capitol Trades, or SEC EDGAR) is
  down. The congressional or insider signal may be degraded for that day's
  run. No action needed unless it persists across multiple days.
- **SLIPPAGE_HIGH** — a fill-poll timeout. Informational only.

## Safe restart

Never kill or restart the bot mid-session — the scheduler runs on a
single-thread executor, so killing mid-job can leave a half-completed
pipeline run. Before restarting:

1. Check the log for the last completed job (see the scheduler job list
   above) and make sure nothing is mid-run.
2. Restart with the same command you started it with. `initialize()` calls
   `reconcile_with_broker()` automatically on startup, which cleans up any
   ghost positions left by an unclean shutdown — so a restart between jobs
   is safe.

## Stopping for the month / moving to real cash

- **launchd (once active — see status note above):**
  `launchctl bootout gui/$(id -u)/com.thomasvromen.tradingbot` — unloading (not
  just killing the PID) is required, or `KeepAlive` restarts it.
- **tmux:** reattach (`tmux attach -t bot`) and Ctrl-C.
- **nohup:** `kill <PID>` (find it with `ps aux | grep run_bot.py`).

Before sizing any real capital, review the **live paper P&L track record**
from this run (`performance/tracker.py` / dashboard) — not the backtest
numbers in `docs/FACTOR_BACKTEST_2026-06-28.md`, which are a simplified,
survivorship-biased proxy, not a live performance guarantee.
