# Watchdog Residual-Gaps Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** close the four residual gaps identified after the 2026-07-16/17 watchdog build — reboot-without-login, an undetected bug in the watchdog itself, an infinite restart-crash-loop on a genuine code bug, and a "stuck but still logging" scenario that never trips the quiet-gate.

**Architecture:** (1) move the watchdog from a per-login LaunchAgent to a system-level LaunchDaemon (survives reboot/logout, not cold-boot-from-off — FileVault requires that regardless); (2) wrap the watchdog's own `main()` in a try/except that alerts on any unhandled exception, plus have the *existing, independent* `dead_mans_switch.py` also check the watchdog's own log freshness — a second process checking the first, so a watchdog bug can't go silent; (3) a small restart-history file + threshold in `restart_bot()` that suppresses further auto-restarts and alerts distinctly once restarts look like a loop instead of a one-off recovery; (4) a second, longer "hard ceiling" grace window that bypasses the quiet-gate entirely, so something that keeps producing log output without ever finishing a real job still gets caught eventually.

**Tech Stack:** same as the existing watchdog — stdlib only, pytest + pytest-mock, macOS `launchd` (adding `/Library/LaunchDaemons/` this time, not `~/Library/LaunchAgents/`).

**Out of scope:** cold-boot-from-fully-powered-off (FileVault pre-boot password is an unavoidable hardware-level gate, not something software can skip — documented, not solved); migrating off the laptop entirely (bigger decision, already deferred in the original plan).

---

### Task 1: LaunchDaemon for the watchdog (closes gap 1)

**Files:**
- Create (via root-privileged shell, not a repo file): `/Library/LaunchDaemons/com.thomasvromen.tradingbot-watchdog.plist`
- Remove: `~/Library/LaunchAgents/com.thomasvromen.tradingbot-watchdog.plist` (superseded — running both would double-fire)
- Modify: `trading bot/docs/RUNBOOK.md` (`#watchdog` section)

- [ ] **Step 1: Write the LaunchDaemon plist to a scratch path first**

Root-owned files can't be written directly by an unprivileged `Write`. Stage it in the scratchpad:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.thomasvromen.tradingbot-watchdog</string>

    <key>UserName</key>
    <string>thomasvromen</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>-m</string>
        <string>monitoring.watchdog</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/thomasvromen/Documents/Claude code test/trading bot</string>

    <!-- A LaunchDaemon (system domain), not a LaunchAgent (gui/<uid>
         domain) -- starts at boot regardless of whether anyone is logged
         in. UserName keeps it running as thomasvromen (not root) so it
         reads .env / writes trading.db / bot.log / bot_status.json with
         normal ownership. Does NOT help with a truly cold boot from fully
         powered off -- FileVault's pre-boot password gate is unavoidable
         and happens before any launchd domain (system or gui) starts. -->

    <key>StartInterval</key>
    <integer>900</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/thomasvromen/Documents/Claude code test/trading bot/watchdog.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/thomasvromen/Documents/Claude code test/trading bot/watchdog.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Install it with the same osascript admin-privileges pattern used for `pmset`**

One shell script, run via `osascript -e '... with administrator privileges'` (pops the native auth dialog, no Terminal/TTY needed): copy the staged plist to `/Library/LaunchDaemons/`, `chown root:wheel`, `chmod 644` (launchd refuses to load an incorrectly-owned/permissioned LaunchDaemon plist), then `launchctl bootstrap system <path>`.

- [ ] **Step 3: Unload and remove the old LaunchAgent**

```bash
launchctl bootout gui/$(id -u)/com.thomasvromen.tradingbot-watchdog
rm ~/Library/LaunchAgents/com.thomasvromen.tradingbot-watchdog.plist
```

- [ ] **Step 4: Verify**

```bash
sudo launchctl list | grep tradingbot-watchdog   # via the same osascript pattern if sudo prompts
launchctl print system/com.thomasvromen.tradingbot-watchdog | head -20
```
Force a cycle and confirm a fresh log line: `sudo launchctl kickstart -k system/com.thomasvromen.tradingbot-watchdog` (or wait for the next natural 15-min tick), then `tail -3 watchdog.log`.

- [ ] **Step 5: Update `docs/RUNBOOK.md#watchdog`**

