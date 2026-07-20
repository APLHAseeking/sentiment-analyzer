"""Tests for the SimulatedBroker — offline paper execution."""
import pytest
from unittest.mock import MagicMock, patch

from execution.paper_broker import SimulatedBroker
from execution.broker_interface import OrderStatus, OrderSide


@pytest.fixture
def broker():
    return SimulatedBroker(initial_cash=100_000.0, slippage_bps=0.0, commission_per_share=0.0)


def _mock_price(mocker, ticker: str, price: float):
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = price
    mocker.patch("execution.paper_broker.yf.Ticker", return_value=mock_ticker)


def test_initial_cash(broker):
    assert broker.get_cash() == 100_000.0


def test_initial_equity_equals_cash(broker):
    assert broker.get_equity() == 100_000.0


def test_initial_positions_empty(broker):
    assert broker.get_positions() == []


def test_place_buy_order_fills(broker, mocker):
    _mock_price(mocker, "AAPL", 150.0)
    order = broker.place_order("AAPL", "buy", 10.0)
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 10.0
    assert order.filled_avg_price == 150.0


def test_buy_reduces_cash(broker, mocker):
    _mock_price(mocker, "AAPL", 150.0)
    broker.place_order("AAPL", "buy", 10.0)
    assert broker.get_cash() == pytest.approx(100_000.0 - 1_500.0)


def test_buy_creates_position(broker, mocker):
    _mock_price(mocker, "AAPL", 150.0)
    broker.place_order("AAPL", "buy", 10.0)
    positions = broker._positions
    assert "AAPL" in positions
    assert positions["AAPL"].qty == 10.0


def test_sell_removes_position(broker, mocker):
    _mock_price(mocker, "AAPL", 150.0)
    broker.place_order("AAPL", "buy", 10.0)
    broker.place_order("AAPL", "sell", 10.0)
    assert "AAPL" not in broker._positions


def test_sell_increases_cash(broker, mocker):
    _mock_price(mocker, "AAPL", 150.0)
    broker.place_order("AAPL", "buy", 10.0)
    broker.place_order("AAPL", "sell", 10.0)
    assert broker.get_cash() == pytest.approx(100_000.0, abs=0.01)


def test_sell_without_position_is_rejected(broker, mocker):
    _mock_price(mocker, "XYZ", 100.0)
    order = broker.place_order("XYZ", "sell", 5.0)
    assert order.status == OrderStatus.REJECTED
    assert order.reject_reason is not None


def test_insufficient_cash_is_rejected(broker, mocker):
    _mock_price(mocker, "COSTLY", 50_000.0)
    order = broker.place_order("COSTLY", "buy", 10.0)  # $500k > $100k cash
    assert order.status == OrderStatus.REJECTED


def test_slippage_applied_on_buy(mocker):
    broker = SimulatedBroker(initial_cash=100_000.0, slippage_bps=10.0)
    _mock_price(mocker, "AAPL", 100.0)
    order = broker.place_order("AAPL", "buy", 1.0)
    # 10 bps slippage on $100 → $0.10 above
    assert order.filled_avg_price == pytest.approx(100.10, abs=0.01)


def test_slippage_applied_on_sell(mocker):
    broker = SimulatedBroker(initial_cash=100_000.0, slippage_bps=10.0)
    _mock_price(mocker, "AAPL", 100.0)
    broker.place_order("AAPL", "buy", 1.0)
    order = broker.place_order("AAPL", "sell", 1.0)
    # Sell fills below mid
    assert order.filled_avg_price == pytest.approx(99.90, abs=0.01)


def test_order_history_records_all_orders(broker, mocker):
    _mock_price(mocker, "AAPL", 100.0)
    broker.place_order("AAPL", "buy", 5.0)
    broker.place_order("AAPL", "sell", 5.0)
    assert len(broker.get_order_history()) == 2


def test_cancel_order_marks_cancelled(broker, mocker):
    _mock_price(mocker, "AAPL", 100.0)
    # Place a pending order (we need to intercept before fill)
    from execution.broker_interface import Order, OrderSide, OrderType
    order = Order(ticker="AAPL", side=OrderSide.BUY, qty=1.0)
    broker._orders.append(order)
    result = broker.cancel_order(order.order_id)
    assert result is True
    assert order.status == OrderStatus.CANCELLED


def test_snapshot_returns_correct_structure(broker, mocker):
    _mock_price(mocker, "AAPL", 100.0)
    snap = broker.snapshot()
    assert "cash" in snap
    assert "equity" in snap
    assert "positions" in snap
    assert "n_orders" in snap


