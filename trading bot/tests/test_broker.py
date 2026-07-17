from unittest.mock import MagicMock
import pytest
from alpaca.trading.enums import (
    OrderStatus as AlpacaOrderStatus,
    OrderType as AlpacaOrderType,
    OrderSide as AlpacaSide,
)
from bot.broker import AlpacaBroker, _apply_request_timeout, _ALPACA_TIMEOUT_SECONDS
from execution.broker_interface import BrokerInterface, Order, OrderSide, OrderStatus, OrderType


def test_alpaca_broker_implements_interface():
    assert issubclass(AlpacaBroker, BrokerInterface)


def test_apply_request_timeout_defaults_timeout_when_absent():
    """alpaca-py's RESTClient exposes no timeout param and its bare
    requests.Session() has none set — a stalled call blocks forever (same
    fix class as the yfinance hang, market_data/yf_session.py). Patching
    session.request must default one in when the caller didn't pass one."""
    fake_client = MagicMock()
    original_request = fake_client._session.request
    _apply_request_timeout(fake_client)
    fake_client._session.request("GET", "https://paper-api.alpaca.markets/v2/account")
    original_request.assert_called_once_with(
        "GET", "https://paper-api.alpaca.markets/v2/account", timeout=_ALPACA_TIMEOUT_SECONDS
    )


def test_apply_request_timeout_does_not_override_explicit_timeout():
    fake_client = MagicMock()
    original_request = fake_client._session.request
    _apply_request_timeout(fake_client)
    fake_client._session.request("GET", "https://x", timeout=5)
    original_request.assert_called_once_with("GET", "https://x", timeout=5)


def test_alpaca_broker_real_construction_applies_timeout_patch(monkeypatch):
    """The real (non-injected) construction branch — used in production —
    must wire the timeout patch onto the client it builds."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    broker = AlpacaBroker()
    assert broker._api._session.request.__name__ == "_request_with_timeout"


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


def test_place_order_buy_returns_order(monkeypatch):
    import time as time_module
    monkeypatch.setattr(time_module, "sleep", lambda *_a, **_kw: None)

    mock_api = MagicMock()
    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)
    mock_api.submit_order.assert_called_once()
    assert order.ticker == "AAPL"
    assert order.status == OrderStatus.SUBMITTED


def test_place_order_sell(monkeypatch):
    import time as time_module
    monkeypatch.setattr(time_module, "sleep", lambda *_a, **_kw: None)

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


def test_place_order_stays_submitted_if_not_yet_filled(monkeypatch):
    import time as time_module
    monkeypatch.setattr(time_module, "sleep", lambda *_a, **_kw: None)

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


def test_place_order_confirms_fill_that_lands_a_few_seconds_late(monkeypatch):
    """CF and VTRS (2026-07-07) were booked as phantom positions because the
    poll window was only 3 attempts / 0.2s apart (~0.4s total) — nowhere near
    enough for Alpaca's paper API to confirm a market-order fill, so the order
    was treated as unconfirmed and the caller fell back to a stale quote
    price. The poll must keep retrying long enough to catch a fill that
    confirms a few seconds (several polls) after submission, not just an
    instant one."""
    import time as time_module
    monkeypatch.setattr(time_module, "sleep", lambda *_a, **_kw: None)

    mock_api = MagicMock()
    mock_api.submit_order.return_value.id = "order-slow-fill"
    pending = MagicMock(
        status=AlpacaOrderStatus.NEW, filled_qty="0", filled_avg_price="0", filled_at=None,
    )
    filled = MagicMock(
        status=AlpacaOrderStatus.FILLED, filled_qty="10", filled_avg_price="151.00",
        filled_at="2026-07-09T14:00:05+00:00",
    )
    # 5 pending polls before the 6th confirms FILLED — beyond the old 3-attempt window.
    mock_api.get_order_by_id.side_effect = [pending] * 5 + [filled]

    broker = AlpacaBroker(api_client=mock_api)
    order = broker.place_order(ticker="AAPL", side="buy", qty=10.0)

    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == 151.00


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


def test_place_stop_order_uses_day_for_fractional_qty():
    """Alpaca rejects fractional-share orders submitted GTC ('fractional
    orders must be DAY orders') — NAV-based sizing routinely produces
    fractional qty, so a fractional qty must go out as DAY, not GTC."""
    mock_api = MagicMock()
    mock_api.submit_order.return_value = MagicMock(id="stop-frac")
    broker = AlpacaBroker(api_client=mock_api)

    order_id = broker.place_stop_order("AAPL", qty=7.34389171119657, stop_price=100.0)

    assert order_id == "stop-frac"
    sent_req = mock_api.submit_order.call_args[0][0]
    from alpaca.trading.enums import TimeInForce
    assert sent_req.time_in_force == TimeInForce.DAY


def test_place_stop_order_uses_gtc_for_whole_share_qty():
    """Whole-share qty has no fractional-order restriction — keep GTC so the
    stop rests indefinitely instead of expiring at end of session."""
    mock_api = MagicMock()
    mock_api.submit_order.return_value = MagicMock(id="stop-whole")
    broker = AlpacaBroker(api_client=mock_api)

    order_id = broker.place_stop_order("AAPL", qty=10.0, stop_price=100.0)

    assert order_id == "stop-whole"
    sent_req = mock_api.submit_order.call_args[0][0]
    from alpaca.trading.enums import TimeInForce
    assert sent_req.time_in_force == TimeInForce.GTC


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


# ------------------------------------------------------------------
# Short-selling support: account-level shorting_enabled, per-symbol
# is_shortable, and place_stop_order's new `side` param (buy-to-cover
# for shorts vs. the existing default sell-stop for longs).
# ------------------------------------------------------------------

@pytest.fixture
def mock_api():
    return MagicMock()


@pytest.fixture
def broker(mock_api):
    return AlpacaBroker(api_client=mock_api)


def test_shorting_enabled_true(broker, mock_api):
    mock_api.get_account.return_value.shorting_enabled = True
    assert broker.shorting_enabled() is True


def test_shorting_enabled_false(broker, mock_api):
    mock_api.get_account.return_value.shorting_enabled = False
    assert broker.shorting_enabled() is False


def test_is_shortable_true(broker, mock_api):
    mock_api.get_asset.return_value.shortable = True
    mock_api.get_asset.return_value.easy_to_borrow = True
    assert broker.is_shortable("AAPL") is True


def test_is_shortable_false_when_hard_to_borrow(broker, mock_api):
    mock_api.get_asset.return_value.shortable = True
    mock_api.get_asset.return_value.easy_to_borrow = False
    assert broker.is_shortable("GME") is False


def test_place_stop_order_short_side_is_buy(broker, mock_api):
    mock_api.submit_order.return_value.id = "order-123"
    broker.place_stop_order(ticker="TSLA", qty=5.0, stop_price=260.0, side="buy")
    req = mock_api.submit_order.call_args[0][0]
    assert str(req.side).lower().endswith("buy")


def test_place_stop_order_default_side_is_still_sell(broker, mock_api):
    # Backward-compat: existing long-side callers don't pass `side` at all.
    mock_api.submit_order.return_value.id = "order-124"
    broker.place_stop_order(ticker="AAPL", qty=5.0, stop_price=140.0)
    req = mock_api.submit_order.call_args[0][0]
    assert str(req.side).lower().endswith("sell")


def test_place_stop_order_rejects_invalid_side(broker, mock_api):
    result = broker.place_stop_order(ticker="AAPL", qty=5.0, stop_price=140.0, side="short")
    assert result is None
    mock_api.submit_order.assert_not_called()