Note the LaunchDaemon change, the `UserName` key, the install command, and the FileVault caveat explicitly (don't let a future reader assume this closes cold-boot too).

- [ ] **Step 6: No code commit for the plist itself** (outside the repo) — commit the RUNBOOK.md update alone.

```bash
cd "trading bot" && git add docs/RUNBOOK.md
git commit -m "docs: watchdog moved to a LaunchDaemon (survives reboot without login)"
```

---

### Task 2: Watchdog-for-the-watchdog (closes gap 2)

**Files:**
- Modify: `trading bot/monitoring/watchdog.py` (`main()`)
- Modify: `trading bot/monitoring/dead_mans_switch.py` (new check)
- Test: `trading bot/tests/test_watchdog.py`, `trading bot/tests/test_dead_mans_switch.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_watchdog.py`:
```python
def test_main_alerts_and_returns_1_on_unhandled_exception(mocker):
    mocker.patch("monitoring.watchdog.setup_logging")
    mocker.patch("monitoring.watchdog.check_and_recover", side_effect=RuntimeError("boom"))
    alert_spy = mocker.patch("monitoring.watchdog.fire_alert")

    result = watchdog.main()

    assert result == 1
    alert_spy.assert_called_once()
    assert alert_spy.call_args.args[0] == "watchdog_crashed"


def test_main_returns_0_on_normal_cycle(mocker):
    mocker.patch("monitoring.watchdog.setup_logging")
    mocker.patch("monitoring.watchdog.check_and_recover", return_value="healthy:quiet_but_expected")
    alert_spy = mocker.patch("monitoring.watchdog.fire_alert")

    assert watchdog.main() == 0
    alert_spy.assert_not_called()
```

`tests/test_dead_mans_switch.py` (add near the top, after the existing imports):
```python
def test_watchdog_check_healthy_when_log_recent(mocker, tmp_path):
    log_file = tmp_path / "watchdog.log"
    log_file.write_text("2026-07-17 11:15:11 INFO ... Watchdog cycle: healthy\n")
    mocker.patch("monitoring.dead_mans_switch._WATCHDOG_LOG", log_file)
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")
    now = datetime.fromtimestamp(log_file.stat().st_mtime, tz=UTC)

    assert check_watchdog_freshness(now=now) is True
    alert_spy.assert_not_called()


def test_watchdog_check_stale_when_log_old_fires_distinct_alert(mocker, tmp_path):
    from datetime import timedelta
    log_file = tmp_path / "watchdog.log"
    log_file.write_text("old\n")
    mocker.patch("monitoring.dead_mans_switch._WATCHDOG_LOG", log_file)
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")
    stale_now = datetime.fromtimestamp(log_file.stat().st_mtime, tz=UTC) + timedelta(minutes=41)

    assert check_watchdog_freshness(now=stale_now) is False
    alert_spy.assert_called_once()
    assert alert_spy.call_args.args[0] == "dead_mans_switch_watchdog"


def test_watchdog_check_stale_when_log_missing_entirely(mocker, tmp_path):
    mocker.patch("monitoring.dead_mans_switch._WATCHDOG_LOG", tmp_path / "does_not_exist.log")
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")

    assert check_watchdog_freshness() is False
    alert_spy.assert_called_once()
```
Add `from monitoring.dead_mans_switch import check_pipeline_freshness, check_watchdog_freshness` to the existing import line.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_watchdog.py -k "main_" tests/test_dead_mans_switch.py -v`
Expected: FAIL — `check_watchdog_freshness` doesn't exist yet; `main()`'s current form never calls `fire_alert` on exception, it just propagates.

- [ ] **Step 3: Implement `main()`'s try/except in `monitoring/watchdog.py`**

```python
def main() -> int:
    setup_logging()
    try:
        result = check_and_recover()
        log.info("Watchdog cycle: %s", result)
        return 0
    except Exception as exc:
        log.exception("Watchdog cycle crashed unexpectedly")
        fire_alert(
            "watchdog_crashed",
            f"Watchdog cycle raised an unhandled exception: {exc}",
            {"error": str(exc)},
        )
        return 1
```
This replaces the existing `main()` body (find it via the `if __name__ == "__main__":` anchor near the file's end — it's the last function).

- [ ] **Step 4: Implement `check_watchdog_freshness` in `monitoring/dead_mans_switch.py`**

Add near the top, after the existing `_NYSE` constant:
```python
_WATCHDOG_LOG = Path(__file__).resolve().parent.parent / "watchdog.log"
_WATCHDOG_STALE_MINUTES = 40  # ~2.5x the watchdog's own 15-min StartInterval
```
(needs `from pathlib import Path` added to the imports — check it isn't already there first.)

Add the function after `check_pipeline_freshness`:
```python
def check_watchdog_freshness(now: datetime | None = None) -> bool:
    """Return True if the reliability watchdog (monitoring/watchdog.py)
    itself looks alive, False if it appears to have stopped running
    entirely. This is the layer that catches a bug IN the watchdog --
    nothing inside that process can be trusted to detect its own failure,
    same reasoning as check_pipeline_freshness above but one level up."""
    now = now or datetime.now(UTC)
    if not _WATCHDOG_LOG.exists():
        fire_alert(
            "dead_mans_switch_watchdog",
            "watchdog.log does not exist -- the reliability watchdog may never have run",
            {},
        )
        return False
    age_minutes = (now - datetime.fromtimestamp(
        _WATCHDOG_LOG.stat().st_mtime, tz=UTC
    )).total_seconds() / 60
    if age_minutes > _WATCHDOG_STALE_MINUTES:
        fire_alert(
            "dead_mans_switch_watchdog",
            f"watchdog.log hasn't been touched in {age_minutes:.0f} min "
            f"(expected every 15) -- the reliability watchdog may have stopped running",
            {"age_minutes": age_minutes},
        )
        return False
    return True
```

Update `main()` to call both checks:
```python
def main() -> int:
    setup_logging()
    pipeline_healthy = check_pipeline_freshness()
    watchdog_healthy = check_watchdog_freshness()
    return 0 if (pipeline_healthy and watchdog_healthy) else 1
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_watchdog.py tests/test_dead_mans_switch.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add monitoring/watchdog.py monitoring/dead_mans_switch.py tests/test_watchdog.py tests/test_dead_mans_switch.py
git commit -m "feat: watchdog-for-the-watchdog -- alert on crash, cross-check log freshness

main() now alerts on any unhandled exception instead of failing silently
until the next 15-min cycle. dead_mans_switch.py (independent process)
now also checks watchdog.log freshness, so a bug that stops the
watchdog from ever running successfully still gets a human paged."
```

---

### Task 3: Crash-loop circuit breaker (closes gap 3)

**Files:**
- Modify: `trading bot/monitoring/watchdog.py`
- Test: `trading bot/tests/test_watchdog.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_restart_bot_suppresses_when_crash_loop_detected(mocker):
    mocker.patch("monitoring.watchdog._recent_restart_count", return_value=3)
    mock_popen = mocker.patch("monitoring.watchdog.subprocess.Popen")
    mock_kill = mocker.patch("monitoring.watchdog.os.kill")
    alert_spy = mocker.patch("monitoring.watchdog.fire_alert")

    watchdog.restart_bot("test reason", {"pid": 4242})

    mock_popen.assert_not_called()
    mock_kill.assert_not_called()
    alert_spy.assert_called_once()
    assert alert_spy.call_args.args[0] == "watchdog_crash_loop"


def test_restart_bot_proceeds_when_under_crash_loop_threshold(mocker):
    mocker.patch("monitoring.watchdog._recent_restart_count", return_value=1)
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=False)
    mocker.patch("monitoring.watchdog._record_restart")
    mock_popen = mocker.patch("monitoring.watchdog.subprocess.Popen")
    mocker.patch("monitoring.watchdog.fire_alert")
    mocker.patch("builtins.open", mocker.mock_open())

    watchdog.restart_bot("test reason", {"pid": 4242})

    mock_popen.assert_called_once()


