# Runbook — Running the Bot Unattended

Quick reference for running the paper-trading bot for a month without expert
babysitting. See `CLAUDE.md` for full architecture; this doc is just the
operational how-to.

<a id="after-a-reboot"></a>
## After a reboot (read this first if you just restarted the Mac)

As of 2026-07-17, both `monitoring/watchdog.py` and
`monitoring/dead_mans_switch.py` run as **LaunchDaemons**
(`/Library/LaunchDaemons/`, not per-login `LaunchAgents`) — they start at
boot automatically, no login required, no manual re-install needed after a
normal restart. **What does NOT auto-start: the bot itself
(`run_bot.py`).** It's a plain `nohup`'d process, not a LaunchDaemon/Agent —
a reboot kills it and nothing brings it back until the watchdog's first
cycle notices. That's expected, not a bug: `RunAtLoad` means the watchdog
fires within seconds of boot, finds `bot_status.json`'s PID no longer
alive, and launches the bot itself via its normal `restart_bot()` path —
same mechanism as every other auto-recovery, just triggered by "no process"
instead of "stale/wedged process."

**Verify it worked** (run these once, any time after the reboot):
```bash
cd "trading bot"
launchctl print system/com.thomasvromen.tradingbot-watchdog | head -6       # state, active count
launchctl print system/com.thomasvromen.tradingbot-deadmansswitch | head -6
tail -5 watchdog.log            # expect a fresh cycle within ~15 min of boot
ps aux | grep '[r]un_bot.py'    # expect 2 lines (python + caffeinate) once the watchdog has acted
cat bot_status.json             # pid should match ps output above; commit should match: git rev-parse HEAD
pmset -g custom | grep powernap # expect 0 on both Battery and AC -- pmset settings DO persist across reboot, this is just a sanity check
```
If `ps aux` shows nothing and it's been >15 min since boot: something's
wrong with the watchdog itself, not just a normal startup delay — check
`watchdog.log` for a crash, or run `python -m monitoring.watchdog` manually
from `trading bot/` to see the error directly.

