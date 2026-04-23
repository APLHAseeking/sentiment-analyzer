import pytest
from bot.portfolio import Portfolio

MAX_POSITIONS = 20

@pytest.fixture
def portfolio(db, mock_broker):
    return Portfolio(broker=mock_broker)

def test_initial_cash(portfolio, mock_broker):
    assert portfolio.get_cash() == 100_000.0

def test_can_open_when_under_limit(portfolio, mock_broker):
    mock_broker.get_positions.return_value = []
    assert portfolio.can_open_new_position() is True

def test_cannot_open_at_max_positions(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [
        {"ticker": f"T{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
        for i in range(MAX_POSITIONS)
    ]
    assert portfolio.can_open_new_position() is False

def test_cannot_open_after_daily_limit(portfolio, mock_broker):
    portfolio._opened_today = 3
    assert portfolio.can_open_new_position() is False

def test_open_position_places_order(portfolio, mock_broker):
    portfolio.open_position("AAPL", position_pct=5.0, signal_id=1,
                            rationale="Test", entry_price=150.0)
    mock_broker.place_order.assert_called_once()
    kwargs = mock_broker.place_order.call_args[1]
    assert kwargs["ticker"] == "AAPL"
    assert kwargs["side"] == "buy"

def test_open_position_caps_at_max_pct(portfolio, mock_broker):
    portfolio.open_position("AAPL", position_pct=15.0, signal_id=1,
                            rationale="Test", entry_price=100.0)
    kwargs = mock_broker.place_order.call_args[1]
    expected_shares = 100_000.0 * (8.0 / 100) / 100.0
    assert kwargs["qty"] == pytest.approx(expected_shares)

def test_stop_loss_triggers(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 33.0,
        "current_price": 100.0, "avg_entry_price": 120.0,
    }]
    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    assert "AAPL" in closed
    mock_broker.place_order.assert_called_with(ticker="AAPL", side="sell", qty=33.0)

def test_stop_loss_does_not_trigger_within_threshold(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 110.0, "avg_entry_price": 120.0,
    }]
    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    assert closed == []
