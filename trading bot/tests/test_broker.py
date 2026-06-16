from unittest.mock import MagicMock
from bot.broker import AlpacaBroker
from execution.broker_interface import BrokerInterface, Order, OrderSide, OrderStatus, OrderType


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
    assert order.status == OrderStatus.SUBMITTED


def test_place_order_sell():
    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="sell", qty=5.0)
    assert order.ticker == "AAPL"
    assert order.status == OrderStatus.SUBMITTED
    submitted_req = mock_api.submit_order.call_args[0][0]
    from alpaca.trading.enums import OrderSide as AlpacaSide
    assert submitted_req.side == AlpacaSide.SELL


def test_place_order_rejected_when_api_fails():
    mock_api = MagicMock()
    mock_api.submit_order.side_effect = Exception("insufficient funds")
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=999_999.0)
    assert order.status == OrderStatus.REJECTED
    assert "insufficient funds" in order.reject_reason


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


def test_get_order_history_returns_empty_list_when_no_orders():
    mock_api = MagicMock()
    mock_api.get_orders.return_value = []
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.get_order_history() == []


def test_place_order_polls_for_fill_and_updates_order():
    mock_api = MagicMock()
    mock_api.submit_order.return_value.id = "order-123"
    filled_response = MagicMock(
        status="filled", filled_qty="10", filled_avg_price="151.23",
        filled_at="2026-06-15T14:30:00+00:00",
    )
    mock_api.get_order_by_id.return_value = filled_response

    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)

    mock_api.get_order_by_id.assert_called_with("order-123")
    assert order.order_id == "order-123"
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 10.0
    assert order.filled_avg_price == 151.23
    assert order.filled_at == "2026-06-15T14:30:00+00:00"


def test_place_order_stays_submitted_if_not_yet_filled():
    mock_api = MagicMock()
    mock_api.submit_order.return_value.id = "order-456"
    pending_response = MagicMock(
        status="new", filled_qty="0", filled_avg_price="0",
        filled_at=None,
    )
    mock_api.get_order_by_id.return_value = pending_response

    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)

    assert order.status == OrderStatus.SUBMITTED
    assert order.filled_qty == 0.0
    assert order.filled_avg_price == 0.0


def test_place_order_marks_rejected_when_broker_rejects_after_submit():
    mock_api = MagicMock()
    mock_api.submit_order.return_value.id = "order-789"
    rejected_response = MagicMock(
        status="rejected", filled_qty="0", filled_avg_price="0",
        filled_at=None, reject_reason="insufficient buying power",
    )
    mock_api.get_order_by_id.return_value = rejected_response

    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)

    assert order.status == OrderStatus.REJECTED
    assert order.reject_reason == "insufficient buying power"


def test_get_order_history_returns_orders_from_api():
    mock_api = MagicMock()
    api_order = MagicMock(
        id="order-999", symbol="MSFT", side="buy", qty="5",
        order_type="market", status="filled",
        filled_qty="5", filled_avg_price="305.10",
        filled_at="2026-06-14T15:00:00+00:00",
    )
    mock_api.get_orders.return_value = [api_order]

    broker = AlpacaBroker(api_client=mock_api)
    history = broker.get_order_history()

    assert len(history) == 1
    order = history[0]
    assert order.ticker == "MSFT"
    assert order.side == OrderSide.BUY
    assert order.qty == 5.0
    assert order.order_id == "order-999"
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 5.0
    assert order.filled_avg_price == 305.10
    assert order.filled_at == "2026-06-14T15:00:00+00:00"


def test_get_order_history_returns_empty_list_on_api_failure():
    mock_api = MagicMock()
    mock_api.get_orders.side_effect = Exception("network error")
    broker = AlpacaBroker(api_client=mock_api)
    assert broker.get_order_history() == []
