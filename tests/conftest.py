import pytest
import importlib

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Initialise a fresh in-memory SQLite DB for each test."""
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
