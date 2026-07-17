import logging

import pytest
from bot.portfolio import Portfolio

@pytest.fixture
def portfolio(db, mock_broker):
    return Portfolio(broker=mock_broker)

def test_initial_cash(portfolio, mock_broker):
    assert portfolio.get_cash() == 100_000.0

def test_can_open_when_under_limit(portfolio, mock_broker):
    mock_broker.get_positions.return_value = []
    assert portfolio.can_open_new_position() is True

def test_cannot_open_at_max_positions(portfolio, mock_broker):
    from system.config import settings
    mock_broker.get_positions.return_value = [
        {"ticker": f"T{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
        for i in range(settings.risk.max_positions)
    ]
    assert portfolio.can_open_new_position() is False

def test_short_positions_do_not_count_against_long_limit(portfolio, mock_broker):
    from system.config import settings
    # max_positions long slots all open as LONG, plus some short positions —
    # the shorts must not push this over the long limit.
    mock_broker.get_positions.return_value = (
        [{"ticker": f"L{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
         for i in range(settings.risk.max_positions - 1)]
        + [{"ticker": f"S{i}", "qty": -1.0, "current_price": 100.0, "avg_entry_price": 100.0}
           for i in range(3)]
    )
    assert portfolio.can_open_new_position() is True  # one long slot still free

def test_long_positions_do_not_count_against_short_limit(portfolio, mock_broker):
    from system.config import settings
    mock_broker.get_positions.return_value = (
        [{"ticker": f"S{i}", "qty": -1.0, "current_price": 100.0, "avg_entry_price": 100.0}
         for i in range(settings.risk.max_short_positions - 1)]
        + [{"ticker": f"L{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
           for i in range(10)]
    )
    assert portfolio.can_open_new_short_position() is True  # one short slot still free

def test_cannot_open_after_daily_limit(portfolio, mock_broker):
    portfolio._opened_today = portfolio._risk.max_positions_per_day
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

def test_stop_loss_triggers(portfolio, mock_broker, db):
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 33.0,
        "current_price": 100.0, "avg_entry_price": 120.0,
    }]
    db.insert_disclosures([{
        "id": "sl-001", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sl-001", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 120.0, 33.0, 5.0, "2026-04-01", sid, "Test")
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

# --- Trailing stop tests ---

def test_trailing_stop_triggers_from_peak(portfolio, mock_broker, db):
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 108.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "tr-001", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("tr-001", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    db.update_position_peak("AAPL", 130.0)
    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    assert "AAPL" in closed
    mock_broker.place_order.assert_called_with(ticker="AAPL", side="sell", qty=10.0)

def test_trailing_stop_does_not_trigger_within_15pct_of_peak(portfolio, mock_broker, db):
    mock_broker.get_positions.return_value = [{
        "ticker": "MSFT", "qty": 5.0,
        "current_price": 115.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "tr-002", "politician": "J", "ticker": "MSFT",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("tr-002", "MSFT", 7, 4.0, "Good", [])
    db.insert_position("MSFT", 100.0, 5.0, 4.0, "2026-04-01", sid, "Test")
    db.update_position_peak("MSFT", 120.0)
    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    assert closed == []

# --- Take-profit tests ---

def test_take_profit_reduces_on_25pct_gain(portfolio, mock_broker, db):
    mock_broker.get_positions.return_value = [{
        "ticker": "XOM", "qty": 10.0,
        "current_price": 130.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "tp-001", "politician": "J", "ticker": "XOM",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("tp-001", "XOM", 8, 5.0, "Good", [])
    db.insert_position("XOM", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    reduced = portfolio.enforce_take_profits(take_profit_pct=25.0)
    assert "XOM" in reduced
    mock_broker.place_order.assert_called_with(ticker="XOM", side="sell", qty=5.0)

def test_take_profit_does_not_trigger_below_threshold(portfolio, mock_broker):
    mock_broker.get_positions.return_value = [{
        "ticker": "GOOG", "qty": 5.0,
        "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    reduced = portfolio.enforce_take_profits(take_profit_pct=25.0)
    assert reduced == []

# --- Sector cap tests ---

def test_sector_cap_blocks_new_position(portfolio):
    sector_allocation = {"Technology": 40.0}
    assert portfolio.is_sector_capped("Technology", sector_allocation, cap_pct=30.0) is True

def test_sector_cap_allows_below_cap(portfolio):
    sector_allocation = {"Technology": 25.0}
    assert portfolio.is_sector_capped("Technology", sector_allocation, cap_pct=30.0) is False

# --- Liquidity tests ---

def test_liquidity_check_blocks_illiquid_position(portfolio):
    assert portfolio.is_liquid_enough(
        position_size_usd=50_000, avg_daily_volume_usd=200_000, max_adv_pct=10.0
    ) is False

def test_liquidity_check_passes_liquid_position(portfolio):
    assert portfolio.is_liquid_enough(
        position_size_usd=10_000, avg_daily_volume_usd=500_000, max_adv_pct=10.0
    ) is True

# --- Drawdown guard tests ---

def test_drawdown_guard_blocks_new_positions(portfolio):
    assert portfolio.is_in_drawdown(peak_nav=100_000, current_nav=87_000, max_drawdown_pct=10.0) is True

def test_drawdown_guard_allows_within_limit(portfolio):
    assert portfolio.is_in_drawdown(peak_nav=100_000, current_nav=93_000, max_drawdown_pct=10.0) is False

# --- Boundary tests ---

def test_sector_cap_at_exact_boundary(portfolio):
    assert portfolio.is_sector_capped("Technology", {"Technology": 30.0}, cap_pct=30.0) is True

def test_liquidity_at_exact_boundary(portfolio):
    # exactly 10% of ADV is allowed (<=)
    assert portfolio.is_liquid_enough(10_000, 100_000, max_adv_pct=10.0) is True

def test_drawdown_at_exact_boundary(portfolio):
    # exactly 10% drawdown triggers the guard
    assert portfolio.is_in_drawdown(100_000, 90_000, max_drawdown_pct=10.0) is True

# --- Stop-loss DB write test ---

def test_open_position_stores_signal_source(mock_broker, db):
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    db.insert_disclosures([{
        "id": "src-p1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-28T08:00:00",
    }])
    portfolio.open_position("AAPL", 5.0, None, "test", 100.0, signal_source="fundamental")
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["signal_source"] == "fundamental"


def test_close_position_stores_signal_source(mock_broker, db):
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.close_position(
        ticker="AAPL", shares=10.0, exit_price=110.0,
        exit_reason="ai_exit", signal_id=None,
        entry_price=100.0, entry_date="2026-04-01",
        signal_source="fundamental",
    )
    rows = db.get_closed_positions()
    assert rows[0]["signal_source"] == "fundamental"


def test_open_position_defaults_source_to_congressional(mock_broker, db):
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("MSFT", 4.0, None, "test", 200.0)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "MSFT")
    assert pos["signal_source"] == "congressional"


# --- Stop-loss DB write test ---

def test_stop_loss_writes_closed_position_record(portfolio, mock_broker, db):
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    mock_broker.get_positions.return_value = [{
        "ticker": "TSLA", "qty": 5.0,
        "current_price": 80.0, "avg_entry_price": 100.0,
    }]
    _filled = Order(ticker="TSLA", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    _filled.status = OrderStatus.FILLED
    _filled.filled_qty = 5.0
    mock_broker.place_order.return_value = _filled
    db.insert_disclosures([{
        "id": "sl-wr-001", "politician": "J", "ticker": "TSLA",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sl-wr-001", "TSLA", 8, 5.0, "Good", [])
    db.insert_position("TSLA", 100.0, 5.0, 5.0, "2026-04-01", sid, "Test")
    portfolio.enforce_stop_losses(stop_loss_pct=15.0)
    rows = db.get_closed_positions()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "TSLA"
    assert rows[0]["exit_reason"] == "stop_loss"
    assert abs(rows[0]["realized_pnl"] - (-100.0)) < 0.01  # (80-100)*5 = -100


from system.config import RiskConfig


def test_enforce_stop_losses_source_include_processes_only_matching(mock_broker, db):
    from system.config import RiskConfig
    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=5.0))
    # Both positions drop 6% — triggers 5% custom threshold
    mock_broker.get_positions.return_value = [
        {"ticker": "SH",   "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
        {"ticker": "AAPL", "qty": 5.0,  "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "sf-aapl-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sf-aapl-01", "AAPL", 7, 4.0, "Good", [])
    # stop_pct=5.0 explicit: this test exercises source scope filtering, not stop width,
    # so give both positions a stop_pct matching the injected RiskConfig (positions no
    # longer fall back to a live RiskConfig at poll time — see enforce_stop_losses).
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge", stop_pct=5.0)
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional", stop_pct=5.0)
    # source_include="hedge" → only SH processed
    closed = p.enforce_stop_losses(source_include="hedge")
    assert "SH" in closed
    assert "AAPL" not in closed


def test_enforce_stop_losses_source_exclude_skips_matching(mock_broker, db):
    from system.config import RiskConfig
    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=5.0))
    mock_broker.get_positions.return_value = [
        {"ticker": "SH",   "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
        {"ticker": "AAPL", "qty": 5.0,  "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "sf-aapl-02", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sf-aapl-02", "AAPL", 7, 4.0, "Good", [])
    # stop_pct=5.0 explicit: see note in test_enforce_stop_losses_source_include_processes_only_matching
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge", stop_pct=5.0)
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional", stop_pct=5.0)
    # source_exclude="hedge" → SH skipped, AAPL processed
    closed = p.enforce_stop_losses(source_exclude="hedge")
    assert "SH" not in closed
    assert "AAPL" in closed


def test_enforce_stop_losses_raises_when_both_filters_set(mock_broker):
    p = Portfolio(broker=mock_broker)
    with pytest.raises(ValueError, match="mutually exclusive"):
        p.enforce_stop_losses(source_include="hedge", source_exclude="congressional")


def test_portfolio_reads_max_positions_from_config(mock_broker):
    risk_cfg = RiskConfig(max_positions=5)
    p = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = [
        {"ticker": f"T{i}", "qty": 1.0, "current_price": 100.0, "avg_entry_price": 100.0}
        for i in range(5)
    ]
    assert p.can_open_new_position() is False


def test_portfolio_reads_stop_loss_from_config(mock_broker, db):
    # 6% drop — triggers a 5% per-position stop_pct, would NOT trigger the 15% DB default.
    # NOTE: enforce_stop_losses() with no override now reads each position's OWN stored
    # stop_pct (set at open time), not RiskConfig.trailing_stop_pct directly — see
    # Component 6 of the technical-analysis-layer change. risk_cfg is still passed since
    # Portfolio requires a risk_cfg instance, but it no longer drives this stop's width.
    risk_cfg = RiskConfig(trailing_stop_pct=5.0)
    p = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 94.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "cfg-sl-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("cfg-sl-01", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=5.0)
    closed = p.enforce_stop_losses()   # no explicit pct — reads the position's own stop_pct
    assert "AAPL" in closed


def test_portfolio_reads_take_profit_from_config(mock_broker, db):
    # 6% gain — triggers 5% custom threshold, would NOT trigger the default 25%
    risk_cfg = RiskConfig(take_profit_pct=5.0)
    p = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = [{
        "ticker": "TSLA", "qty": 5.0,
        "current_price": 106.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "cfg-tp-01", "politician": "J", "ticker": "TSLA",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("cfg-tp-01", "TSLA", 7, 4.0, "Good", [])
    db.insert_position("TSLA", 100.0, 5.0, 4.0, "2026-04-01", sid, "Test")
    reduced = p.enforce_take_profits()  # no explicit pct — must read from injected config
    assert "TSLA" in reduced


def test_open_position_rejected_order_does_not_insert_to_db(mock_broker, db):
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    rejected_order = Order(
        ticker="AAPL", side=OrderSide.BUY, qty=10.0, order_type=OrderType.MARKET,
    )
    rejected_order.status = OrderStatus.REJECTED
    rejected_order.reject_reason = "insufficient buying power"
    mock_broker.place_order.return_value = rejected_order

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.open_position("AAPL", 5.0, None, "test", 100.0)

    assert result is False
    open_positions = db.get_open_positions()
    assert not any(p["ticker"] == "AAPL" for p in open_positions)


def test_open_position_unconfirmed_fill_does_not_insert_to_db(mock_broker, db):
    """A fill-poll timeout leaves the order SUBMITTED (not FILLED, not
    REJECTED) with filled_avg_price still 0.0 — this used to fall through to
    the NAV-estimate fallback and book a phantom position for an order that
    was never actually confirmed at the broker. It must now cancel the
    dangling order and skip the candidate instead."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    pending_order = Order(
        ticker="CF", side=OrderSide.BUY, qty=7.34, order_type=OrderType.MARKET,
    )
    pending_order.status = OrderStatus.SUBMITTED
    pending_order.order_id = "b5b24e9e-fake"
    mock_broker.place_order.return_value = pending_order
    mock_broker.get_positions.return_value = []

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.open_position("CF", 5.0, None, "test", 113.20)

    assert result is False
    open_positions = db.get_open_positions()
    assert not any(p["ticker"] == "CF" for p in open_positions)
    mock_broker.cancel_order.assert_called_once_with("b5b24e9e-fake")


def test_open_position_unconfirmed_fill_alerts_distinctly_when_cancel_fails(mock_broker, db, mocker):
    """cancel_order can fail for reasons other than 'already filled' (transient
    broker error, uncancellable state) — reconcile_with_broker's untracked-
    position check only looks at broker *positions*, not outstanding *orders*,
    so a failed cancel here is a real visibility gap. The alert emitted for a
    failed cancel must be distinguishable from the success-path alert."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    pending_order = Order(
        ticker="CF", side=OrderSide.BUY, qty=7.34, order_type=OrderType.MARKET,
    )
    pending_order.status = OrderStatus.SUBMITTED
    pending_order.order_id = "b5b24e9e-fake"
    mock_broker.place_order.return_value = pending_order
    mock_broker.get_positions.return_value = []
    mock_broker.cancel_order.return_value = False

    mock_emit = mocker.patch("bot.portfolio.emit_event")

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.open_position("CF", 5.0, None, "test", 113.20)

    assert result is False
    mock_broker.cancel_order.assert_called_once_with("b5b24e9e-fake")
    mock_emit.assert_called_once()
    args, kwargs = mock_emit.call_args
    message = args[2]
    assert "cancel FAILED" in message
    assert "cancelled, position not opened" not in message
    assert kwargs["data"]["cancel_failed"] is True
    assert kwargs["level"] == logging.CRITICAL


def test_reconcile_removes_ghost_positions(mock_broker, db):
    # Insert a position in SQLite that doesn't exist at the broker
    db.insert_disclosures([{
        "id": "rec-001", "politician": "J", "ticker": "GHOST",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    db.insert_position("GHOST", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    # Broker reports no positions (ghost not there)
    mock_broker.get_positions.return_value = []

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.reconcile_with_broker()

    assert "GHOST" in result["ghost_positions"]
    assert result["untracked_positions"] == []
    # Ghost position must be removed from SQLite
    open_positions = db.get_open_positions()
    assert not any(p["ticker"] == "GHOST" for p in open_positions)


def test_reconcile_books_ghost_position_with_matching_fill(mock_broker, db):
    """A ghost position (gone from the broker, e.g. a resting GTC stop filled
    server-side) must be booked into closed_positions with the real fill price,
    not just silently deleted — the fill already happened, its P&L must not be
    discarded."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType

    db.insert_disclosures([{
        "id": "rec-fill-001", "politician": "J", "ticker": "STOP",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("rec-fill-001", "STOP", 8, 5.0, "Good", [])
    db.insert_position("STOP", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")

    # Broker no longer reports the position (the resting stop filled server-side)
    mock_broker.get_positions.return_value = []

    fill = Order(ticker="STOP", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    fill.status = OrderStatus.FILLED
    fill.filled_qty = 10.0
    fill.filled_avg_price = 85.0
    fill.filled_at = "2026-04-10T15:30:00+00:00"
    mock_broker.get_order_history.return_value = [fill]

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.reconcile_with_broker()

    assert "STOP" in result["ghost_positions"]
    # Removed from open positions...
    assert not any(p["ticker"] == "STOP" for p in db.get_open_positions())
    # ...but booked into closed_positions with the real fill price, not discarded.
    closed = db.get_closed_positions()
    assert len(closed) == 1
    assert closed[0]["ticker"] == "STOP"
    assert closed[0]["exit_price"] == pytest.approx(85.0)
    # realized_pnl = (85 - 100) * 10 = -150 (no commissions configured on mock_broker)
    assert closed[0]["realized_pnl"] == pytest.approx(-150.0)


def test_reconcile_ghost_without_matching_fill_still_deletes_and_alerts(mock_broker, db, mocker):
    """If no matching fill is found in get_order_history() for a ghost ticker,
    fall through to the bare delete — but now also fire an alert, since silently
    losing a position with no fill record is itself worth flagging."""
    mock_fire_alert = mocker.patch("monitoring.logger.fire_alert")

    db.insert_position("GHOST2", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    mock_broker.get_positions.return_value = []
    mock_broker.get_order_history.return_value = []  # no fill on record at all

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.reconcile_with_broker()

    assert "GHOST2" in result["ghost_positions"]
    assert not any(p["ticker"] == "GHOST2" for p in db.get_open_positions())
    assert db.get_closed_positions() == []
    mock_fire_alert.assert_called_once()


def test_enforce_take_profits_source_exclude_skips_matching(mock_broker, db):
    from system.config import RiskConfig
    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(take_profit_pct=5.0))
    # Both positions gain 6% — triggers 5% threshold
    mock_broker.get_positions.return_value = [
        {"ticker": "SH",   "qty": 10.0, "current_price": 106.0, "avg_entry_price": 100.0},
        {"ticker": "AAPL", "qty": 5.0,  "current_price": 106.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "sf-tp-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("sf-tp-01", "AAPL", 7, 4.0, "Good", [])
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge")
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional")
    # source_exclude="hedge" → SH skipped, AAPL reduced
    reduced = p.enforce_take_profits(source_exclude="hedge")
    assert "SH" not in reduced
    assert "AAPL" in reduced


# ------------------------------------------------------------------
# Stop order registration + trailing tests (Task 2.1)
# ------------------------------------------------------------------

def test_open_position_registers_stop_order(mock_broker, db):
    """Opening a position must call place_stop_order on the broker."""
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("AAPL", 5.0, None, "test", 100.0)
    mock_broker.place_stop_order.assert_called_once()
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    assert call_kwargs["ticker"] == "AAPL"
    # Default trailing_stop_pct is 15% → stop at 100 * (1 - 0.15) = 85.0
    from system.config import settings
    expected_stop = 100.0 * (1 - settings.risk.trailing_stop_pct / 100)
    assert call_kwargs["stop_price"] == pytest.approx(expected_stop)


def test_open_position_alerts_when_initial_stop_placement_fails(mock_broker, db, mocker):
    """place_stop_order returning None on every attempt (e.g. a real rejection,
    not the transient wash-trade race) must exhaust retries — the position is
    still opened (DB insert already committed before the stop call), but a
    naked position with zero resting stop must fire an alert, not fail
    silently."""
    mocker.patch("time.sleep")  # skip real backoff delay in test
    mock_fire_alert = mocker.patch("monitoring.logger.fire_alert")
    mock_broker.get_positions.return_value = []
    mock_broker.place_stop_order.return_value = None
    portfolio = Portfolio(broker=mock_broker)

    result = portfolio.open_position("AAPL", 5.0, None, "test", 100.0)

    assert result is True
    assert mock_broker.place_stop_order.call_count == 3
    mock_fire_alert.assert_called_once()


def test_open_position_retries_initial_stop_after_wash_trade_rejection(mock_broker, db, mocker):
    """Alpaca's wash-trade check can reject a stop placed immediately after its
    opposite-side buy fills (transient — its fill-state propagation lags ours).
    A later attempt succeeding must NOT alert or leave the position unprotected."""
    mocker.patch("time.sleep")  # skip real backoff delay in test
    mock_fire_alert = mocker.patch("monitoring.logger.fire_alert")
    mock_broker.get_positions.return_value = []
    mock_broker.place_stop_order.side_effect = [None, "stop-id-123"]
    portfolio = Portfolio(broker=mock_broker)

    result = portfolio.open_position("AAPL", 5.0, None, "test", 100.0)

    assert result is True
    assert mock_broker.place_stop_order.call_count == 2
    mock_fire_alert.assert_not_called()


def test_open_position_stop_uses_custom_trailing_pct(mock_broker, db):
    """Stop price respects the injected trailing_stop_pct config."""
    from system.config import RiskConfig
    risk_cfg = RiskConfig(trailing_stop_pct=10.0)
    portfolio = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    mock_broker.get_positions.return_value = []
    portfolio.open_position("MSFT", 4.0, None, "test", 200.0)
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    expected_stop = 200.0 * (1 - 10.0 / 100)  # 180.0
    assert call_kwargs["stop_price"] == pytest.approx(expected_stop)


def test_enforce_stop_losses_trails_stop_upward(mock_broker, db):
    """When price rises, enforce_stop_losses should raise the resting stop."""
    from system.config import RiskConfig
    risk_cfg = RiskConfig(trailing_stop_pct=15.0)
    portfolio = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)

    # Broker has no existing stop → get_stop_orders returns {}
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "trail-001", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("trail-001", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    # New stop should be 120 * 0.85 = 102.0, which is higher than 0.0 (no prior stop)
    mock_broker.place_stop_order.assert_called_once()
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    assert call_kwargs["ticker"] == "AAPL"
    assert call_kwargs["stop_price"] == pytest.approx(120.0 * 0.85)


def test_enforce_stop_losses_does_not_trail_stop_downward(mock_broker, db):
    """When current price is below an existing stop level, the stop must not be lowered."""
    from system.config import RiskConfig
    risk_cfg = RiskConfig(trailing_stop_pct=15.0)
    portfolio = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)

    # Existing stop is already at 110 * 0.85 = 93.5 (set when price was 110)
    mock_broker.get_stop_orders.return_value = {"AAPL": (93.5, 10.0)}
    # Price has dropped back to 100 — new candidate stop = 100 * 0.85 = 85.0 < 93.5
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 100.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "no-lower-001", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("no-lower-001", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    # Peak stored at 110 so stop-loss of 15% does NOT trigger from 100
    db.update_position_peak("AAPL", 100.0)  # peak = 100; drop = 0% < 15%

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    # place_stop_order should NOT be called (new candidate 85.0 < existing 93.5)
    mock_broker.place_stop_order.assert_not_called()


def test_open_position_rejected_order_does_not_place_stop(mock_broker, db):
    """If the order is rejected, no stop should be registered."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    rejected_order = Order(
        ticker="AAPL", side=OrderSide.BUY, qty=10.0, order_type=OrderType.MARKET,
    )
    rejected_order.status = OrderStatus.REJECTED
    rejected_order.reject_reason = "insufficient buying power"
    mock_broker.place_order.return_value = rejected_order

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.open_position("AAPL", 5.0, None, "test", 100.0)

    assert result is False
    mock_broker.place_stop_order.assert_not_called()


# ------------------------------------------------------------------
# Untracked position handling (Task 2.2)
# ------------------------------------------------------------------

def test_reconcile_auto_flatten_untracked_calls_sell(mock_broker, db, mocker):
    """When auto_flatten_untracked=True, reconcile_with_broker must place a sell order
    for each broker position that is not in SQLite."""
    from system.config import RiskConfig
    # Broker has one untracked position not in SQLite
    mock_broker.get_positions.return_value = [
        {"ticker": "UNTRK", "qty": 15.0, "current_price": 50.0, "avg_entry_price": 45.0}
    ]
    mocker.patch("monitoring.logger.fire_alert")  # suppress webhook calls

    risk_cfg = RiskConfig(auto_flatten_untracked=True)
    portfolio = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    result = portfolio.reconcile_with_broker()

    # untracked position must appear in the return dict
    assert "UNTRK" in result["untracked_positions"]
    # broker.place_order must have been called with side="sell"
    mock_broker.place_order.assert_called_once_with(ticker="UNTRK", side="sell", qty=15.0)


def test_reconcile_no_flatten_emits_critical_alert(mock_broker, db, mocker):
    """When auto_flatten_untracked=False (default), reconcile_with_broker must emit
    a CRITICAL alert but NOT place any sell order."""
    from system.config import RiskConfig
    mock_broker.get_positions.return_value = [
        {"ticker": "UNTRK2", "qty": 10.0, "current_price": 60.0, "avg_entry_price": 55.0}
    ]
    # fire_alert is imported into monitoring.logger; patch at that binding
    mock_fire_alert = mocker.patch("monitoring.logger.fire_alert")

    risk_cfg = RiskConfig(auto_flatten_untracked=False)
    portfolio = Portfolio(broker=mock_broker, risk_cfg=risk_cfg)
    result = portfolio.reconcile_with_broker()

    assert "UNTRK2" in result["untracked_positions"]
    # No sell order should have been placed
    mock_broker.place_order.assert_not_called()
    # But an alert must have been fired
    mock_fire_alert.assert_called_once()


def test_close_position_rejected_sell_does_not_log_or_delete(mock_broker, db, mocker):
    """A rejected sell must leave the DB position intact and book no closed_position."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    rejected = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    # Position still in DB, nothing booked as closed
    assert any(p["ticker"] == "AAPL" for p in db.get_open_positions())
    assert db.get_closed_positions() == []


def test_reduce_position_rejected_sell_does_not_change_shares(mock_broker, db):
    """A rejected partial sell must not change DB shares or book a trade."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    rejected = Order(ticker="AAPL", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.reduce_position(
        "AAPL", 10.0, exit_price=110.0,
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    pos = [p for p in db.get_open_positions() if p["ticker"] == "AAPL"][0]
    assert pos["shares"] == pytest.approx(10.0)  # unchanged
    assert db.get_closed_positions() == []


def test_trailing_up_replaces_resting_stop(mock_broker, db):
    """On a trail-up with no KNOWN existing stop id, the new stop is placed and
    cancel_stop_order must NOT be called at all — there's nothing to cancel, and
    a ticker-wide sweep (order_id=None) would risk catching the just-placed new
    stop too (see test_trailing_up_with_no_known_prior_stop_keeps_the_new_stop
    for the real-broker regression this guards against)."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {}  # no existing stop → will place
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    mock_broker.cancel_stop_order.assert_not_called()
    mock_broker.place_stop_order.assert_called_once()


def test_enforce_stop_losses_cancels_old_stop_by_id_not_ticker(mock_broker, db):
    """On a trail-up with a pre-existing stop, the OLD stop must be cancelled by
    its specific order id — NOT ticker-only — so the just-placed new stop (which
    shares ticker/type/status) survives."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    # get_stop_orders now returns (stop_price, qty, order_id).
    mock_broker.get_stop_orders.return_value = {"AAPL": (90.0, 10.0, "old-stop-id")}
    mock_broker.place_stop_order.return_value = "new-stop-id"
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    mock_broker.cancel_stop_order.assert_called_once_with("AAPL", order_id="old-stop-id")


def test_enforce_stop_losses_places_new_stop_before_cancelling_old(mock_broker, db):
    """On a trail-up, the new (higher) stop must be placed BEFORE the old one
    is cancelled, so the position is never left without a resting stop."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {"AAPL": (90.0, 10.0, "old-stop-id")}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    call_order = []

    def _place(**kw):
        call_order.append("place")
        return "new-stop-order-id"  # successful placement — must not be None

    mock_broker.place_stop_order.side_effect = _place
    mock_broker.cancel_stop_order.side_effect = lambda t, order_id=None: call_order.append("cancel")

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    assert call_order == ["place", "cancel"]


def test_enforce_stop_losses_keeps_old_stop_when_new_placement_fails(mock_broker, db, mocker):
    """place_stop_order returns None on failure. If the new (higher) stop fails
    to place, the old resting stop must NOT be cancelled — otherwise the
    position is left with zero resting stops. An alert must fire instead."""
    from system.config import RiskConfig
    mock_fire_alert = mocker.patch("monitoring.logger.fire_alert")

    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {"AAPL": (90.0, 10.0, "old-stop-id")}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    # Simulate broker-side placement failure.
    mock_broker.place_stop_order.return_value = None

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    mock_broker.place_stop_order.assert_called_once()
    # The old stop must survive — cancel_stop_order must NOT be called.
    mock_broker.cancel_stop_order.assert_not_called()
    mock_fire_alert.assert_called_once()


def test_close_position_cancels_resting_stop(mock_broker, db):
    """Closing a position must cancel its resting stop so it can't fire later."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    filled = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    filled.status = OrderStatus.FILLED
    filled.filled_qty = 10.0
    mock_broker.place_order.return_value = filled
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    mock_broker.cancel_stop_order.assert_called_once_with("AAPL")


def test_hedge_stop_pass_does_not_retrail_long_positions(mock_broker, db):
    """enforce_stop_losses(source_include='hedge') must not touch a long position's stop."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    # AAPL is a long (congressional) position
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test", "congressional")

    portfolio.enforce_stop_losses(stop_loss_pct=10.0, source_include="hedge")

    mock_broker.place_stop_order.assert_not_called()
    mock_broker.cancel_stop_order.assert_not_called()


def test_open_position_uses_broker_fill_data_when_available(mock_broker, db):
    """If the broker reports a real fill (filled_avg_price > 0), open_position
    must record the ACTUAL filled shares/price, not the pre-trade NAV estimate."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    from system.config import settings

    filled_order = Order(
        ticker="AAPL", side=OrderSide.BUY, qty=8.0, order_type=OrderType.MARKET,
    )
    filled_order.status = OrderStatus.FILLED
    filled_order.filled_qty = 7.5
    filled_order.filled_avg_price = 101.50
    mock_broker.place_order.return_value = filled_order
    mock_broker.get_positions.return_value = []

    portfolio = Portfolio(broker=mock_broker)
    result = portfolio.open_position("AAPL", 5.0, None, "test", entry_price=100.0)

    assert result is True
    positions = db.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["shares"] == pytest.approx(7.5)
    assert positions[0]["entry_price"] == pytest.approx(101.50)

    stop_kwargs = mock_broker.place_stop_order.call_args[1]
    assert stop_kwargs["qty"] == pytest.approx(7.5)
    expected_stop = 101.50 * (1 - settings.risk.trailing_stop_pct / 100)
    assert stop_kwargs["stop_price"] == pytest.approx(expected_stop)


def test_open_position_falls_back_to_nav_estimate_when_no_fill_price(mock_broker, db):
    """If filled_avg_price == 0 (default mock_broker fixture), open_position
    keeps the pre-trade NAV-based shares/entry_price — existing behavior."""
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("AAPL", 5.0, None, "test", entry_price=100.0)

    positions = db.get_open_positions()
    assert len(positions) == 1
    # NAV-based: shares = (100_000 * 5/100) / 100.0 = 50.0
    assert positions[0]["shares"] == pytest.approx(50.0)
    assert positions[0]["entry_price"] == pytest.approx(100.0)


def test_open_position_uses_initial_stop_pct_when_provided(mock_broker, db):
    """A structural stop width overrides the global trailing_stop_pct for both
    the resting broker stop and the persisted positions.stop_pct."""
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("AAPL", 5.0, None, "test", 100.0, initial_stop_pct=2.0)
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    assert call_kwargs["stop_price"] == pytest.approx(98.0)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "AAPL")
    assert pos["stop_pct"] == pytest.approx(2.0)


def test_open_position_default_stop_pct_unchanged_when_not_provided(mock_broker, db):
    """initial_stop_pct=None (default) must behave exactly as before this feature."""
    mock_broker.get_positions.return_value = []
    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("MSFT", 5.0, None, "test", 200.0)
    from system.config import settings
    call_kwargs = mock_broker.place_stop_order.call_args[1]
    expected_stop = 200.0 * (1 - settings.risk.trailing_stop_pct / 100)
    assert call_kwargs["stop_price"] == pytest.approx(expected_stop)
    pos = next(p for p in db.get_open_positions() if p["ticker"] == "MSFT")
    assert pos["stop_pct"] == pytest.approx(settings.risk.trailing_stop_pct)


def test_enforce_stop_losses_uses_per_position_stop_pct_with_no_override(mock_broker, db):
    """Each position's own stop_pct governs its trailing/closing when no
    explicit stop_loss_pct override is given to enforce_stop_losses()."""
    portfolio = Portfolio(broker=mock_broker)
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
        {"ticker": "MSFT", "qty": 5.0, "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "pp-stop-01", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("pp-stop-01", "AAPL", 8, 5.0, "Good", [])
    # AAPL: tight 5% structural stop -> a 6% drop from peak closes it
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=5.0)
    # MSFT: default 15% stop -> the same 6% drop does NOT close it
    db.insert_position("MSFT", 100.0, 5.0, 4.0, "2026-04-01", None, "Test")

    closed = portfolio.enforce_stop_losses()

    assert "AAPL" in closed
    assert "MSFT" not in closed


def test_enforce_stop_losses_explicit_override_ignores_per_position_stop_pct(mock_broker, db):
    """An explicit stop_loss_pct override (used by hedge-scoped polling) must
    apply uniformly, even when a position has its own stored stop_pct."""
    portfolio = Portfolio(broker=mock_broker)
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "pp-stop-02", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("pp-stop-02", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=5.0)

    # 6% drop from peak would close under the position's own 5% stop_pct, but the
    # explicit 20% override must keep it open.
    closed = portfolio.enforce_stop_losses(stop_loss_pct=20.0)

    assert "AAPL" not in closed


def test_enforce_stop_losses_honors_stored_zero_stop_pct(mock_broker, db):
    """A stored stop_pct of exactly 0.0 must not be silently replaced by the
    global default — Python's `or` treats 0.0 as falsy, masking a real (if
    degenerate) structural stop with a much wider one."""
    portfolio = Portfolio(broker=mock_broker)
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 94.0, "avg_entry_price": 100.0},
    ]
    db.insert_disclosures([{
        "id": "pp-stop-03", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("pp-stop-03", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test", stop_pct=0.0)

    closed = portfolio.enforce_stop_losses()

    assert "AAPL" in closed


def test_enforce_stop_losses_honors_stored_zero_peak_price(mock_broker, db):
    """A stored peak_price of exactly 0.0 must not be silently replaced by
    avg_entry_price — Python's `or` treats 0.0 as falsy, masking a real (if
    degenerate) stored peak with the entry price.

    avg_entry_price=100, current=80 would be a 20% drop from peak if the bug
    incorrectly falls back to avg_entry_price as the peak — which trips a 15%
    stop_loss_pct and closes the position. With the stored peak honored as
    0.0, the position must NOT close (a non-positive peak can't be used as a
    denominator for the drop-from-peak calc, so that check is skipped instead
    of dividing by zero)."""
    portfolio = Portfolio(broker=mock_broker)
    mock_broker.get_stop_orders.return_value = {}
    mock_broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 80.0, "avg_entry_price": 100.0},
    ]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    # Force peak_price to an explicit 0.0 (update_position_peak only ever raises
    # it, so write it directly to simulate an explicitly-stored degenerate value).
    with db.get_conn() as conn:
        conn.execute("UPDATE positions SET peak_price = 0.0 WHERE ticker = ?", ("AAPL",))

    closed = portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    assert "AAPL" not in closed


def test_trailing_up_leaves_exactly_one_resting_stop_real_simulated_broker(db):
    """End-to-end against the REAL SimulatedBroker (not a MagicMock): a trail-up
    through Portfolio.enforce_stop_losses must leave EXACTLY ONE resting stop, at
    the new (higher) price — not zero. Regression for the bug where placing the
    new stop overwrote the old, then cancel_stop_order(ticker) popped the new one,
    leaving the position with no resting stop at all."""
    from execution.paper_broker import SimulatedBroker
    from system.config import RiskConfig

    broker = SimulatedBroker(initial_cash=100_000.0, slippage_bps=0.0)
    # Seed a position directly into the broker's in-memory book.
    from execution.broker_interface import Position
    broker._positions["AAPL"] = Position(
        ticker="AAPL", qty=10.0, avg_entry_price=100.0, current_price=100.0
    )
    # Initial resting stop at 15% below entry (=85).
    broker.place_stop_order("AAPL", qty=10.0, stop_price=85.0)
    assert len(broker.get_stop_orders()) == 1

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    # Price rallies to 120 → new stop should trail up to 102 (15% below 120).
    broker.set_position_price("AAPL", 120.0)
    portfolio = Portfolio(broker=broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    stops = broker.get_stop_orders()
    assert len(stops) == 1, f"expected exactly one resting stop, got {stops}"
    assert stops["AAPL"][0] == pytest.approx(102.0)  # new (higher) stop, old gone


def test_trailing_up_with_no_known_prior_stop_keeps_the_new_stop(db):
    """Post-restart scenario against the REAL SimulatedBroker: the broker has NO
    resting stop registered for the ticker (fresh in-memory broker after a
    restart), but the position is real and open in the DB. A trail-up must
    still leave the just-placed new stop resting — get_stop_orders() returning
    nothing for this ticker must NOT be read as "cancel everything for this
    ticker" (order_id=None), or the just-placed new stop is immediately wiped
    out by that ticker-wide sweep, leaving zero resting stops."""
    from execution.paper_broker import SimulatedBroker
    from system.config import RiskConfig
    from execution.broker_interface import Position

    broker = SimulatedBroker(initial_cash=100_000.0, slippage_bps=0.0)
    broker._positions["AAPL"] = Position(
        ticker="AAPL", qty=10.0, avg_entry_price=100.0, current_price=100.0
    )
    # No stop registered with the broker at all — simulates a fresh broker
    # instance (e.g. after a bot restart) for a position that already exists
    # in the persistent DB.
    assert broker.get_stop_orders() == {}

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    broker.set_position_price("AAPL", 120.0)
    portfolio = Portfolio(broker=broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    stops = broker.get_stop_orders()
    assert len(stops) == 1, f"expected the just-placed new stop to survive, got {stops}"
    assert stops["AAPL"][0] == pytest.approx(102.0)


# ------------------------------------------------------------------
# HIGH #3: CANCELLED / SUBMITTED sells are no-ops (like REJECTED)
# ------------------------------------------------------------------

def test_close_position_cancelled_is_noop(mock_broker, db):
    """A CANCELLED sell must leave DB position intact and book nothing."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    cancelled = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    cancelled.status = OrderStatus.CANCELLED
    mock_broker.place_order.return_value = cancelled

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    result = portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    assert result is False
    assert any(p["ticker"] == "AAPL" for p in db.get_open_positions())
    assert db.get_closed_positions() == []


def test_close_position_submitted_is_noop(mock_broker, db):
    """A SUBMITTED (poll-timeout) sell must leave DB position intact and book nothing."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    submitted = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    submitted.status = OrderStatus.SUBMITTED
    mock_broker.place_order.return_value = submitted

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    result = portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    assert result is False
    assert any(p["ticker"] == "AAPL" for p in db.get_open_positions())
    assert db.get_closed_positions() == []


def test_reduce_position_cancelled_is_noop(mock_broker, db):
    """A CANCELLED partial sell must not change DB shares or book a trade."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    cancelled = Order(ticker="AAPL", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    cancelled.status = OrderStatus.CANCELLED
    mock_broker.place_order.return_value = cancelled

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    result = portfolio.reduce_position(
        "AAPL", 10.0, exit_price=110.0,
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    assert result is False
    pos = [p for p in db.get_open_positions() if p["ticker"] == "AAPL"][0]
    assert pos["shares"] == pytest.approx(10.0)
    assert db.get_closed_positions() == []


def test_reduce_position_submitted_is_noop(mock_broker, db):
    """A SUBMITTED (poll-timeout) partial sell must not change DB shares or book a trade."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    submitted = Order(ticker="AAPL", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    submitted.status = OrderStatus.SUBMITTED
    mock_broker.place_order.return_value = submitted

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    result = portfolio.reduce_position(
        "AAPL", 10.0, exit_price=110.0,
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    assert result is False
    pos = [p for p in db.get_open_positions() if p["ticker"] == "AAPL"][0]
    assert pos["shares"] == pytest.approx(10.0)
    assert db.get_closed_positions() == []


# ------------------------------------------------------------------
# HIGH #1 / HIGH #2: enforce methods gate on actual fill
# ------------------------------------------------------------------

def test_enforce_take_profits_rejected_not_marked_taken(mock_broker, db):
    """If reduce_position's sell is REJECTED, take_profit_taken must NOT be set,
    and the ticker must NOT appear in the returned reduced list."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    from system.config import RiskConfig
    rejected = Order(ticker="XOM", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(take_profit_pct=25.0))
    mock_broker.get_positions.return_value = [{
        "ticker": "XOM", "qty": 10.0, "current_price": 130.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "tp-rej-01", "politician": "J", "ticker": "XOM",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("tp-rej-01", "XOM", 8, 5.0, "Good", [])
    db.insert_position("XOM", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")

    reduced = p.enforce_take_profits(take_profit_pct=25.0)

    assert "XOM" not in reduced
    pos = [pos2 for pos2 in db.get_open_positions() if pos2["ticker"] == "XOM"][0]
    assert dict(pos).get("take_profit_taken", 0) == 0


def test_enforce_stop_losses_rejected_not_in_closed_list(mock_broker, db):
    """If close_position's sell is REJECTED, the ticker must NOT appear in the closed list."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    from system.config import RiskConfig
    rejected = Order(ticker="TSLA", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_positions.return_value = [{
        "ticker": "TSLA", "qty": 5.0, "current_price": 80.0, "avg_entry_price": 100.0,
    }]
    mock_broker.get_stop_orders.return_value = {}
    db.insert_position("TSLA", 100.0, 5.0, 5.0, "2026-04-01", None, "Test", stop_pct=15.0)

    closed = p.enforce_stop_losses(stop_loss_pct=15.0)

    assert "TSLA" not in closed
    assert any(pos["ticker"] == "TSLA" for pos in db.get_open_positions())


def test_enforce_take_profits_hard_exit_rejected_not_in_reduced_list(mock_broker, db):
    """If close_position's sell is REJECTED for a hard exit, ticker must NOT be in reduced list."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    from system.config import RiskConfig
    rejected = Order(ticker="XOM", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    rejected.status = OrderStatus.REJECTED
    rejected.reject_reason = "market closed"
    mock_broker.place_order.return_value = rejected

    p = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(hard_exit_pct=50.0))
    mock_broker.get_positions.return_value = [{
        "ticker": "XOM", "qty": 10.0, "current_price": 160.0, "avg_entry_price": 100.0,
    }]
    db.insert_disclosures([{
        "id": "he-rej-01", "politician": "J", "ticker": "XOM",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    sid = db.insert_signal("he-rej-01", "XOM", 8, 5.0, "Good", [])
    db.insert_position("XOM", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")

    reduced = p.enforce_take_profits(hard_exit_pct=50.0)

    assert "XOM" not in reduced


# ------------------------------------------------------------------
# MEDIUM #1: Partial fills booked at order.filled_qty, not caller shares
# ------------------------------------------------------------------

def test_close_position_uses_filled_qty_not_caller_shares(mock_broker, db):
    """On a partial fill, close_position must book order.filled_qty, not caller-supplied shares."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    partial = Order(ticker="AAPL", side=OrderSide.SELL, qty=10.0, order_type=OrderType.MARKET)
    partial.status = OrderStatus.FILLED
    partial.filled_qty = 8.0  # broker only filled 8 of the 10 requested
    mock_broker.place_order.return_value = partial

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.close_position(
        "AAPL", 10.0, exit_price=110.0, exit_reason="ai_exit",
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    rows = db.get_closed_positions()
    assert len(rows) == 1
    assert rows[0]["shares"] == pytest.approx(8.0)  # actual fill, not caller-supplied 10


def test_reduce_position_uses_filled_qty_not_caller_shares(mock_broker, db):
    """On a partial reduce fill, use order.filled_qty; remaining shares = original - filled_qty."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    partial = Order(ticker="AAPL", side=OrderSide.SELL, qty=5.0, order_type=OrderType.MARKET)
    partial.status = OrderStatus.FILLED
    partial.filled_qty = 3.0  # broker only filled 3 of 5 sell_qty
    mock_broker.place_order.return_value = partial

    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")
    portfolio = Portfolio(broker=mock_broker)

    portfolio.reduce_position(
        "AAPL", 10.0, exit_price=110.0,
        signal_id=None, entry_price=100.0, entry_date="2026-04-01",
    )

    rows = db.get_closed_positions()
    assert len(rows) == 1
    assert rows[0]["shares"] == pytest.approx(3.0)  # actual fill, not sell_qty=5

    remaining = [p for p in db.get_open_positions() if p["ticker"] == "AAPL"]
    assert remaining[0]["shares"] == pytest.approx(7.0)  # 10 - 3


# ------------------------------------------------------------------
# MEDIUM #2: position_pct recomputed from actual fill
# ------------------------------------------------------------------

def test_open_position_recomputes_position_pct_from_actual_fill(mock_broker, db):
    """position_pct stored in DB must reflect actual fill value / NAV, not pre-slippage estimate."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType

    filled_order = Order(
        ticker="AAPL", side=OrderSide.BUY, qty=50.0, order_type=OrderType.MARKET,
    )
    filled_order.status = OrderStatus.FILLED
    filled_order.filled_qty = 45.0
    filled_order.filled_avg_price = 102.0
    mock_broker.place_order.return_value = filled_order
    mock_broker.get_positions.return_value = []

    portfolio = Portfolio(broker=mock_broker)
    portfolio.open_position("AAPL", 5.0, None, "test", entry_price=100.0)

    # NAV = 100_000 (all cash, no positions)
    # Actual fill value = 45 * 102.0 = 4590
    # Expected position_pct = 4590 / 100_000 * 100 = 4.59
    pos = db.get_open_positions()[0]
    assert pos["position_pct"] == pytest.approx(4.59)


# ------------------------------------------------------------------
# Task 5: open_position direction support + short position-limit tracking
# ------------------------------------------------------------------

def test_can_open_new_short_when_under_limit(portfolio, mock_broker):
    mock_broker.get_positions.return_value = []
    assert portfolio.can_open_new_short_position() is True

def test_cannot_open_short_at_max_short_positions(portfolio, mock_broker):
    from system.config import settings
    mock_broker.get_positions.return_value = [
        {"ticker": f"S{i}", "qty": -1.0, "current_price": 100.0, "avg_entry_price": 100.0}
        for i in range(settings.risk.max_short_positions)
    ]
    assert portfolio.can_open_new_short_position() is False

def test_cannot_open_short_after_daily_short_limit(portfolio, mock_broker):
    portfolio._opened_short_today = portfolio._risk.max_short_positions_per_day
    assert portfolio.can_open_new_short_position() is False

def test_open_short_position_places_sell_order(portfolio, mock_broker):
    mock_broker.is_shortable.return_value = True
    mock_broker.shorting_enabled.return_value = True
    portfolio.open_position(
        "TSLA", position_pct=4.0, signal_id=1, rationale="Test",
        entry_price=250.0, direction="short",
    )
    kwargs = mock_broker.place_order.call_args[1]
    assert kwargs["side"] == "sell"

def test_open_short_position_skipped_when_account_cannot_short(portfolio, mock_broker):
    mock_broker.shorting_enabled.return_value = False
    opened = portfolio.open_position(
        "TSLA", position_pct=4.0, signal_id=1, rationale="Test",
        entry_price=250.0, direction="short",
    )
    assert opened is False
    mock_broker.place_order.assert_not_called()

def test_open_short_position_skipped_when_not_shortable(portfolio, mock_broker):
    mock_broker.shorting_enabled.return_value = True
    mock_broker.is_shortable.return_value = False
    opened = portfolio.open_position(
        "GME", position_pct=4.0, signal_id=1, rationale="Test",
        entry_price=25.0, direction="short",
    )
    assert opened is False
    mock_broker.place_order.assert_not_called()

def test_open_short_position_stop_is_above_entry(portfolio, mock_broker):
    """Mirrors test_open_position_uses_broker_fill_data_when_available's mocking
    pattern: a real Order with status set to the OrderStatus.FILLED enum member
    (not `.status.value = "filled"`, which both fails to satisfy the code's
    `order.status == OrderStatus.FILLED` enum comparison and would error outright —
    OrderStatus.FILLED is an enum member and does not allow attribute assignment)."""
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    mock_broker.is_shortable.return_value = True
    mock_broker.shorting_enabled.return_value = True
    filled_order = Order(
        ticker="TSLA", side=OrderSide.SELL, qty=4.0, order_type=OrderType.MARKET,
    )
    filled_order.status = OrderStatus.FILLED
    filled_order.filled_qty = 4.0
    filled_order.filled_avg_price = 250.0
    mock_broker.place_order.return_value = filled_order

    portfolio.open_position(
        "TSLA", position_pct=4.0, signal_id=1, rationale="Test",
        entry_price=250.0, direction="short",
    )
    stop_kwargs = mock_broker.place_stop_order.call_args[1]
    assert stop_kwargs["stop_price"] > 250.0
    assert stop_kwargs["side"] == "buy"
