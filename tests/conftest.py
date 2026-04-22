import os

_DEFAULTS = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "ALPACA_API_KEY": "test-alpaca-key",
    "ALPACA_SECRET_KEY": "test-alpaca-secret",
    "PROPUBLICA_API_KEY": "test-propublica-key",
    "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
}
for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import pytest
import importlib

@pytest.fixture(autouse=True, scope="session")
def _test_env():
    """Set dummy env vars so bot.config can be imported during test collection."""
    defaults = {
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "ALPACA_API_KEY": "test-alpaca-key",
        "ALPACA_SECRET_KEY": "test-alpaca-secret",
        "PROPUBLICA_API_KEY": "test-propublica-key",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Initialise a fresh temporary SQLite DB for each test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import bot.db
    importlib.reload(bot.db)
    bot.db.init_db()
    return bot.db

@pytest.fixture
def mock_broker(mocker):
    broker = mocker.MagicMock()
    broker.get_cash.return_value = 100_000.0
    broker.get_positions.return_value = []
    return broker
