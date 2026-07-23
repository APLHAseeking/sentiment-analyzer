"""Background heartbeat + thread-stack diagnostic for the live scheduler.

Runs as a daemon thread INSIDE the main bot process -- no new process, no
launchd/cron/System-Settings surface (see docs/STATE.md's 2026-07-21
constraint against further daemon/permission changes for the scheduler-wedge
topic). Exists to catch the next occurrence of the recurring scheduler
wedge (see docs/CLAUDE-REFERENCE.md#history) with real evidence instead of a
silent bot.log gap: the 2026-07-22 occurrence held its `caffeinate`
sleep-prevention assertion the whole time (confirmed via `pmset -g log`),
ruling out system sleep for at least that one instance -- the actual
mechanism is still unknown.

Logs a heartbeat line and overwrites `dump_file` with a full stack trace of
every thread on each tick (`faulthandler.dump_traceback`), so whenever the
gap is next noticed, the last dump written before it resumes shows exactly
what every thread -- including the scheduler's own -- was doing when it
stalled.
"""
from __future__ import annotations

import faulthandler
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DUMP_FILE = _REPO_DIR / "bot_threaddump.log"
_DEFAULT_INTERVAL_SEC = 300.0  # 5 min


def _heartbeat_loop(stop_event: threading.Event, interval_sec: float, dump_file: Path) -> None:
    while not stop_event.is_set():
        log.info("heartbeat: alive, %d thread(s)", threading.active_count())
        try:
            with dump_file.open("w") as f:
                faulthandler.dump_traceback(file=f, all_threads=True)
        except Exception as exc:
            log.warning("heartbeat: could not write thread dump to %s: %s", dump_file, exc)
        stop_event.wait(interval_sec)


def start_heartbeat(
    interval_sec: float = _DEFAULT_INTERVAL_SEC,
    dump_file: Path = _DEFAULT_DUMP_FILE,
) -> threading.Event:
    """Start the heartbeat daemon thread. Returns the stop Event; set it to
    stop the thread early (production never calls this -- it runs for the
    life of the process; tests use it to clean up)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(stop_event, interval_sec, dump_file),
        name="heartbeat",
        daemon=True,
    )
    thread.start()
    return stop_event
