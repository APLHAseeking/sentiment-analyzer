"""Tests for the independent pipeline-staleness check (monitoring/dead_mans_switch.py).

This check must run as its own separate process from run_bot.py -- see the
module docstring for why. These tests exercise only its logic (job_runs
staleness vs. the NYSE calendar), not the process-supervision side.
"""
from datetime import datetime, UTC

from monitoring.dead_mans_switch import check_pipeline_freshness

# Wednesday, with Tuesday 2026-07-14 as the prior trading session (both
# ordinary weekdays, no US market holiday between them).
_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
_LAST_SESSION = "2026-07-14"


def test_healthy_when_job_ran_for_last_session(db, mocker):
    db.record_job_run("run_morning_pipeline", _LAST_SESSION)
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")

    assert check_pipeline_freshness(now=_NOW) is True
    alert_spy.assert_not_called()


def test_stale_when_no_job_ever_ran_fires_alert(db, mocker):
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")

    assert check_pipeline_freshness(now=_NOW) is False
    alert_spy.assert_called_once()
    assert alert_spy.call_args[0][0] == "dead_mans_switch"
    assert alert_spy.call_args[0][2]["last_run"] is None


def test_stale_when_last_run_predates_expected_session_fires_alert(db, mocker):
    """The exact 2026-07-10..07-13 incident: the last recorded run is days
    older than the most recent session that should have completed."""
    db.record_job_run("run_eod", "2026-07-10")
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")

    assert check_pipeline_freshness(now=_NOW) is False
    alert_spy.assert_called_once()
    assert alert_spy.call_args[0][2]["last_run"] == "2026-07-10"
    assert alert_spy.call_args[0][2]["expected_by"] == _LAST_SESSION


def test_healthy_when_last_run_is_after_expected_session(db, mocker):
    """A run more recent than the minimum-expected session is still healthy
    (e.g. a same-day second window already completed)."""
    db.record_job_run("run_morning_pipeline", "2026-07-15")
    alert_spy = mocker.patch("monitoring.dead_mans_switch.fire_alert")

    assert check_pipeline_freshness(now=_NOW) is True
    alert_spy.assert_not_called()