def test_restart_bot_records_history_on_successful_restart(mocker):
    mocker.patch("monitoring.watchdog._recent_restart_count", return_value=0)
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=False)
    record_spy = mocker.patch("monitoring.watchdog._record_restart")
    mocker.patch("monitoring.watchdog.subprocess.Popen")
    mocker.patch("monitoring.watchdog.fire_alert")
    mocker.patch("builtins.open", mocker.mock_open())

    watchdog.restart_bot("test reason", {"pid": 4242})

    record_spy.assert_called_once()


def test_recent_restart_count_prunes_entries_outside_window(mocker, tmp_path):
    from datetime import timedelta
    history_file = tmp_path / "watchdog_restart_history.json"
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=90)).isoformat()
    recent = (now - timedelta(minutes=10)).isoformat()
    history_file.write_text(json.dumps([
        {"timestamp": old, "reason": "old one, outside 60min window"},
        {"timestamp": recent, "reason": "recent one, inside window"},
    ]))
    mocker.patch("monitoring.watchdog._RESTART_HISTORY_FILE", history_file)

    assert watchdog._recent_restart_count(now) == 1


def test_recent_restart_count_zero_when_no_history_file(tmp_path):
    mocker_path = tmp_path / "does_not_exist.json"
    import monitoring.watchdog as wd
    original = wd._RESTART_HISTORY_FILE
    wd._RESTART_HISTORY_FILE = mocker_path
    try:
        assert wd._recent_restart_count(datetime(2026, 7, 17, 12, 0, tzinfo=UTC)) == 0
    finally:
        wd._RESTART_HISTORY_FILE = original