def test_set_position_price_updates_valuation(broker, mocker):
    _mock_price(mocker, "AAPL", 100.0)
    broker.place_order("AAPL", "buy", 10.0)
    broker.set_position_price("AAPL", 150.0)
    assert broker.get_equity() == pytest.approx(100_000.0 - 1_000.0 + 1_500.0, abs=1.0)


def test_is_paper_is_true(broker):
    assert broker.is_paper is True


# ------------------------------------------------------------------
# Stop order tests (Task 2.1)
# ------------------------------------------------------------------

def test_place_stop_order_registers_stop(broker):
    order_id = broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    assert order_id.startswith("sim-stop-AAPL")
    stops = broker.get_stop_orders()
    assert "AAPL" in stops
    # (stop_price, qty, order_id)
    assert stops["AAPL"][:2] == (90.0, 10.0)
    assert stops["AAPL"][2] == order_id


def test_place_stop_order_returns_id(broker):
    result = broker.place_stop_order("MSFT", qty=5.0, stop_price=200.0)
    assert result.startswith("sim-stop-MSFT")


def test_place_stop_order_returns_unique_ids(broker):
    """Two stops for the same ticker must get distinct ids so the trail-up
    path can cancel exactly one by id without touching the other."""
    id1 = broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    id2 = broker.place_stop_order("AAPL", qty=10.0, stop_price=95.0)
    assert id1 != id2


def test_get_stop_orders_empty_initially(broker):
    assert broker.get_stop_orders() == {}


def test_place_stop_order_reports_latest_when_multiple(broker):
    """Two coexisting stops for a ticker (trail-up in flight): get_stop_orders
    reports the most recently placed one."""
    broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    broker.place_stop_order("AAPL", qty=10.0, stop_price=95.0)  # trail up
    stops = broker.get_stop_orders()
    assert stops["AAPL"][0] == 95.0  # latest stop_price


def test_cancel_stop_order_no_id_removes_all_entries(broker):
    broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    broker.place_stop_order("AAPL", qty=10.0, stop_price=95.0)
    broker.cancel_stop_order("AAPL")  # no id → cancel all
    assert "AAPL" not in broker.get_stop_orders()


def test_cancel_stop_order_by_id_removes_only_that_stop(broker):
    """Cancelling the OLD stop by id must leave the NEW stop resting."""
    old_id = broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    new_id = broker.place_stop_order("AAPL", qty=10.0, stop_price=95.0)
    broker.cancel_stop_order("AAPL", order_id=old_id)
    stops = broker.get_stop_orders()
    assert "AAPL" in stops  # the new stop survives
    assert stops["AAPL"][0] == 95.0
    assert stops["AAPL"][2] == new_id


def test_cancel_stop_order_noop_if_not_registered(broker):
    # Should not raise even when ticker has no stop
    broker.cancel_stop_order("NONEXISTENT")
    broker.cancel_stop_order("NONEXISTENT", order_id="whatever")


def test_get_stop_orders_returns_copy(broker):
    broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    stops = broker.get_stop_orders()
    stops["AAPL"] = (0.0, 0.0, None)  # mutate the returned dict
    # Original should be unchanged
    assert broker.get_stop_orders()["AAPL"][0] == 90.0


def test_multiple_tickers_tracked_independently(broker):
    broker.place_stop_order("AAPL", qty=10.0, stop_price=90.0)
    broker.place_stop_order("MSFT", qty=5.0, stop_price=200.0)
    stops = broker.get_stop_orders()
    assert len(stops) == 2
    assert stops["AAPL"][:2] == (90.0, 10.0)
    assert stops["MSFT"][:2] == (200.0, 5.0)


# ------------------------------------------------------------------
# Stop order tests with side parameter (short-selling support)
# ------------------------------------------------------------------

def test_place_stop_order_accepts_side_param(broker):
    # Doesn't need to DO anything with side (SimulatedBroker's stops are just
    # passive records consumed by Portfolio's own direction-aware polling) —
    # it just must not raise TypeError when a short-side caller passes side="buy".
    order_id = broker.place_stop_order(ticker="TSLA", qty=5.0, stop_price=260.0, side="buy")
    assert order_id is not None


def test_place_stop_order_still_works_with_no_side(broker):
    order_id = broker.place_stop_order(ticker="AAPL", qty=5.0, stop_price=140.0)
    assert order_id is not None
