import os

_DEFAULTS = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "OPENAI_API_KEY": "test-openai-key",
    "ALPACA_API_KEY": "test-alpaca-key",
    "ALPACA_SECRET_KEY": "test-alpaca-secret",
    "PROPUBLICA_API_KEY": "test-propublica-key",
    "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
    # Empty, not omitted: system.config's load_dotenv() would otherwise fill
    # in the real ALERT_WEBHOOK_URL from .env, and any alert=True code path
    # a test exercises (e.g. an ORDER_REJECTED rejection) posts to the real
    # Slack channel with fake fixture data.
    "ALERT_WEBHOOK_URL": "",
}
for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import pytest
import importlib


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Initialise a fresh temporary SQLite DB for each test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import bot.db
    importlib.reload(bot.db)
    bot.db.init_db()
    # Seed one disclosure + signal so tests can use signal_id=1 without FK violations.
    bot.db.insert_disclosures([{
        "id": "seed-001", "politician": "Test Politician", "ticker": "SEED",
        "transaction_date": "2026-01-01", "disclosure_date": "2026-01-05",
        "transaction_type": "purchase", "amount_range": "$1,001 - $15,000",
        "scraped_at": "2026-01-05T00:00:00",
    }])
    bot.db.insert_signal("seed-001", "SEED", 5, 1.0, "seed signal", [])
    return bot.db

@pytest.fixture
def mock_broker(mocker):
    from execution.broker_interface import Order, OrderSide, OrderStatus, OrderType
    broker = mocker.MagicMock()
    broker.get_cash.return_value = 100_000.0
    broker.get_positions.return_value = []
    broker.get_commission_per_share.return_value = 0.0
    # Return a filled order by default so open/close/reduce write to DB.
    _default_order = Order(
        ticker="MOCK", side=OrderSide.BUY, qty=1.0, order_type=OrderType.MARKET,
    )
    _default_order.status = OrderStatus.FILLED
    _default_order.filled_qty = 1.0
    broker.place_order.return_value = _default_order
    return broker
