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
    mock_broker.get_positions.return_value = [{
        "ticker": "TSLA", "qty": 5.0,
        "current_price": 80.0, "avg_entry_price": 100.0,
    }]
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
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge")
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional")
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
    db.insert_position("SH",   100.0, 10.0, 5.0, "2026-04-01", None, "Hedge", "hedge")
    db.insert_position("AAPL", 100.0, 5.0,  4.0, "2026-04-01", sid,  "Test",  "congressional")
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
    # 6% drop — triggers 5% custom threshold, would NOT trigger the default 15%
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
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    closed = p.enforce_stop_losses()   # no explicit pct — must read from injected config
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


def test_trailing_up_cancels_old_stop_before_placing_new(mock_broker, db):
    """On a trail-up the old resting stop must be cancelled before the new one is placed."""
    from system.config import RiskConfig
    portfolio = Portfolio(broker=mock_broker, risk_cfg=RiskConfig(trailing_stop_pct=15.0))
    mock_broker.get_stop_orders.return_value = {}  # no existing stop → will place
    mock_broker.get_positions.return_value = [{
        "ticker": "AAPL", "qty": 10.0, "current_price": 120.0, "avg_entry_price": 100.0,
    }]
    db.insert_position("AAPL", 100.0, 10.0, 5.0, "2026-04-01", None, "Test")

    portfolio.enforce_stop_losses(stop_loss_pct=15.0)

    mock_broker.cancel_stop_order.assert_called_once_with("AAPL")
    mock_broker.place_stop_order.assert_called_once()


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