```
Add `import json` and `from datetime import datetime, UTC` to the test file's imports if not already present (check first — `datetime`/`UTC` likely already imported for the existing `find_overdue_job` tests; `json` is new).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_watchdog.py -k "crash_loop or recent_restart_count" -v`
Expected: FAIL — `_recent_restart_count`/`_record_restart`/`_RESTART_HISTORY_FILE` don't exist yet.

- [ ] **Step 3: Implement in `monitoring/watchdog.py`**

Add near the other module constants:
```python
_RESTART_HISTORY_FILE = _REPO_DIR / "watchdog_restart_history.json"
_CRASH_LOOP_WINDOW_MINUTES = 60
_CRASH_LOOP_THRESHOLD = 3
```
Add near `restart_bot`, before it:
```python
def _read_restart_history() -> list[dict]:
    try:
        return json.loads(_RESTART_HISTORY_FILE.read_text())
    except Exception:
        return []


def _record_restart(reason: str, now: datetime) -> None:
    history = _read_restart_history()
    history.append({"timestamp": now.isoformat(), "reason": reason})
    cutoff = now - timedelta(hours=24)
    history = [h for h in history if datetime.fromisoformat(h["timestamp"]) >= cutoff]
    try:
        _RESTART_HISTORY_FILE.write_text(json.dumps(history))
    except Exception as exc:
        log.warning("Could not write restart history: %s", exc)


def _recent_restart_count(now: datetime, window_minutes: int = _CRASH_LOOP_WINDOW_MINUTES) -> int:
    cutoff = now - timedelta(minutes=window_minutes)
    return sum(
        1 for h in _read_restart_history()
        if datetime.fromisoformat(h["timestamp"]) >= cutoff
    )
```
Modify `restart_bot`'s signature and body:
```python
def restart_bot(reason: str, status: dict | None, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    recent = _recent_restart_count(now)
    if recent >= _CRASH_LOOP_THRESHOLD:
        log.error(
            "Crash-loop suspected (%d restarts in the last %d min) -- "
            "suppressing further auto-restart", recent, _CRASH_LOOP_WINDOW_MINUTES,
        )
        fire_alert(
            "watchdog_crash_loop",
            f"Suppressing auto-restart: {recent} restarts in the last "
            f"{_CRASH_LOOP_WINDOW_MINUTES} min. This looks like a persistent "
            f"bug, not a transient wedge -- manual intervention required.",
            {"reason": reason, "recent_restart_count": recent},
        )
        return

    old_pid = status.get("pid") if status else None
    if old_pid is not None and is_process_alive(old_pid):
        if cmdline_matches_run_bot(old_pid):
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(10):
                if not is_process_alive(old_pid):
                    break
                time.sleep(1)
        else:
            log.warning(
                "Status file PID %d no longer matches run_bot.py (likely PID reuse) "
                "-- not killing it. Launching a new instance anyway.", old_pid,
            )

    with open(_LOG_FILE, "a") as log_fh:
        subprocess.Popen(
            ["nohup", "caffeinate", "-i", "-s", sys.executable, "run_bot.py"],
            cwd=str(_REPO_DIR), stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _record_restart(reason, now)
    fire_alert(
        "watchdog_restart",
        f"Auto-restarted trading bot: {reason}",
        {"reason": reason, "old_pid": old_pid},
    )
```
Add `import json` to the top of `watchdog.py`.