**What a reboot does NOT fix on its own:** if you rebooted specifically
because of a cold boot from fully powered off (not a restart/logout),
FileVault's pre-boot password screen requires you to authenticate before
macOS boots at all — nothing above starts until after that, unavoidably.
See [`#watchdog`](#watchdog) for why.

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

**Keeping it running unattended (macOS) — current state as of 2026-07-17:**

The bot itself auto-restarts via `monitoring/watchdog.py`, a separate
LaunchDaemon that checks every 15 minutes and relaunches the bot on crash,
wedge, or stale deploy — see [`#watchdog`](#watchdog) below for the full
mechanism. **You do not need to babysit this manually day to day.** The
sections below are for the initial manual start (the watchdog only
restarts an already-started bot — see "Starting for the first time" just
below) and for understanding what's actually happening if you want to dig
in.

**Starting for the first time** (or after intentionally stopping it —
`## Safe restart` below covers routine restarts):
```bash
cd "trading bot"
nohup caffeinate -i -s /opt/homebrew/bin/python3 run_bot.py > bot.log 2>&1 &
disown
```
Use the absolute `/opt/homebrew/bin/python3` path, not the bare `python3`
— confirm it's still correct first with `which python3` if the Homebrew
install ever moves. (This isn't paranoia: a bare `"python3"` string inside
the watchdog's own restart command resolved to the wrong interpreter under
a LaunchAgent's minimal PATH and caused a real live outage 2026-07-17,
`docs/CLAUDE-REFERENCE.md#history` — same ambiguity exists for a human
running this by hand if their shell's PATH ever differs.) Once started,
`write_status_file()` writes `bot_status.json` on the bot's own startup,
and the watchdog takes over from there — no `launchd`/`KeepAlive`/`tmux`
setup needed for the bot process itself.

**Manual alternatives, if you want to run it in the foreground / watch
live output** instead of relying on the watchdog:
- `tmux` (lets you reattach later):
  ```bash
  tmux new -s bot
  python run_bot.py
  # detach: Ctrl-b d
  # reattach later: tmux attach -t bot
  ```
  If you do this, the watchdog will leave a `tmux`-run process alone as
  long as it's alive and its PID matches `bot_status.json` — but a `tmux`
  session dies with the terminal/SSH connection unless you explicitly
  detach first, unlike the `nohup`+`caffeinate` form above.

**History — why this isn't a `launchd`/`KeepAlive` LaunchAgent for the
bot process itself:** tried 2026-07-10, closed 2026-07-14. `KeepAlive`
(a persistent, restart-forever daemon class) is blocked by macOS
Background Task Management for Homebrew's unsigned `python3` binary,
confirmed via a throwaway no-`KeepAlive` test plist (same binary) running
fine while the `KeepAlive` version exited immediately every ~30s
(`last exit code = 78`). This is specifically a `KeepAlive` limitation —
it does **not** apply to `StartInterval`-based LaunchAgents/Daemons (short
periodic jobs, not persistent daemons), which is exactly what
`monitoring/watchdog.py` and `monitoring/dead_mans_switch.py` are, and why
they work fine unattended despite this history. A `StartInterval`
supervisor workaround was proposed at the time and declined ("manual
restart is an acceptable tradeoff") — that decision was **reversed
2026-07-16** after a wedge sat undetected ~20 hours; the watchdog above is
that workaround, finally built.

<a id="sleep-wedges"></a>
## System sleep silently stops the bot (found 2026-07-15)

**Symptom:** the bot process stays alive (`ps` shows it running) but the
scheduler dispatches zero jobs for hours — no `job_runs` row, no log output
past startup, no error. Looks identical to a hung scheduler; happened 3
times in 3 days (2026-07-14 twice, 2026-07-15 once) before the actual cause
was found.

**Root cause, not a code bug:** this MacBook Air going to sleep
(`pmset -g log`, grep for `Sleep`/`Wake`) suspends the whole `nohup`'d
process along with it. All 3 wedges directly correlate with a real
`Entering Sleep state` log line within minutes of the wedge starting —
`Idle Sleep` (inactivity, both on battery and AC) in every case, plus one
`Clamshell Sleep` (lid closed). A process `sample` taken while wedged shows
both the scheduler's main thread and its executor thread genuinely parked
(empty queue, timed wait) — not stuck inside a job, not crashed, just never
woken back up correctly after the machine slept. `BlockingScheduler`'s own
misfire-detection (logs `Scheduler job missed: ...`) never fired either,
consistent with the whole process — not just one job — losing wall-clock
continuity.

**Fix in place now:** `caffeinate -i -s` wraps every `run_bot.py` launch
(command above), tied to the bot's own lifetime — if the bot exits, the
sleep-prevention assertion releases automatically. Verify it's holding:
```bash
pmset -g assertions | grep -A1 "caffeinate.*python3"
```
`-i` prevents idle sleep, `-s` prevents system sleep while on AC power —
covers every `Idle Sleep`/`Maintenance Sleep` event seen so far.

**Known gap — does NOT fully cover Power Nap / clamshell sleep.**
2026-07-16: the bot wedged again despite the caffeinate assertion being
active the whole time. `pmset -g log` showed the laptop repeatedly cycling
`Sleep`/`DarkWake` ("Sleep Service Back to Sleep") on battery — classic
Power Nap behavior (`pmset -g custom` showed `powernap 1` on both Battery
and AC Power), which is a separate sleep mechanism from the idle/system
sleep `caffeinate -i -s` actually covers. **Mitigation applied 2026-07-17**
(`sudo pmset -a powernap 0`, targeted, no meaningful battery/heat cost —
see escalation options below for the heavier alternatives): run via
`osascript -e 'do shell script "pmset -a powernap 0" with administrator
privileges'`, which pops the native macOS auth dialog (password/Touch ID)
instead of needing a Terminal + interactive `sudo` — faster than the
`! sudo ...` / Terminal route documented previously. Verified via
`pmset -g custom | grep powernap` — both lines now read `0`. Verified via
research, not assumption: closing the lid without an external display
attached triggers sleep at the hardware lid-sensor level separately, and
no `caffeinate` assertion overrides that either — Apple's own
clamshell-mode support requires an external display, keyboard/mouse, and
power connected simultaneously. If the laptop's lid gets closed while the
bot is running, it can still wedge.

**Belt-and-suspenders as of 2026-07-16: even if a sleep-driven wedge
recurs, it no longer requires a human to notice.** See
[`#watchdog`](#watchdog) below — an active auto-restart watchdog now
bounds any wedge (whatever its cause) to roughly 15-30 minutes instead of
however long it takes someone to check. The escalation options below
remain worth doing to *minimize* how often restarts happen (a restart
loses whatever window was in flight), but they're no longer the only
thing standing between a sleep event and multi-hour downtime.

**If lid-closed sleep recurs, next steps in order of increasing commitment:**
1. **`sudo pmset -a disablesleep 1`** — fully disables all sleep, including
   clamshell, until reversed with `sudo pmset -a disablesleep 0`. Verified
   via research to be the standard trick for running a Mac lid-closed with
   no external display. Needs a one-time interactive `sudo` (not something
   an agent can run unattended — do it yourself, or via `! sudo pmset -a
   disablesleep 1` in a Claude Code session). **Real tradeoff:** the CPU
   never suspends, so on battery with the lid closed it drains meaningfully
   faster than normal sleep and the machine runs hot in a closed bag. Only
   sensible if the laptop stays on AC power while running unattended.
2. **The actual fix: stop relying on a personal laptop for a 24/7
   unattended process.** Even with `disablesleep`, a laptop that gets
   carried around, closed, taken off Wi-Fi, put through OS updates, or
   simply shut for the night is fundamentally the wrong host for this. Real
   options, cheapest first (verified 2026-07-15, terms can drift — recheck
   before committing):
   - **Oracle Cloud "Always Free" tier** — genuinely free forever (not a
     12-month trial), currently 2 OCPU / 12GB RAM ARM instance (recently
     reduced from 4/24, still far more than this single Python process
     needs) plus 200GB storage and 10TB egress. Some reported inconsistency
     in how the reduced limit is enforced — verify current terms at
     signup.
   - **Small VPS** (Hetzner, DigitalOcean, Linode/Akamai, Vultr) — roughly
     $4-6/month, no free-tier fine print, and gets `systemd` instead of
     `launchd` — a real auto-restart-on-crash daemon manager that doesn't
     hit the unsigned-binary wall that blocked `launchd` here (see
     `launchd` status above).
   - **A dedicated always-on device at home** (Raspberry Pi, old mini PC) —
     one-time cost, no lid to close, but still depends on home power/network
     staying up.
   This is a bigger decision (new host, credentials, access) than a config
   change — worth a deliberate choice, not a silent migration.

<a id="dead-mans-switch"></a>
## Dead-man's switch (added 2026-07-14)

The bot went completely dead for 3 days (2026-07-10 → 07-13, zero `job_runs`
rows, zero `dashboard.log` activity) with no alert at all — only a human
happened to check. Nothing running *inside* the bot process can ever detect
its own death, so `monitoring/dead_mans_switch.py` runs as a **separate**
process and fires the same webhook alert (`monitoring/alerts.py`) if no
`job_runs` row exists for the most recently completed NYSE session. As of
2026-07-17 it also checks the watchdog's own log freshness — see
[`#watchdog`](#watchdog) below.

```bash
python -m monitoring.dead_mans_switch    # one-off manual check, exit 0 = healthy
```

**Status (2026-07-17): a LaunchDaemon, not a LaunchAgent** — moved the same
day and for the same reason as the watchdog below (survive reboot/logout
without a login session), via the same `osascript ... with administrator
privileges` install pattern. Loaded via
`/Library/LaunchDaemons/com.thomasvromen.tradingbot-deadmansswitch.plist`
(root-owned, `chmod 644`, `UserName: thomasvromen`). The original
2026-07-14 LaunchAgent version had no `KeepAlive` (a periodic
`StartInterval` check, not a persistent daemon), so it was never subject to
the macOS Background Task Management block that closed the main bot's own
`KeepAlive` auto-restart — that history is why this was the first LaunchAgent
that ever worked unattended, before the LaunchDaemon move made it survive
reboot too.

```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/com.thomasvromen.tradingbot-deadmansswitch.plist  # if not already loaded
launchctl print system/com.thomasvromen.tradingbot-deadmansswitch | head -6   # confirm registered
```
Runs every 4h (`StartInterval`) plus once immediately on load/boot. Logs to
`dead_mans_switch.log` (separate from `bot.log` and `watchdog.log`).

<a id="watchdog"></a>
## Reliability watchdog (added 2026-07-16)

The dead-man's switch above is **alert-only** and checks every 4 hours —
it only ever catches a fully-dead process, and only by the next trading
day at the earliest. On 2026-07-16 the bot wedged (scheduler silently
stopped dispatching jobs — see [`#sleep-wedges`](#sleep-wedges)) and sat
undetected for ~20 hours because that was, by design, an "alert a human
and hope they check soon" model. This **reverses** the 2026-07-14
decision to stay alert-only (see `docs/STATE.md`'s Decisions log for the
original reasoning and the reversal) — `monitoring/watchdog.py` runs as
its own `StartInterval` LaunchAgent, checks every 15 minutes, and
**takes action** instead of just alerting.

**What it checks, in order:**
1. Is the PID from `bot_status.json` (written at bot startup by
   `monitoring/status_file.py`) still alive? If not, restart — no
   further checks needed, a dead process can't be "mid-job".
2. Has `bot.log` been touched in the last 10 minutes? If yes, do nothing
   this cycle — this is the safety gate that stops the watchdog from
   ever mistaking a legitimately long-running pipeline (the 07-16
   catch-up run took ~10 minutes end-to-end) for a wedge.
3. Is any of the three core cron jobs (`run_morning_pipeline` 15:40,
   `run_intraday_check` 20:00, `run_eod` 22:30, all Amsterdam time) more
   than 30 minutes overdue for today without a `job_runs` row? If so,
   restart.
4. Is the running process's commit (from `bot_status.json`) different
   from the repo's current `HEAD`? If so, restart — this closes the
   "stale deploy" gap that caused two prior incidents
   (`docs/CLAUDE-REFERENCE.md#history`, 2026-07-14 and 2026-07-15
   sessions each found the running process was older than the latest
   fix and needed a human-triggered restart to pick it up).

**Restart mechanics:** kills the old PID (verified via `ps -p <pid> -o
command=` to contain `run_bot.py` first — never kills on PID alone, in
case of PID reuse), relaunches via `caffeinate -i -s <python> run_bot.py`
with `start_new_session=True` (Python's own session-detach, equivalent to
what `nohup` does — **the watchdog does NOT shell out to `nohup` itself**,
unlike the manual command documented above under Starting), and fires an
alert via the existing webhook either way (so a restart is visible, not
silent). Why the divergence: `nohup` needs a console to detach FROM: a
genuine LaunchDaemon invocation has no controlling terminal at all, and
`nohup` fails outright (`can't detach from console: Inappropriate ioctl
for device`) before ever exec'ing python — live-observed 2026-07-17 on
the watchdog's second natural restart, silently leaving the bot down with
no further log output. The manual `nohup ... & disown` command under
Starting is still correct for a human running it in an actual terminal
(which has a console to detach from) — only the watchdog's own
programmatic restart needed this fix.

```bash
python -m monitoring.watchdog    # one-off manual cycle, logs its decision
sudo launchctl kickstart -k system/com.thomasvromen.tradingbot-watchdog  # force a cycle now
tail -f watchdog.log
```

**Status (2026-07-17): a LaunchDaemon, not a LaunchAgent.** Loaded via
`/Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist`
(root-owned, `chmod 644` — launchd refuses to load an incorrectly
permissioned system daemon), with a `UserName` key so it still runs as
`thomasvromen` (not root) and reads `.env`/writes `trading.db`/`bot.log`/
`bot_status.json` with normal ownership. `StartInterval` 900s (15 min) +
`RunAtLoad`, same as before.

**Why a daemon and not an agent:** a LaunchAgent (`gui/<uid>` domain)
only runs while that user is logged in — a reboot, crash, or logout left
the whole reliability stack (watchdog + bot) dead until someone logged
back in, since `launchd` never even started them. A LaunchDaemon (`system`
domain) starts at boot regardless of login state. **Does not close every
gap:** FileVault requires its own pre-boot password before macOS itself
boots at all, on a truly cold boot from fully powered off — no launchd
domain, system or gui, runs before that. This closes "OS restarted or you
got logged out while the disk stayed unlocked" (the common case), not
"laptop was fully powered off."

Install (needs one-time root — done via
`osascript -e '...with administrator privileges'`, which pops the native
auth dialog instead of requiring a Terminal/TTY, same pattern used for
`sudo pmset` above):
```bash
cp path/to/staged.plist /Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist
chown root:wheel /Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist
chmod 644 /Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist
launchctl bootstrap system /Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist
```

**To pause it** (e.g. during manual debugging of the bot itself, so the
watchdog doesn't restart it mid-investigation):
```bash
sudo launchctl bootout system/com.thomasvromen.tradingbot-watchdog
# ...when done:
sudo launchctl bootstrap system /Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist
```

## Daily health check

Once a day, check:

1. **Dashboard:** `streamlit run dashboard/app.py` (reads `dashboard_state.json`).
   Confirm its timestamp is recent — it's updated by the scheduler jobs, all
   Amsterdam time (see `docs/CLAUDE-REFERENCE.md#scheduler` for the
   authoritative list; grep `orchestration/main_loop.py` for `add_job` if
   that drifts too — this line has already gone stale once, 2026-07-17,
   after the entry window moved 14:00→15:40 on 2026-07-13 and nobody
   updated this pointer): Monday 07:00 universe refresh, 13:00 + 17:00
   screener prefetch, 15:40 + 18:00 morning pipeline, 16:00 exit review,
   15:45 + 17:00 + 20:00 intraday checks, 22:30 EOD, Friday 22:45 weekly
   report. A stale timestamp past the next expected job time means
   something stopped running — check `watchdog.log` first, it likely
   already caught and is handling it.
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
- **watchdog_restart** — the watchdog auto-restarted the bot (dead process,
  overdue job, stale deploy, or the hard-ceiling bypass). Informational —
  this is the system working as designed. Check `watchdog.log` for which
  of the four triggers fired if you want to know why.
- **watchdog_crashed** — the watchdog process itself raised an unhandled
  exception. This needs a human: something in `monitoring/watchdog.py` is
  broken. Check `watchdog.log` for the traceback.
- **watchdog_crash_loop** — 3+ auto-restarts in the last 60 minutes;
  further auto-restart is suppressed. This means a restart isn't fixing
  the underlying problem — needs a human to actually diagnose and fix the
  bot, not another restart.
- **dead_mans_switch_watchdog** — the dead-man's-switch found
  `watchdog.log` stale (or missing entirely). The watchdog itself may have
  stopped running — check `launchctl print system/com.thomasvromen.tradingbot-watchdog`.

## Safe restart

**Most of the time you don't need this section** — the watchdog
(`docs/RUNBOOK.md#watchdog`) restarts the bot automatically on crash,
wedge, or stale deploy, gated on its own 10-min quiet-check so it won't
kill something legitimately mid-job. This section is for when you want to
restart it yourself right now (e.g. deploying a code change you don't
want to wait ~15 min for the watchdog to notice via its stale-deploy
check).

Never kill or restart the bot mid-session — the scheduler runs on a
single-thread executor, so killing mid-job can leave a half-completed
pipeline run. Before restarting:

1. Check the log for the last completed job (see the scheduler job list
   above) and make sure nothing is mid-run.
2. Restart with the same command you started it with (`## Starting`
   above). `initialize()` calls `reconcile_with_broker()` automatically on
   startup, which cleans up any ghost positions left by an unclean
   shutdown — so a restart between jobs is safe.

## Stopping for the month / moving to real cash

**Pause the watchdog FIRST, or it undoes everything below within ~15
minutes** — it exists specifically to relaunch the bot whenever it finds
it not running, and it can't tell "intentionally stopped" from "crashed":
```bash
sudo launchctl bootout system/com.thomasvromen.tradingbot-watchdog
```
Then stop the bot itself:
- **tmux:** reattach (`tmux attach -t bot`) and Ctrl-C.
- **nohup:** `kill <PID>` (find it with `ps aux | grep run_bot.py`).

**To resume later, one command is enough — it auto-starts the bot too:**
```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist
```
`bot_status.json` isn't deleted when you `kill` the bot — it's still on
disk pointing at the now-dead PID from before you stopped it. The
watchdog's very first cycle after resuming will see that PID isn't alive
and relaunch the bot immediately via its normal `restart_bot()` path — you
do NOT need to also run `## Starting` manually. If you want to resume the
watchdog WITHOUT immediately restarting the bot (e.g., just to check
something), delete or rename `bot_status.json` first.

Before sizing any real capital, review the **live paper P&L track record**
from this run (`performance/tracker.py` / dashboard) — not the backtest
numbers in `docs/FACTOR_BACKTEST_2026-06-28.md`, which are a simplified,
survivorship-biased proxy, not a live performance guarantee.
