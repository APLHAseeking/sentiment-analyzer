# Trading Bot Reliability Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound any future trading-bot downtime to ~15-30 minutes, regardless of root cause, by replacing the current "alert a human and hope they check" model with an active watchdog that detects staleness and auto-restarts — plus close the two lowest-risk contributing gaps (Power Nap sleep cycling, unrecorded intraday/EOD job completions).

**Architecture:** A new, independent LaunchAgent (`monitoring/watchdog.py`, same `StartInterval` pattern as the already-working `dead_mans_switch.py`) runs every 15 minutes. It reads a small status file the bot writes at startup (PID + git commit), checks process liveness, per-job staleness (extending `job_runs` coverage to all three core cron jobs, not just `run_morning_pipeline`), and deploy freshness (running commit vs. HEAD) — gated by a "no log activity in the last 10 minutes" safety guard so it never kills a bot that's legitimately mid-pipeline. On any stale signal it kills the old PID (verified by cmdline, never by image name) and relaunches with the documented `nohup caffeinate -i -s python3 run_bot.py` command, alerting either way via the existing `fire_alert` webhook.

**Tech Stack:** Python 3.11+ stdlib (`os`, `subprocess`, `signal`, `pathlib`), `exchange_calendars` (already a dep, used by `dead_mans_switch.py`), pytest + `pytest-mock` (repo convention — all tests offline, no real subprocess/DB/network calls), macOS `launchd`.

