"""Active reliability watchdog for the trading bot. Runs as its OWN
LaunchAgent (see docs/RUNBOOK.md#watchdog), separate from both run_bot.py
and monitoring/dead_mans_switch.py.

Unlike the dead-man's switch (alert-only, 4h interval, only ever catches a
fully-dead process by the NEXT trading day's check), this runs every ~15
minutes and takes action: kills and relaunches the bot if it looks crashed,
wedged, or running stale code.

Built 2026-07-16 after the bot silently wedged for ~20 hours (job_runs
last row 07-15 22:30, not found until a human checked at 07-16 18:51) --
see docs/CLAUDE-REFERENCE.md#history. Manual-restart-only was an explicit,
deliberate user decision on 2026-07-14 ("paper-trading, low stakes... an
acceptable tradeoff") -- reversed 2026-07-16 after this incident showed
"a human notices" doesn't bound downtime the way that decision assumed.

Safety: every restart path is gated on `_log_quiet_for()` -- bot.log must
be untouched for _QUIET_MINUTES before ANY action is taken, so a
legitimately long-running catch-up pipeline (observed to take ~10 minutes
end-to-end) is never mistaken for a wedge. The only unconditional check is
process liveness (a dead PID cannot be "mid-job").
"""
from __future__ import annotations

import json
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
_HARD_GRACE_MINUTES = 120  # absolute ceiling that bypasses the quiet-gate entirely

_RESTART_HISTORY_FILE = _REPO_DIR / "watchdog_restart_history.json"
_CRASH_LOOP_WINDOW_MINUTES = 60
_CRASH_LOOP_THRESHOLD = 3

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
        # sys.executable, not the bare "python3" string: a LaunchAgent's
        # subprocess PATH is minimal (no /opt/homebrew/bin) and resolved
        # "python3" to the system CommandLineTools 3.9 interpreter instead
        # of the Homebrew 3.11+ one this project requires (datetime.UTC) --
        # live-observed 2026-07-17 crashing every auto-restart with
        # ImportError. sys.executable is the watchdog's own interpreter,
        # which is already known-correct (hardcoded absolute path in this
        # LaunchAgent's own plist).
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


def check_and_recover(now: datetime | None = None) -> str:
    """Run one watchdog cycle. Returns a short status string for logging/tests."""
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
        # Bypasses the quiet-gate entirely -- something has been stuck for
        # far longer than any legitimate pipeline run should ever take,
        # regardless of whether it's still producing log output (e.g. an
        # infinite retry loop that never completes the actual job).
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


if __name__ == "__main__":
    sys.exit(main())
