from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

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
