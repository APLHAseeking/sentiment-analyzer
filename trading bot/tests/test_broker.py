from unittest.mock import MagicMock
from bot.broker import AlpacaBroker
from execution.broker_interface import BrokerInterface, OrderStatus


def test_alpaca_broker_implements_interface():
    assert issubclass(AlpacaBroker, BrokerInterface)


def test_is_paper():
    broker = AlpacaBroker(api_client=MagicMock())
    assert broker.is_paper is True


def test_get_cash():
    mock_api = MagicMock()
    mock_api.get_account.return_value = MagicMock(cash="50000.00")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.get_cash() == 50_000.0


def test_get_equity():
    mock_api = MagicMock()
    mock_api.get_account.return_value = MagicMock(equity="120000.00")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.get_equity() == 120_000.0


def test_get_positions():
    mock_pos = MagicMock()
    mock_pos.symbol = "AAPL"
    mock_pos.qty = "10"
    mock_pos.current_price = "150.00"
    mock_pos.avg_entry_price = "140.00"
    mock_api = MagicMock()
    mock_api.get_all_positions.return_value = [mock_pos]
    broker = AlpacaBroker(api_client=mock_api)
    positions = broker.get_positions()
    assert positions == [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 150.0, "avg_entry_price": 140.0,
    }]


def test_place_order_buy_returns_order():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)
    mock_api.submit_order.assert_called_once()
    assert order.ticker == "AAPL"
    assert order.status == OrderStatus.PENDING


def test_place_order_sell():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="sell", qty=5.0)
    assert order.ticker == "AAPL"


def test_cancel_order_success():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    result = broker.cancel_order("test-id")
    assert result is True
    mock_api.cancel_order_by_id.assert_called_once_with("test-id")


def test_cancel_order_failure_returns_false():
    mock_api = MagicMock()
    mock_api.cancel_order_by_id.side_effect = Exception("not found")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.cancel_order("bad-id") is False


def test_get_order_history_returns_empty_list():
    broker = AlpacaBroker(api_client=MagicMock())
    assert broker.get_order_history() == []
