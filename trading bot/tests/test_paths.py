"""Tests for system.paths — absolute anchoring of state file paths."""
import os
from system.paths import PROJECT_ROOT, resolve


def test_project_root_is_trading_bot_directory():
    # PROJECT_ROOT is the "trading bot/" directory (parent of system/)
    assert os.path.isdir(os.path.join(PROJECT_ROOT, "system"))
    assert os.path.isdir(os.path.join(PROJECT_ROOT, "bot"))
    assert os.path.basename(PROJECT_ROOT) == "trading bot"


def test_resolve_relative_path_is_anchored_to_project_root():
    result = resolve("trading.db")
    assert os.path.isabs(result)
    assert result == os.path.join(PROJECT_ROOT, "trading.db")


def test_resolve_absolute_path_is_unchanged():
    abs_path = "/tmp/somewhere/custom.db"
    assert resolve(abs_path) == abs_path


def test_settings_paths_are_absolute():
    from system.config import Settings
    s = Settings()
    assert os.path.isabs(s.db_path)
    assert os.path.isabs(s.risk.lock_file_path)
    assert os.path.isabs(s.regime.model_path)
    assert os.path.isabs(s.dashboard.data_store_path)


def test_db_path_definitions_agree(monkeypatch):
    """bot.db, bot.config, and system.config must all resolve DB_PATH identically."""
    monkeypatch.delenv("DB_PATH", raising=False)
    import importlib
    import bot.db
    import bot.config
    import system.config
    importlib.reload(bot.config)
    importlib.reload(system.config)
    assert bot.db._db_path() == bot.config.DB_PATH == system.config.Settings().db_path