Update every `restart_bot(...)` call site inside `check_and_recover` to pass `now` (they already have `now` in scope): `restart_bot("process not alive", status, now)`, etc.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_watchdog.py -v`
Expected: all pass (existing 22 + 5 new = 27)

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add monitoring/watchdog.py tests/test_watchdog.py
git commit -m "feat: crash-loop circuit breaker on the watchdog's auto-restart

3+ restarts in 60 min now suppresses further auto-restart and fires a
distinct watchdog_crash_loop alert instead of retrying forever every 15
min against a persistent code bug that a restart can never fix."
```

---

### Task 4: Hard ceiling bypassing the quiet-gate (closes gap 4)

**Files:**
- Modify: `trading bot/monitoring/watchdog.py`
- Test: `trading bot/tests/test_watchdog.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_find_overdue_job_respects_custom_grace_minutes(mocker):
    mocker.patch.object(watchdog._NYSE, "is_session", return_value=True)
    mocker.patch.object(watchdog.db, "job_ran_today", return_value=False)
    now = datetime(2026, 7, 16, 16, 15, tzinfo=_AMS)  # 35 min past 15:40

    assert watchdog.find_overdue_job(now, grace_minutes=30) == "run_morning_pipeline"
    assert watchdog.find_overdue_job(now, grace_minutes=60) is None


def test_check_and_recover_bypasses_quiet_gate_on_hard_ceiling(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)

    def fake_overdue(now, grace_minutes=watchdog._GRACE_MINUTES):
        return "run_eod" if grace_minutes == watchdog._HARD_GRACE_MINUTES else None
    mocker.patch("monitoring.watchdog.find_overdue_job", side_effect=fake_overdue)
    mocker.patch("monitoring.watchdog._log_quiet_for", return_value=False)  # still "busy"
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_called_once()
    assert result == "restarted:hard_overdue:run_eod"


def test_check_and_recover_does_not_bypass_when_under_hard_ceiling(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog.find_overdue_job", return_value=None)  # never overdue, either grace
    mocker.patch("monitoring.watchdog._log_quiet_for", return_value=False)
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_not_called()
    assert result == "healthy:recent_activity"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_watchdog.py -k "grace_minutes or hard_ceiling" -v`
Expected: FAIL — `find_overdue_job` doesn't accept `grace_minutes` yet; `_HARD_GRACE_MINUTES` doesn't exist; `check_and_recover` never bypasses the quiet gate.

- [ ] **Step 3: Implement**

Add the new constant near `_GRACE_MINUTES`:
```python
_HARD_GRACE_MINUTES = 120  # absolute ceiling that bypasses the quiet-gate entirely
```
Change `find_overdue_job`'s signature to accept an override:
```python
def find_overdue_job(now: datetime, grace_minutes: int = _GRACE_MINUTES) -> str | None:
    """First expected job that's overdue today (Amsterdam time), or None."""
    now_local = now.astimezone(_AMSTERDAM)
    today = now_local.date().isoformat()
    if not _NYSE.is_session(today):
        return None
    for job_name, hour, minute in _EXPECTED_JOBS:
        deadline = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0) \
            + timedelta(minutes=grace_minutes)
        if now_local < deadline:
            continue
        if not db.job_ran_today(job_name, today):
            return job_name
    return None
```
Update `check_and_recover` to check the hard ceiling before the quiet-gate:
```python
def check_and_recover(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    status = read_status_file()

    if status is None:
        log.warning("No status file found -- bot may not have started via the "
                    "watchdog-aware code path yet. Skipping this cycle.")
        return "no_status_file"

    if not is_process_alive(status["pid"]):
        restart_bot("process not alive", status, now)
        return "restarted:process_dead"

    hard_overdue = find_overdue_job(now, grace_minutes=_HARD_GRACE_MINUTES)
    if hard_overdue is not None:
        restart_bot(
            f"job '{hard_overdue}' overdue by >{_HARD_GRACE_MINUTES}min "
            f"(hard ceiling, bypassed quiet-gate)", status, now,
        )
        return f"restarted:hard_overdue:{hard_overdue}"

    if not _log_quiet_for(_QUIET_MINUTES, now):
        return "healthy:recent_activity"

    overdue = find_overdue_job(now)
    if overdue is not None:
        restart_bot(f"job '{overdue}' overdue by >{_GRACE_MINUTES}min", status, now)
        return f"restarted:overdue:{overdue}"

    if is_stale_deploy(status, get_git_commit(_REPO_DIR)):
        restart_bot("running commit does not match HEAD", status, now)
        return "restarted:stale_deploy"

    return "healthy:quiet_but_expected"
```

