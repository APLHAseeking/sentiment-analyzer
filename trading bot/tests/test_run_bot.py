"""Tests for run_bot._make_broker — paper-mode safety guard."""
from unittest.mock import MagicMock, patch

import pytest

from run_bot import _make_broker


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
