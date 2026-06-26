from unittest.mock import MagicMock
from alpaca.trading.enums import (
    OrderStatus as AlpacaOrderStatus,
    OrderType as AlpacaOrderType,
    OrderSide as AlpacaSide,
)
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
        status=AlpacaOrderStatus.FILLED, filled_qty="10", filled_avg_price="151.23",
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
        status=AlpacaOrderStatus.NEW, filled_qty="0", filled_avg_price="0",
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
        status=AlpacaOrderStatus.REJECTED, filled_qty="0", filled_avg_price="0",
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
        id="order-999", symbol="MSFT", side=AlpacaSide.BUY, qty="5",
        order_type=AlpacaOrderType.MARKET, status=AlpacaOrderStatus.FILLED,
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


def test_place_order_fires_alert_when_fill_poll_times_out(monkeypatch):
    """If the order never reaches a terminal status within the poll retries,
    the caller falls back to the pre-trade quote price. That fallback must not
    be silent — an alert should fire so a human knows the recorded price may
    not reflect the real fill."""
    import time as time_module
    monkeypatch.setattr(time_module, "sleep", lambda *_a, **_kw: None)

    mock_api = MagicMock()
    mock_api.submit_order.return_value.id = "order-timeout-1"
    still_pending = MagicMock(
        status=AlpacaOrderStatus.NEW, filled_qty="0", filled_avg_price="0", filled_at=None,
    )
    mock_api.get_order_by_id.return_value = still_pending

    # fire_alert is imported into monitoring.logger; patch at that binding
    # (matches the pattern used in tests/test_portfolio.py).
    mock_fire_alert = MagicMock()
    monkeypatch.setattr("monitoring.logger.fire_alert", mock_fire_alert)

    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)

    # Order never reached a terminal state — stays SUBMITTED (existing behavior).
    assert order.status == OrderStatus.SUBMITTED
    # But the timeout fallback must now be flagged.
    mock_fire_alert.assert_called_once()


# ------------------------------------------------------------------
# Stop order cancellation — uses REAL alpaca enums (not raw strings),
# because str(OrderType.STOP) == "OrderType.STOP", not "stop".
# ------------------------------------------------------------------

def _alpaca_stop_order(symbol, stop_price, qty, order_id, status):
    """Build a mock Alpaca order carrying REAL enum instances for type/status."""
    o = MagicMock()
    o.symbol = symbol
    o.order_type = AlpacaOrderType.STOP
    o.status = status
    o.stop_price = stop_price
    o.qty = qty
    o.id = order_id
    return o


def test_get_stop_orders_matches_real_enum_orders():
    """get_stop_orders must recognise resting stops whose order_type/status are
    real alpaca enums (OrderType.STOP / OrderStatus.NEW), not the literal
    strings 'stop'/'new'. With the old str()=='stop' filter this returned {}."""
    o = _alpaca_stop_order("AAPL", stop_price=90.0, qty=10.0,
                           order_id="stop-1", status=AlpacaOrderStatus.NEW)
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [o]
    broker = AlpacaBroker(api_client=mock_api)

    stops = broker.get_stop_orders()
    assert "AAPL" in stops
    assert stops["AAPL"][0] == 90.0
    assert stops["AAPL"][1] == 10.0
    assert stops["AAPL"][2] == "stop-1"  # order id exposed


def test_cancel_stop_order_by_id_targets_only_that_order():
    """When given an order_id, cancel_stop_order must cancel ONLY that stop,
    leaving any other resting stop for the same ticker (e.g. the just-placed
    new trail-up stop) untouched."""
    old = _alpaca_stop_order("AAPL", 90.0, 10.0, "old-stop", AlpacaOrderStatus.NEW)
    new = _alpaca_stop_order("AAPL", 102.0, 10.0, "new-stop", AlpacaOrderStatus.NEW)
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [old, new]
    broker = AlpacaBroker(api_client=mock_api)

    broker.cancel_stop_order("AAPL", order_id="old-stop")

    mock_api.cancel_order_by_id.assert_called_once_with("old-stop")


def test_cancel_stop_order_no_id_cancels_all_resting_stops():
    """With no order_id (full close / reduce path), cancel_stop_order cancels
    every resting stop for the ticker."""
    s = _alpaca_stop_order("AAPL", 90.0, 10.0, "stop-1", AlpacaOrderStatus.NEW)
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [s]
    broker = AlpacaBroker(api_client=mock_api)

    broker.cancel_stop_order("AAPL")

    mock_api.cancel_order_by_id.assert_called_once_with("stop-1")


def test_cancel_stop_order_ignores_non_stop_and_terminal_orders():
    """Only open (new/accepted) STOP orders are cancellable; filled/other-type
    orders must be left alone."""
    filled_stop = _alpaca_stop_order("AAPL", 90.0, 10.0, "filled", AlpacaOrderStatus.FILLED)
    market = MagicMock()
    market.symbol = "AAPL"
    market.order_type = AlpacaOrderType.MARKET
    market.status = AlpacaOrderStatus.NEW
    market.id = "market-1"
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [filled_stop, market]
    broker = AlpacaBroker(api_client=mock_api)

    broker.cancel_stop_order("AAPL")

    mock_api.cancel_order_by_id.assert_not_called()