- [ ] **Step 4: Run the FULL watchdog test file** — this task changes `check_and_recover`'s control flow, which every earlier `test_check_and_recover_*` test also exercises.

Run: `pytest tests/test_watchdog.py -v`
Expected: all pass. If any earlier `check_and_recover` test now fails, it's almost certainly because `find_overdue_job` is now called with the hard-ceiling grace FIRST and the test's mock returns a value for every call regardless of `grace_minutes` — fix by using a `side_effect` callable keyed on `grace_minutes` (see Task 3/4's own tests for the pattern), not a bare `return_value`.

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add monitoring/watchdog.py tests/test_watchdog.py
git commit -m "feat: hard ceiling bypasses the quiet-gate for a stuck-but-logging bot

The 10-min quiet-gate is a deliberate safety valve against killing a
legitimately busy pipeline, but it means anything that keeps writing
log output without ever completing a real job (e.g. an infinite retry
loop) would never trip the staleness check. A 120-min hard ceiling now
overrides the quiet-gate -- bounds even this failure mode instead of
leaving it genuinely unbounded."
```

---

### Task 5: Final documentation pass

**Files:**
- Modify: `docs/STATE.md`, `trading bot/CLAUDE.md`, `trading bot/docs/CLAUDE-REFERENCE.md`

- [ ] **Step 1: Run the full suite one final time**

Run: `cd "trading bot" && pytest -q`
Expected: green, count higher than the 975 baseline (975 + ~2 from Task 2's watchdog test + 3 from dead_mans_switch + 5 from Task 3 + 3 from Task 4 = ~988).

- [ ] **Step 2: Record all four fixes in `docs/STATE.md`** — `## Done` entry dated 2026-07-17, `## Decisions` note that this directly answers the user's "will it ever happen again without intervention" question with the honest residual (cold-boot-from-off only).

- [ ] **Step 3: Append to `trading bot/docs/CLAUDE-REFERENCE.md#history`** and update `CLAUDE.md`'s banner test count, per the repo's own convention.

- [ ] **Step 4: Commit**

```bash
cd "trading bot" && git add CLAUDE.md docs/CLAUDE-REFERENCE.md
cd .. && git add docs/STATE.md
git commit -m "docs: record the four residual-gap fixes closing out the reliability watchdog work"
```

## Self-review notes

- **Spec coverage:** gap 1 (LaunchDaemon, Task 1) ✓, gap 2 (crash-alert + cross-check, Task 2) ✓, gap 3 (circuit breaker, Task 3) ✓, gap 4 (hard ceiling, Task 4) ✓.
- **Placeholder scan:** none — every step has complete code or an exact shell command.
- **Type consistency:** `find_overdue_job(now: datetime, grace_minutes: int = _GRACE_MINUTES) -> str | None`, `restart_bot(reason: str, status: dict | None, now: datetime | None = None) -> None`, `check_watchdog_freshness(now: datetime | None = None) -> bool` — consistent across implementation and tests. `restart_bot`'s new third parameter is backward-compatible (defaults to `None` -> `datetime.now(UTC)`), so it doesn't break any existing call site the plan doesn't also update.
- **Ordering risk flagged explicitly in Task 4 Step 4**: changing `check_and_recover`'s control flow can silently break earlier tests that mock `find_overdue_job` with a bare `return_value` instead of a `grace_minutes`-aware `side_effect` — called out so it isn't a surprise mid-implementation.
