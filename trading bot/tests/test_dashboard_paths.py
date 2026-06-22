"""Tests for dashboard/app.py — DB_PATH/STATE_PATH must anchor to PROJECT_ROOT,
the same way system/paths.py fixed every other relative state-file path."""
import importlib
import os

from system.paths import PROJECT_ROOT


def test_dashboard_db_path_is_absolute_and_anchored(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    import dashboard.app as dashboard_app
    importlib.reload(dashboard_app)
    assert os.path.isabs(dashboard_app.DB_PATH)
    assert dashboard_app.DB_PATH == os.path.join(PROJECT_ROOT, "trading.db")


def test_dashboard_state_path_is_absolute_and_anchored(monkeypatch):
    monkeypatch.delenv("DASHBOARD_STATE", raising=False)
    import dashboard.app as dashboard_app
    importlib.reload(dashboard_app)
    assert os.path.isabs(dashboard_app.STATE_PATH)
    assert dashboard_app.STATE_PATH == os.path.join(PROJECT_ROOT, "dashboard_state.json")


def test_dashboard_db_path_respects_absolute_env_override(monkeypatch, tmp_path):
    abs_override = str(tmp_path / "custom.db")
    monkeypatch.setenv("DB_PATH", abs_override)
    import dashboard.app as dashboard_app
    importlib.reload(dashboard_app)
    assert dashboard_app.DB_PATH == abs_override


def test_dashboard_state_path_respects_absolute_env_override(monkeypatch, tmp_path):
    abs_override = str(tmp_path / "custom_state.json")
    monkeypatch.setenv("DASHBOARD_STATE", abs_override)
    import dashboard.app as dashboard_app
    importlib.reload(dashboard_app)
    assert dashboard_app.STATE_PATH == abs_override


def test_dashboard_paths_are_anchored_regardless_of_cwd(monkeypatch, tmp_path):
    """The original bug: running `streamlit run dashboard/app.py` from a
    different cwd silently read/wrote the wrong files. Anchoring must not
    depend on process cwd at import time."""
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("DASHBOARD_STATE", raising=False)
    monkeypatch.chdir(tmp_path)
    import dashboard.app as dashboard_app
    importlib.reload(dashboard_app)
    assert dashboard_app.DB_PATH == os.path.join(PROJECT_ROOT, "trading.db")
    assert dashboard_app.STATE_PATH == os.path.join(PROJECT_ROOT, "dashboard_state.json")