**Out of scope (explicitly, per user's answers to the pre-plan questions):**
- `sudo pmset -a disablesleep 1` or migrating off the laptop — user chose the lighter "Power Nap off" mitigation instead; those stay documented options only.
- Russell 1000 universe fix, `requirements.txt` pinning, `docs/guardrails/MIGRATION-LOG.md` drift — pre-existing unrelated open items, not touched.
- `sudo pmset -a powernap 0` itself is NOT run by the agent — sudo requires the user's password interactively. Task 6 hands the user the exact command.

---

### Task 1: Extend `job_runs` coverage to `run_intraday_check` and `run_eod`

Today only `run_morning_pipeline` calls `db.record_job_run()` (confirmed via grep — it's the only call site in `orchestration/main_loop.py`). That means a wedge occurring *after* the morning pipeline already succeeded (e.g. incident #20, 2026-07-14: PID 51755 went idle 2h+ mid-afternoon after already running that day) is invisible to any freshness check keyed on `job_runs`. The watchdog needs all three core jobs recorded to have a real signal throughout the trading day.

**Files:**
- Modify: `trading bot/orchestration/main_loop.py:1455-1458` (end of `run_intraday_check`) and `:1508-1511` (end of `run_eod`)
- Test: `trading bot/tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Add near the existing `test_run_eod_*` tests (around line 1090) and near `test_intraday_check_deleverage_excludes_hedges` (around line 898):

```python
def test_run_intraday_check_records_job_run(mocker, orch):
    orch._broker = _mock_broker(cash=50_000, position_value=50_000)
    orch._risk = mocker.MagicMock()
    orch._risk.state = RiskState.NORMAL
    orch._portfolio.enforce_stop_losses = mocker.MagicMock()
    record_spy = mocker.patch("orchestration.main_loop.record_job_run")

    orch.run_intraday_check()

    record_spy.assert_called_once_with("run_intraday_check", date.today().isoformat())


def test_run_eod_records_job_run(mocker, orch):
    orch._broker = _mock_broker(cash=50_000, position_value=50_000)
    orch._risk = mocker.MagicMock()
    orch._risk.state = RiskState.NORMAL
    orch._portfolio.log_snapshot = mocker.MagicMock()
    mocker.patch.object(orch, "_update_dashboard")
    record_spy = mocker.patch("orchestration.main_loop.record_job_run")

    orch.run_eod()

    record_spy.assert_called_once_with("run_eod", date.today().isoformat())
```

Check the file's existing imports at the top (`from risk.risk_manager import RiskState` is already imported inline in nearby tests — follow the same local-import style already used at line 1077/1092; `date` is already imported module-wide per the existing `test_start_does_not_catch_up_when_already_ran_today` test using `date.today()` implicitly via `orch.start()` — confirm `from datetime import date` is already imported at the top of `test_orchestrator.py`, add it if missing).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "trading bot" && pytest tests/test_orchestrator.py -k "records_job_run" -v`
Expected: FAIL — `record_job_run` not called (current code never calls it in these two methods).

- [ ] **Step 3: Add the two calls**

In `run_intraday_check` (currently ends at line 1457 with the `except Exception as exc: log.warning(...)` block), add the call right after the try/except, before the method ends:

```python
            log.info("Intraday check complete. Risk state: %s", self._risk.state.value)
        except Exception as exc:
            log.warning("Intraday check failed: %s", exc)
        record_job_run("run_intraday_check", date.today().isoformat())
```

In `run_eod` (currently ends at line 1510 with `log.info("EOD snapshot logged. Risk state: %s", ...)`), add the call right after:

```python
        self._update_dashboard()
        log.info("EOD snapshot logged. Risk state: %s", self._risk.state.value)
        record_job_run("run_eod", date.today().isoformat())
```

Both `record_job_run` and `date` are already imported at the top of `main_loop.py` (`record_job_run` via the `from bot.db import (...)` block at line 52, `date` via `from datetime import date, datetime, timedelta` at line 30) — no new imports needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "trading bot" && pytest tests/test_orchestrator.py -k "records_job_run" -v`
Expected: PASS

- [ ] **Step 5: Run the full orchestrator test file to check for regressions**

Run: `cd "trading bot" && pytest tests/test_orchestrator.py -q`
Expected: all pass (same count as the Task 0 baseline, +2)

- [ ] **Step 6: Commit**

```bash
cd "trading bot" && git add orchestration/main_loop.py tests/test_orchestrator.py
git commit -m "feat: record run_intraday_check/run_eod completions in job_runs

Only run_morning_pipeline was recorded, so a wedge occurring after the
morning pipeline already succeeded was invisible to any job_runs-based
staleness check. Needed by the new watchdog (next commit)."
```

---

### Task 2: Startup status file (`monitoring/status_file.py`)

The watchdog needs to know, from outside the bot process, which PID it should find and which git commit that PID is running (to catch incidents #18/#22 — a running process silently older than the latest fix, which has already happened twice and needed a human to notice).

**Files:**
- Create: `trading bot/monitoring/status_file.py`
- Test: `trading bot/tests/test_status_file.py`
- Modify: `trading bot/orchestration/main_loop.py:70` (import) and `:242-245` (call after "Orchestrator initialized" log)

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_status_file.py"""
from __future__ import annotations

import json

from monitoring.status_file import get_git_commit, read_status_file, write_status_file


def test_get_git_commit_returns_stripped_output(mocker, tmp_path):
    mock_run = mocker.patch("monitoring.status_file.subprocess.run")
    mock_run.return_value.stdout = "abc123\n"
    mock_run.return_value.returncode = 0

    commit = get_git_commit(tmp_path)

    assert commit == "abc123"


def test_get_git_commit_returns_none_on_failure(mocker, tmp_path):
    mocker.patch("monitoring.status_file.subprocess.run", side_effect=OSError("no git"))

    assert get_git_commit(tmp_path) is None


def test_write_and_read_status_file_roundtrip(mocker, tmp_path):
    mocker.patch("monitoring.status_file.get_git_commit", return_value="abc123")
    mocker.patch("monitoring.status_file.os.getpid", return_value=4242)
    path = tmp_path / "bot_status.json"

    write_status_file(path=path, repo_dir=tmp_path)
    status = read_status_file(path=path)

    assert status["pid"] == 4242
    assert status["commit"] == "abc123"
    assert "started_at" in status


def test_write_status_file_does_not_raise_on_write_failure(mocker, tmp_path):
    mocker.patch("monitoring.status_file.get_git_commit", return_value="abc123")
    bad_path = tmp_path / "no_such_dir" / "bot_status.json"  # parent doesn't exist

    write_status_file(path=bad_path, repo_dir=tmp_path)  # must not raise


def test_read_status_file_returns_none_when_missing(tmp_path):
    assert read_status_file(path=tmp_path / "missing.json") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "trading bot" && pytest tests/test_status_file.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring.status_file'`

- [ ] **Step 3: Write the implementation**

```python
"""trading bot/monitoring/status_file.py

Startup status file for monitoring/watchdog.py to read. Written once at
orchestrator startup (orchestration/main_loop.py). Lets a separate
watchdog process detect two things nothing inside the bot's own process
could ever check about itself: whether its PID is still alive, and
whether it's running stale code (see CLAUDE-REFERENCE.md#history
incidents #18/#22 -- a running process silently older than the latest
fix, twice, caught only because a human happened to check).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_STATUS_FILE = _REPO_DIR / "bot_status.json"


def get_git_commit(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def write_status_file(path: Path = _DEFAULT_STATUS_FILE, repo_dir: Path = _REPO_DIR) -> None:
    status = {
        "pid": os.getpid(),
        "commit": get_git_commit(repo_dir),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        path.write_text(json.dumps(status))
    except Exception as exc:
        log.warning("Could not write status file %s: %s", path, exc)


def read_status_file(path: Path = _DEFAULT_STATUS_FILE) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "trading bot" && pytest tests/test_status_file.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Wire the startup call into `main_loop.py`**

Add the import next to the existing `monitoring.logger` import (line 70):

```python
from monitoring.logger import EventType, emit_event, setup_logging
from monitoring.status_file import write_status_file
```

Add the call right after the "Orchestrator initialized" log statement (currently lines 242-245):

```python
        log.info("Orchestrator initialized. Regime: %s (conf=%.2f, stable=%s)",
                 self._regime_state.regime_label if self._regime_state else "unknown",
                 self._regime_state.confidence if self._regime_state else 0,
                 self._regime_state.is_stable if self._regime_state else False)
        write_status_file()
```

(Read the exact current lines 242-247 first with the Read tool before editing — the log call may wrap differently than shown here; match against the real file, don't guess the closing paren line.)

- [ ] **Step 6: Add a test confirming the startup hook fires**

In `tests/test_orchestrator.py`, find the existing orchestrator-construction test (search for how the `orch` fixture or an equivalent `__init__` test mocks `get_regime_data`/`HMMRegimeEngine` — reuse that exact fixture setup) and add:

```python
def test_orchestrator_init_writes_status_file(mocker, orch):
    # `orch` fixture already constructs a real orchestrator instance during
    # collection, before this test's mock is installed -- so instead,
    # confirm the call site exists and would fire, by re-invoking the
    # write directly is not meaningful here. Instead assert via import:
    # the simplest correct test is a spy installed BEFORE construction.
    write_spy = mocker.patch("orchestration.main_loop.write_status_file")
    # Re-run whatever the `orch` fixture does to build a fresh instance,
    # OR if the fixture is a plain function (not a class), call it again
    # here. Match the fixture's exact construction call -- read its
    # definition (grep "def orch" in conftest.py / this file) before
    # writing this test, since __init__ side effects (get_regime_data,
    # HMMRegimeEngine, broker mocks) must be mocked identically to how
    # the `orch` fixture already does it.
    ...
```

Do not commit the placeholder above — before writing this test, grep `def orch` in `tests/test_orchestrator.py`/`tests/conftest.py`, read the fixture's full body, and write a real test that constructs an orchestrator the same way the fixture does but with `write_status_file` mocked beforehand, then asserts `write_spy.assert_called_once()`. If the fixture is session/module-scoped and expensive to reconstruct, an acceptable alternative is a narrower unit test that just asserts the call exists at the right point via `inspect` is NOT acceptable (too indirect) — reconstruct a real instance; the existing test file already does this dozens of times for other `__init__` behaviors (e.g. `test_orchestrator_reconciles_ghost_positions`-style tests, if present — grep for one and mirror it).

- [ ] **Step 7: Run full orchestrator suite**

Run: `cd "trading bot" && pytest tests/test_orchestrator.py -q`
Expected: all pass, no regressions vs. Task 1's count

- [ ] **Step 8: Commit**

```bash
cd "trading bot" && git add monitoring/status_file.py tests/test_status_file.py orchestration/main_loop.py tests/test_orchestrator.py
git commit -m "feat: write startup status file (pid, git commit, started_at)

Lets an external watchdog check process liveness and deploy freshness
without any in-process self-monitoring (which can't detect its own
death or staleness)."
```

---

### Task 3: The watchdog itself (`monitoring/watchdog.py`)

**Files:**
- Create: `trading bot/monitoring/watchdog.py`
- Test: `trading bot/tests/test_watchdog.py`

- [ ] **Step 1: Write the failing tests**

```python
"""trading bot/tests/test_watchdog.py"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from monitoring import watchdog

_AMS = ZoneInfo("Europe/Amsterdam")


def test_find_overdue_job_returns_none_on_non_trading_day(mocker):
    mocker.patch.object(watchdog._NYSE, "is_session", return_value=False)
    now = datetime(2026, 7, 18, 18, 0, tzinfo=_AMS)  # a Saturday

    assert watchdog.find_overdue_job(now) is None


def test_find_overdue_job_returns_none_before_first_deadline(mocker):
    mocker.patch.object(watchdog._NYSE, "is_session", return_value=True)
    now = datetime(2026, 7, 16, 16, 0, tzinfo=_AMS)  # 16:00, before 15:40+30min=16:10

    assert watchdog.find_overdue_job(now) is None


def test_find_overdue_job_flags_missed_morning_pipeline(mocker):
    mocker.patch.object(watchdog._NYSE, "is_session", return_value=True)
    mocker.patch.object(watchdog.db, "job_ran_today", return_value=False)
    now = datetime(2026, 7, 16, 16, 15, tzinfo=_AMS)  # past 16:10 deadline

    assert watchdog.find_overdue_job(now) == "run_morning_pipeline"


def test_find_overdue_job_returns_none_when_job_recorded(mocker):
    mocker.patch.object(watchdog._NYSE, "is_session", return_value=True)
    mocker.patch.object(watchdog.db, "job_ran_today", return_value=True)
    now = datetime(2026, 7, 16, 23, 0, tzinfo=_AMS)  # past all three deadlines

    assert watchdog.find_overdue_job(now) is None


def test_is_process_alive_true_when_kill_succeeds(mocker):
    mocker.patch("monitoring.watchdog.os.kill")  # no exception raised

    assert watchdog.is_process_alive(4242) is True


def test_is_process_alive_false_when_process_gone(mocker):
    mocker.patch("monitoring.watchdog.os.kill", side_effect=ProcessLookupError)

    assert watchdog.is_process_alive(4242) is False


def test_is_stale_deploy_true_on_mismatch():
    assert watchdog.is_stale_deploy({"commit": "aaa"}, "bbb") is True


def test_is_stale_deploy_false_on_match():
    assert watchdog.is_stale_deploy({"commit": "aaa"}, "aaa") is False


def test_is_stale_deploy_false_when_either_side_unknown():
    assert watchdog.is_stale_deploy(None, "aaa") is False
    assert watchdog.is_stale_deploy({"commit": None}, "aaa") is False
    assert watchdog.is_stale_deploy({"commit": "aaa"}, None) is False


def test_cmdline_matches_run_bot_true(mocker):
    mock_run = mocker.patch("monitoring.watchdog.subprocess.run")
    mock_run.return_value.stdout = "/usr/bin/python3 run_bot.py\n"

    assert watchdog.cmdline_matches_run_bot(4242) is True


def test_cmdline_matches_run_bot_false_on_mismatch(mocker):
    mock_run = mocker.patch("monitoring.watchdog.subprocess.run")
    mock_run.return_value.stdout = "/usr/bin/python3 some_other_script.py\n"

    assert watchdog.cmdline_matches_run_bot(4242) is False


def test_restart_bot_kills_matching_pid_and_relaunches(mocker):
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog.cmdline_matches_run_bot", return_value=True)
    mock_kill = mocker.patch("monitoring.watchdog.os.kill")
    mocker.patch("monitoring.watchdog.time.sleep")
    mock_popen = mocker.patch("monitoring.watchdog.subprocess.Popen")
    mock_alert = mocker.patch("monitoring.watchdog.fire_alert")
    mocker.patch("builtins.open", mocker.mock_open())

    watchdog.restart_bot("test reason", {"pid": 4242})

    mock_kill.assert_called_once()
    mock_popen.assert_called_once()
    launch_cmd = mock_popen.call_args.args[0]
    assert "run_bot.py" in launch_cmd
    assert "caffeinate" in launch_cmd
    mock_alert.assert_called_once()
    assert mock_alert.call_args.args[0] == "watchdog_restart"


def test_restart_bot_does_not_kill_when_pid_reused_by_other_process(mocker):
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog.cmdline_matches_run_bot", return_value=False)
    mock_kill = mocker.patch("monitoring.watchdog.os.kill")
    mocker.patch("monitoring.watchdog.subprocess.Popen")
    mocker.patch("monitoring.watchdog.fire_alert")
    mocker.patch("builtins.open", mocker.mock_open())

    watchdog.restart_bot("test reason", {"pid": 4242})

    mock_kill.assert_not_called()  # safety: never kill a PID that isn't run_bot.py


def test_restart_bot_launches_even_with_no_prior_status(mocker):
    mock_popen = mocker.patch("monitoring.watchdog.subprocess.Popen")
    mocker.patch("monitoring.watchdog.fire_alert")
    mocker.patch("builtins.open", mocker.mock_open())

    watchdog.restart_bot("no status file", None)

    mock_popen.assert_called_once()


def test_check_and_recover_restarts_when_process_dead(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=False)
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_called_once()
    assert result == "restarted:process_dead"


def test_check_and_recover_skips_when_recently_active(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog._log_quiet_for", return_value=False)
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_not_called()
    assert result == "healthy:recent_activity"


def test_check_and_recover_restarts_on_overdue_job(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog._log_quiet_for", return_value=True)
    mocker.patch("monitoring.watchdog.find_overdue_job", return_value="run_eod")
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_called_once()
    assert result == "restarted:overdue:run_eod"


def test_check_and_recover_restarts_on_stale_deploy(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog._log_quiet_for", return_value=True)
    mocker.patch("monitoring.watchdog.find_overdue_job", return_value=None)
    mocker.patch("monitoring.watchdog.get_git_commit", return_value="bbb")
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_called_once()
    assert result == "restarted:stale_deploy"


def test_check_and_recover_healthy_when_nothing_stale(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value={"pid": 4242, "commit": "aaa"})
    mocker.patch("monitoring.watchdog.is_process_alive", return_value=True)
    mocker.patch("monitoring.watchdog._log_quiet_for", return_value=True)
    mocker.patch("monitoring.watchdog.find_overdue_job", return_value=None)
    mocker.patch("monitoring.watchdog.get_git_commit", return_value="aaa")
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_not_called()
    assert result == "healthy:quiet_but_expected"


def test_check_and_recover_skips_when_no_status_file(mocker):
    mocker.patch("monitoring.watchdog.read_status_file", return_value=None)
    restart_spy = mocker.patch("monitoring.watchdog.restart_bot")

    result = watchdog.check_and_recover()

    restart_spy.assert_not_called()
    assert result == "no_status_file"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "trading bot" && pytest tests/test_watchdog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'monitoring.watchdog'`

- [ ] **Step 3: Write the implementation**

```python
"""trading bot/monitoring/watchdog.py

Active reliability watchdog for the trading bot. Runs as its OWN
LaunchAgent (see docs/RUNBOOK.md#watchdog), separate from both
run_bot.py and monitoring/dead_mans_switch.py.

Unlike the dead-man's switch (alert-only, 4h interval, only ever catches
a fully-dead process by the NEXT trading day's check), this runs every
~15 minutes and takes action: kills and relaunches the bot if it looks
crashed, wedged, or running stale code.

Built 2026-07-16 after the bot silently wedged for ~20 hours (job_runs
last row 07-15 22:30, not found until a human checked at 07-16 18:51) --
see CLAUDE-REFERENCE.md#history. Manual-restart-only was an explicit,
deliberate user decision on 2026-07-14 ("paper-trading, low stakes... an
acceptable tradeoff") -- reversed 2026-07-16 after this incident showed
"a human notices" doesn't bound downtime the way that decision assumed.

Safety: every restart path is gated on `_log_quiet_for()` -- bot.log
must be untouched for _QUIET_MINUTES before ANY action is taken, so a
legitimately long-running catch-up pipeline (observed to take ~10
minutes end-to-end) is never mistaken for a wedge. The only unconditional
check is process liveness (a dead PID cannot be "mid-job").
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from bot import db
from monitoring.alerts import fire_alert
from monitoring.logger import setup_logging
from monitoring.status_file import get_git_commit, read_status_file

log = logging.getLogger(__name__)

_NYSE = xcals.get_calendar("XNYS")
_AMSTERDAM = ZoneInfo("Europe/Amsterdam")
_REPO_DIR = Path(__file__).resolve().parent.parent
_LOG_FILE = _REPO_DIR / "bot.log"

_QUIET_MINUTES = 10   # bot.log must be untouched this long before we act on anything
_GRACE_MINUTES = 30   # minutes past a scheduled job's cron time before it's "overdue"

# (job_name, hour, minute) in Amsterdam time -- must match the cron times
# in orchestration/main_loop.py::start() for the three jobs that call
# db.record_job_run(). Update both places together if the schedule changes.
_EXPECTED_JOBS = [
    ("run_morning_pipeline", 15, 40),
    ("run_intraday_check", 20, 0),
    ("run_eod", 22, 30),
]


def _log_quiet_for(minutes: int, now: datetime, log_file: Path = _LOG_FILE) -> bool:
    if not log_file.exists():
        return True
    age = now - datetime.fromtimestamp(log_file.stat().st_mtime, tz=UTC)
    return age >= timedelta(minutes=minutes)


def find_overdue_job(now: datetime) -> str | None:
    """First expected job that's overdue today (Amsterdam time), or None."""
    now_local = now.astimezone(_AMSTERDAM)
    today = now_local.date().isoformat()
    if not _NYSE.is_session(today):
        return None
    for job_name, hour, minute in _EXPECTED_JOBS:
        deadline = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0) \
            + timedelta(minutes=_GRACE_MINUTES)
        if now_local < deadline:
            continue
        if not db.job_ran_today(job_name, today):
            return job_name
    return None


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_stale_deploy(status: dict | None, current_commit: str | None) -> bool:
    if status is None or current_commit is None:
        return False
    status_commit = status.get("commit")
    if status_commit is None:
        return False
    return status_commit != current_commit


def cmdline_matches_run_bot(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return "run_bot.py" in result.stdout
    except Exception:
        return False


def restart_bot(reason: str, status: dict | None) -> None:
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
            ["nohup", "caffeinate", "-i", "-s", "python3", "run_bot.py"],
            cwd=str(_REPO_DIR), stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    fire_alert(
        "watchdog_restart",
        f"Auto-restarted trading bot: {reason}",
        {"reason": reason, "old_pid": old_pid},
    )


def check_and_recover(now: datetime | None = None) -> str:
    """Run one watchdog cycle. Returns a short status string for logging/tests."""
    now = now or datetime.now(UTC)
    status = read_status_file()

    if status is None:
        log.warning("No status file found -- bot may not have started via the "
                    "watchdog-aware code path yet. Skipping this cycle.")
        return "no_status_file"

    if not is_process_alive(status["pid"]):
        restart_bot("process not alive", status)
        return "restarted:process_dead"

    if not _log_quiet_for(_QUIET_MINUTES, now):
        return "healthy:recent_activity"

    overdue = find_overdue_job(now)
    if overdue is not None:
        restart_bot(f"job '{overdue}' overdue by >{_GRACE_MINUTES}min", status)
        return f"restarted:overdue:{overdue}"

    if is_stale_deploy(status, get_git_commit(_REPO_DIR)):
        restart_bot("running commit does not match HEAD", status)
        return "restarted:stale_deploy"

    return "healthy:quiet_but_expected"


def main() -> int:
    setup_logging()
    result = check_and_recover()
    log.info("Watchdog cycle: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "trading bot" && pytest tests/test_watchdog.py -v`
Expected: all pass (~19 tests)

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add monitoring/watchdog.py tests/test_watchdog.py
git commit -m "feat: add reliability watchdog with auto-restart

Reverses the 2026-07-14 decision to stay alert-only (STATE.md
Decisions) -- that decision assumed a human would notice within hours;
the 2026-07-16 incident sat undetected for ~20h instead. Every restart
is gated on 10 minutes of log quiet so a legitimate long-running
catch-up pipeline is never mistaken for a wedge."
```

---

### Task 4: LaunchAgent for the watchdog

**Files:**
- Create: `~/Library/LaunchAgents/com.thomasvromen.tradingbot-watchdog.plist`

- [ ] **Step 1: Write the plist**

Mirror the existing `com.thomasvromen.tradingbot-deadmansswitch.plist` structure exactly (same `WorkingDirectory`, same `/opt/homebrew/bin/python3 -m` invocation style), with a 900s (15 min) interval instead of 14400s:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.thomasvromen.tradingbot-watchdog</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>-m</string>
        <string>monitoring.watchdog</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/thomasvromen/Documents/Claude code test/trading bot</string>

    <!-- Deliberately a SEPARATE process/LaunchAgent from both the main bot's
         com.thomasvromen.tradingbot.plist and the dead-man's-switch -- must
         keep working even if the main bot process is completely dead or
         wedged, which nothing running inside that process could detect
         about itself. See monitoring/watchdog.py's module docstring. -->

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

- [ ] **Step 2: Load it**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thomasvromen.tradingbot-watchdog.plist
```

- [ ] **Step 3: Verify it registered and ran once (RunAtLoad)**

```bash
launchctl list | grep tradingbot-watchdog
sleep 5
tail -20 "/Users/thomasvromen/Documents/Claude code test/trading bot/watchdog.log"
```

Expected: `launchctl list` shows the label with exit status `0`; `watchdog.log` shows one `Watchdog cycle: <result>` line. Since the bot doesn't have a status file yet (Task 2 not deployed to the *running* process until the next restart), expect `no_status_file` on this first run — that's correct, not a bug; it becomes `healthy:...` after the next bot restart (which will happen naturally, or can be forced once here to pick up Tasks 1-3's code).

- [ ] **Step 4: Restart the bot once so it picks up the status-file-writing code**

```bash
cd "trading bot"
PID=$(ps aux | grep '[r]un_bot.py' | awk '{print $2}')
kill "$PID"
sleep 3
nohup caffeinate -i -s python3 run_bot.py > bot.log 2>&1 &
disown
```

Then verify: `cat bot_status.json` shows a pid/commit/started_at, and `launchctl kickstart -k gui/$(id -u)/com.thomasvromen.tradingbot-watchdog` followed by `tail -5 watchdog.log` shows `healthy:...` instead of `no_status_file`.

- [ ] **Step 5: No commit needed** — plists outside the repo aren't tracked by git. Note the file path in `docs/RUNBOOK.md` (Task 5) so it's documented even though it isn't version-controlled — same pattern as the existing dead-man's-switch plist.

---

### Task 5: Documentation

**Files:**
- Modify: `trading bot/docs/RUNBOOK.md` (new `#watchdog` section, update `#sleep-wedges`)
- Modify: `docs/STATE.md` (`## Decisions`, `## Done`, `## Open items`)
- Modify: `trading bot/CLAUDE.md` (status banner)

- [ ] **Step 1: Add a `#watchdog` section to `RUNBOOK.md`**

Insert a new section after the existing `#dead-mans-switch` section (grep for `<a id="dead-mans-switch">` to find it, read that whole section first to match its structure/tone), documenting: what it checks (process alive, per-job staleness via `job_runs`, deploy freshness), the 10-minute quiet-gate safety mechanism, how to check its log (`watchdog.log`), how to disable it if it ever needs to be paused (`launchctl bootout gui/$(id -u)/com.thomasvromen.tradingbot-watchdog`), and that it's a policy reversal of the 2026-07-14 decision (link to `docs/STATE.md`'s Decisions entry).

- [ ] **Step 2: Update `#sleep-wedges` section**

Add a note that as of 2026-07-16: (a) Power Nap was disabled (`sudo pmset -a powernap 0`, run manually by the user — record whether it was actually run, don't assume) as a targeted mitigation for the "Sleep Service Back to Sleep" cycling pattern observed in `pmset -g log`, and (b) even if sleep wedges recur, the watchdog now bounds downtime to ~15-30 min instead of requiring a human to notice — so full `disablesleep` / laptop migration remain optional hardening, not urgent.

- [ ] **Step 3: Update `docs/STATE.md`**

Add to `## Decisions`:
```
- DECISION (2026-07-16, reverses 2026-07-14): built an active auto-restart
  watchdog (`monitoring/watchdog.py`) after the 2026-07-14 "manual restart
  is an acceptable tradeoff" decision let a wedge sit undetected ~20h.
  Every restart gated on 10 min of log quiet (never kills a legitimately
  running pipeline). See `trading bot/docs/RUNBOOK.md#watchdog`.
```

Add to `## Done` (dated entry describing what was built, mirroring the style of the existing 2026-07-16 entry already there from the restart earlier today).

Update `## Open items`'s sleep-wedge line to reflect: Power Nap mitigation applied (if user confirms they ran the command) + watchdog now bounds downtime regardless.

- [ ] **Step 4: Update `trading bot/CLAUDE.md` banner**

Per the repo's own convention ("After completing a review/remediation/strategy change worth recording -> append it to docs/CLAUDE-REFERENCE.md#history and update this banner's status line"): append a history entry to `docs/CLAUDE-REFERENCE.md#history` and update the banner's final line with the new test count after Task 1-3's tests are added.

- [ ] **Step 5: Commit**

```bash
cd "trading bot" && git add docs/RUNBOOK.md CLAUDE.md docs/CLAUDE-REFERENCE.md
cd .. && git add docs/STATE.md
git commit -m "docs: document the reliability watchdog and reverse the manual-restart-only decision"
```

---

### Task 6: Hand off the one step the agent cannot do

**This step is NOT executed by the agent** — `sudo` requires an interactive password prompt.

Tell the user to run, in their own terminal (or via `! sudo pmset -a powernap 0` in chat):

```bash
sudo pmset -a powernap 0
```

This targets the exact symptom observed in `pmset -g log` (`Entering Sleep state due to 'Sleep Service Back to Sleep'`) without the battery/heat cost of full `disablesleep`. Confirm afterward with `pmset -g custom | grep powernap` (should read `0` under both Battery Power and AC Power).

---

## Self-review notes (completed before handoff)

- **Spec coverage:** watchdog auto-restart (Task 3-4) ✓, deploy-staleness detection (Task 2-3) ✓, per-job staleness beyond just morning_pipeline (Task 1) ✓, safety guard against killing a live pipeline (quiet-gate in Task 3) ✓, safety guard against killing an unrelated PID (cmdline check in Task 3) ✓, docs/decision-reversal recorded (Task 5) ✓, sleep mitigation handed to user correctly since it needs sudo (Task 6) ✓.
- **Placeholder scan:** Step 6 of Task 2 intentionally contains a "figure this out from the fixture" instruction rather than fabricated fixture internals I haven't read — this is a deliberate exception, not a lazy placeholder: the `orch` fixture's exact construction mocks weren't in the context gathered for this plan, and inventing plausible-looking mock calls for it would risk a subtly wrong test that passes for the wrong reason. Every other step has complete, runnable code.
- **Type consistency:** `check_and_recover(now: datetime | None = None) -> str`, `restart_bot(reason: str, status: dict | None) -> None`, `find_overdue_job(now: datetime) -> str | None` — consistent across the implementation and every test.
