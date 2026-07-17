"""Independent staleness check for the trading bot's pipeline activity.

Must run as its OWN scheduled process (see docs/RUNBOOK.md#dead-mans-switch),
separate from run_bot.py's APScheduler. The entire point is to detect the bot
process being completely dead (crashed, never restarted) -- nothing inside
that same process could ever notice its own death. Confirmed gap: the bot
sat fully dead 2026-07-10 through 2026-07-13 (zero job_runs rows, zero
dashboard.log activity) with no alert of any kind; only a human check
happened to catch it.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import exchange_calendars as xcals

from bot import db
from monitoring.alerts import fire_alert
from monitoring.logger import setup_logging

log = logging.getLogger(__name__)
_NYSE = xcals.get_calendar("XNYS")
_WATCHDOG_LOG = Path(__file__).resolve().parent.parent / "watchdog.log"
_WATCHDOG_STALE_MINUTES = 40  # ~2.5x the watchdog's own 15-min StartInterval


def _last_completed_session_before(when: datetime) -> str | None:
    """ISO date of the most recent NYSE session strictly before `when`'s
    date, or None if the calendar has no sessions in the lookback window."""
    sessions = _NYSE.sessions_in_range(
        (when.date() - timedelta(days=14)).isoformat(),
        when.date().isoformat(),
    )
    sessions = [s for s in sessions if s.date() < when.date()]
    if not sessions:
        return None
    return sessions[-1].date().isoformat()


def check_pipeline_freshness(now: datetime | None = None) -> bool:
    """Return True if pipeline activity looks healthy, False if stale.

    Fires a CRITICAL alert on staleness. "Stale" means no job_runs row
    exists for the most recently completed NYSE session as of `now` -- i.e.
    the bot has gone at least one full extra trading day without a single
    recorded run. Weekends/holidays never trigger a false alarm since
    _last_completed_session_before only counts real trading sessions.
    """
    now = now or datetime.now(UTC)
    last_session = _last_completed_session_before(now)
    if last_session is None:
        log.warning("No prior NYSE session found in lookback window — skipping check")
        return True  # can't evaluate; don't false-alarm on a calendar-data gap

    try:
        last_run = db.get_last_job_run_date()
    except Exception as exc:
        fire_alert(
            "dead_mans_switch",
            f"Could not query job_runs to check pipeline health: {exc}",
            {"error": str(exc)},
        )
        return False

    if last_run is not None and last_run >= last_session:
        log.info("Pipeline healthy: last run %s >= expected %s", last_run, last_session)
        return True

    fire_alert(
        "dead_mans_switch",
        f"No completed pipeline run recorded since {last_run or 'ever'} — "
        f"expected at least one by {last_session}. The bot process may be "
        f"dead or stuck.",
        {"last_run": last_run, "expected_by": last_session},
    )
    return False


def check_watchdog_freshness(now: datetime | None = None) -> bool:
    """Return True if the reliability watchdog (monitoring/watchdog.py)
    itself looks alive, False if it appears to have stopped running
    entirely. This is the layer that catches a bug IN the watchdog --
    nothing inside that process can be trusted to detect its own failure,
    same reasoning as check_pipeline_freshness above but one level up.
    """
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


def main() -> int:
    setup_logging()
    pipeline_healthy = check_pipeline_freshness()
    watchdog_healthy = check_watchdog_freshness()
    return 0 if (pipeline_healthy and watchdog_healthy) else 1


if __name__ == "__main__":
    sys.exit(main())
