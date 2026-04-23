import pytest
from unittest.mock import MagicMock, patch

def test_full_pipeline_no_signals(mocker, db):
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mock_broker = MagicMock()
    mock_broker.get_cash.return_value = 100_000.0
    mock_broker.get_positions.return_value = []
    from bot.portfolio import Portfolio
    from bot.scheduler import run_morning_pipeline
    portfolio = Portfolio(broker=mock_broker)
    run_morning_pipeline(portfolio)
    mock_broker.place_order.assert_not_called()

def test_eod_snapshot_writes_to_db(mocker, db):
    mock_broker = MagicMock()
    mock_broker.get_cash.return_value = 90_000.0
    mock_broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 160.0, "avg_entry_price": 150.0}
    ]
    from bot.portfolio import Portfolio
    from bot.scheduler import run_eod_snapshot
    portfolio = Portfolio(broker=mock_broker)
    run_eod_snapshot(portfolio)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM portfolio_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["total_nav"] == pytest.approx(90_000.0 + 1_600.0)

def test_non_trading_day_skips_pipeline(mocker, db):
    mocker.patch("bot.scheduler._is_trading_day", return_value=False)
    mock_broker = MagicMock()
    mock_broker.get_cash.return_value = 100_000.0
    mock_broker.get_positions.return_value = []
    from bot.portfolio import Portfolio
    from bot.scheduler import run_morning_pipeline
    portfolio = Portfolio(broker=mock_broker)
    run_morning_pipeline(portfolio)
    mock_broker.place_order.assert_not_called()
