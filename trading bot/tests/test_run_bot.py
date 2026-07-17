"""Tests for run_bot._make_broker — paper-mode safety guard — and for
main()'s CLI dispatch / run_paper()'s call sequencing."""
from unittest.mock import MagicMock, call, patch

import pytest

import run_bot
from run_bot import _make_broker, main, run_paper


def test_make_broker_simulated_returns_simulated_broker():
    broker = _make_broker(simulated=True)
    from execution.paper_broker import SimulatedBroker
    assert isinstance(broker, SimulatedBroker)


def test_make_broker_live_raises_if_not_paper():
    """If AlpacaBroker.is_paper is False, _make_broker must refuse to return it."""
    fake_broker = MagicMock()
    fake_broker.is_paper = False
    with patch("bot.broker.AlpacaBroker", return_value=fake_broker):
        with pytest.raises(RuntimeError, match="paper"):
            _make_broker(simulated=False)


def test_make_broker_live_returns_broker_if_paper():
    fake_broker = MagicMock()
    fake_broker.is_paper = True
    with patch("bot.broker.AlpacaBroker", return_value=fake_broker):
        result = _make_broker(simulated=False)
    assert result is fake_broker


def test_main_test_alerts_fires_alert_and_returns_early(monkeypatch):
    """--test-alerts should fire the alert, print a confirmation, and return
    before ever calling run_paper() or run_backtest()."""
    monkeypatch.setattr("sys.argv", ["run_bot.py", "--test-alerts"])
    with patch("monitoring.alerts.fire_alert") as mock_fire_alert, \
         patch("run_bot.run_paper") as mock_run_paper, \
         patch("run_bot.run_backtest") as mock_run_backtest:
        main()

    mock_fire_alert.assert_called_once_with(
        "startup",
        "Test alert — trading bot alert pipeline is configured correctly",
        {"test": True},
    )
    mock_run_paper.assert_not_called()
    mock_run_backtest.assert_not_called()


def test_main_backtest_calls_run_backtest_not_run_paper(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_bot.py", "--backtest"])
    with patch("run_bot.run_paper") as mock_run_paper, \
         patch("run_bot.run_backtest") as mock_run_backtest:
        main()

    mock_run_backtest.assert_called_once_with()
    mock_run_paper.assert_not_called()


def test_main_simulated_calls_run_paper_simulated_true(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_bot.py", "--simulated"])
    with patch("run_bot.run_paper") as mock_run_paper, \
         patch("run_bot.run_backtest") as mock_run_backtest:
        main()

    mock_run_paper.assert_called_once_with(simulated=True)
    mock_run_backtest.assert_not_called()


def test_main_no_flags_calls_run_paper_simulated_false(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_bot.py"])
    with patch("run_bot.run_paper") as mock_run_paper, \
         patch("run_bot.run_backtest") as mock_run_backtest:
        main()

    mock_run_paper.assert_called_once_with(simulated=False)
    mock_run_backtest.assert_not_called()


def test_run_paper_calls_sequence_simulated():
    """init_db -> refresh_universe -> _make_broker(simulated=...) ->
    orchestrator.initialize(broker) -> orchestrator.start(), in that order."""
    mock_orchestrator = MagicMock()
    manager = MagicMock()
    manager.attach_mock(mock_orchestrator, "orchestrator")

    with patch("run_bot.init_db") as mock_init_db, \
         patch("run_bot.refresh_universe") as mock_refresh_universe, \
         patch("run_bot._make_broker") as mock_make_broker, \
         patch("orchestration.main_loop.RegimeAwareOrchestrator",
               return_value=mock_orchestrator) as mock_orchestrator_cls:
        manager.attach_mock(mock_init_db, "init_db")
        manager.attach_mock(mock_refresh_universe, "refresh_universe")
        manager.attach_mock(mock_make_broker, "make_broker")

        fake_broker = MagicMock()
        mock_make_broker.return_value = fake_broker

        run_paper(simulated=True)

    mock_init_db.assert_called_once_with()
    mock_refresh_universe.assert_called_once_with()
    mock_make_broker.assert_called_once_with(True)
    mock_orchestrator_cls.assert_called_once()
    mock_orchestrator.initialize.assert_called_once_with(fake_broker)
    mock_orchestrator.start.assert_called_once_with()

    # Verify ordering: init_db -> refresh_universe -> make_broker, before
    # orchestrator.initialize -> orchestrator.start.
    assert manager.mock_calls.index(call.init_db()) < \
        manager.mock_calls.index(call.refresh_universe())
    assert manager.mock_calls.index(call.refresh_universe()) < \
        manager.mock_calls.index(call.make_broker(True))
    assert manager.mock_calls.index(call.orchestrator.initialize(fake_broker)) < \
        manager.mock_calls.index(call.orchestrator.start())
