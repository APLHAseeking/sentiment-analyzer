"""Tests for RegimeAwareOrchestrator — invested-pct capacity gate."""
from unittest.mock import MagicMock
import pytest
from orchestration.main_loop import RegimeAwareOrchestrator


def _mock_broker(cash: float, position_value: float) -> MagicMock:
    broker = MagicMock()
    broker.get_cash.return_value = cash
    broker.get_equity.return_value = cash + position_value
    if position_value > 0:
        broker.get_positions.return_value = [
            {"ticker": "SPY", "qty": 1, "current_price": position_value}
        ]
    else:
        broker.get_positions.return_value = []
    return broker


@pytest.fixture
def orch(mocker):
    mocker.patch("orchestration.main_loop._NYSE.is_session", return_value=True)
    mocker.patch("orchestration.main_loop.get_regime_data", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.run_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])

    from system.config import settings
    o = RegimeAwareOrchestrator(settings)
    o._portfolio = MagicMock()
    o._risk = MagicMock()
    o._store = MagicMock()
    o._market_data = MagicMock()
    o._regime_state = None
    o._engine = MagicMock()
    o._engine.is_fitted = False
    return o


def test_pipeline_skips_entries_when_at_capacity(mocker, orch):
    orch._broker = _mock_broker(cash=15_000, position_value=85_000)  # 85% invested
    process_spy = mocker.patch.object(orch, "_process_signal")
    fundamental_spy = mocker.patch.object(orch, "_process_fundamental_candidate")

    orch.run_morning_pipeline()

    process_spy.assert_not_called()
    fundamental_spy.assert_not_called()
    orch._portfolio.enforce_stop_losses.assert_called_once()


def test_pipeline_enforces_stop_losses_even_at_capacity(mocker, orch):
    orch._broker = _mock_broker(cash=5_000, position_value=95_000)  # 95% invested
    mocker.patch.object(orch, "_process_signal")

    orch.run_morning_pipeline()

    orch._portfolio.enforce_stop_losses.assert_called_once()
    orch._portfolio.enforce_take_profits.assert_called_once()
