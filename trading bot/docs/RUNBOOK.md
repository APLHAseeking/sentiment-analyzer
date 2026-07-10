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

  **Status (2026-07-10): not currently active.** The plist is correct — the
  exact `ProgramArguments`/`WorkingDirectory` command runs fine when invoked
  directly — but every `launchctl bootstrap` attempt registers the job and it
  then exits immediately with code 78 (`EX_CONFIG`), with zero process output
  (`launchctl print gui/$(id -u)/com.thomasvromen.tradingbot` shows `state =
  spawn scheduled`, `last exit code = 78`). This almost certainly means macOS
  is blocking the newly-registered LaunchAgent pending approval — check
  **System Settings → General → Login Items & Extensions** for a pending item
  to approve, then retry `launchctl bootstrap`. Until resolved, the bot runs
  via the manual `nohup` path below (no auto-restart on crash — restart it
  yourself if it dies, same as before this session).
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
