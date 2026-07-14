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

  **Status (2026-07-14): DECIDED NOT TO PURSUE FURTHER — root cause is
  `KeepAlive` itself, not a missing approval.** `launchctl bootstrap`
  succeeds and the job reaches `state = spawn scheduled`, but the spawned
  process exits immediately with `last exit code = 78` every ~30s
  (`ThrottleInterval`), regardless of the System Settings → General →
  Login Items & Extensions → Allow in the Background toggle for `python3`
  being ON (confirmed both entries there were enabled). Isolated the exact
  cause with a throwaway test LaunchAgent (no `KeepAlive`, same
  `/opt/homebrew/bin/python3` binary, `python3 --version`) — it registered
  and ran instantly (exit 0). The dead-man's-switch LaunchAgent (below),
  which also has no `KeepAlive`, likewise runs fine. So this is specifically
  `KeepAlive` (a persistent, restart-forever daemon) being blocked — almost
  certainly because Homebrew's `python3` isn't code-signed/notarized, and
  macOS won't let an unsigned binary register as that class of background
  daemon at all, independent of the visible toggle. Not something a plist
  change or another Settings click fixes.
  A periodic-supervisor workaround (a `StartInterval` script, same pattern
  as the dead-man's switch, that checks if `run_bot.py` is alive and
  restarts it via `nohup` if not — sidesteps `KeepAlive` entirely) was
  proposed and explicitly declined by the user 2026-07-14: paper-trading,
  low stakes, the dead-man's switch already detects a stale bot, manual
  restart is an acceptable tradeoff. **The registration was unloaded
  (`launchctl bootout`) to stop the futile retry loop; the bot runs via the
  manual `nohup` path below (no auto-restart — restart it yourself if it
  dies).** Revisit only if this becomes a bigger pain point.
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

**Status (2026-07-14): active.** Unlike the main bot's LaunchAgent, this one
has no `KeepAlive` (it's a periodic `StartInterval` check, not a persistent
daemon), so it wasn't subject to the same macOS Background Task Management
block — registered and ran successfully on the first bootstrap (confirmed
via `dead_mans_switch.log`: real output, exit 0). Already loaded; runs every
4h plus once on load.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thomasvromen.tradingbot-deadmansswitch.plist  # if not already loaded
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
