"""Tests for the risk manager — circuit breakers, lock file, veto logic."""
import os
import pytest
from unittest.mock import patch

from risk.risk_manager import RiskManager, RiskState
from system.config import Settings, RiskConfig


@pytest.fixture(autouse=True)
def _mock_db(mocker):
    """Prevent risk manager from trying to write to a real DB in unit tests."""
    mocker.patch("risk.risk_manager.db.log_risk_event")
    mocker.patch("risk.risk_manager.emit_event")


def _make_manager(tmp_path, **overrides) -> RiskManager:
    risk = RiskConfig(
        daily_loss_reduce_pct=2.0,
        daily_loss_halt_pct=4.0,
        weekly_loss_halt_pct=8.0,
        max_drawdown_lockout_pct=15.0,
        lock_file_path=str(tmp_path / "RISK_LOCKOUT"),
        **overrides,
    )
    from dataclasses import replace
    s = Settings(risk=risk)
    return RiskManager(s)


def test_initial_state_is_normal(tmp_path):
    mgr = _make_manager(tmp_path)
    assert mgr.state == RiskState.NORMAL


def test_start_of_day_sets_baselines(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    assert mgr._peak_nav == 100_000
    assert mgr._day_start_nav == 100_000


def test_small_daily_loss_triggers_size_reduction(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._peak_nav = 100_000
    # 3% daily loss → exceeds reduce threshold (2%) but not halt threshold (4%)
    mgr.check_circuit_breakers(97_000)
    assert mgr.state == RiskState.SIZES_REDUCED


def test_large_daily_loss_halts_entries(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._peak_nav = 100_000
    # 5% daily loss → exceeds halt threshold (4%)
    mgr.check_circuit_breakers(95_000)
    assert mgr.state == RiskState.ENTRIES_HALTED


def test_weekly_loss_triggers_weekly_halt(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._week_start_nav = 100_000
    mgr._peak_nav = 100_000
    # 9% weekly loss → exceeds weekly halt threshold (8%)
    mgr.check_circuit_breakers(91_000)
    assert mgr.state == RiskState.WEEKLY_HALT


def test_max_drawdown_creates_lock_file(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._peak_nav = 100_000
    # 20% drawdown from peak → exceeds lockout threshold (15%)
    mgr.check_circuit_breakers(80_000)
    assert mgr.state == RiskState.LOCKED_OUT
    lock_path = str(tmp_path / "RISK_LOCKOUT")
    assert os.path.exists(lock_path)
    with open(lock_path) as f:
        content = f.read()
    assert "RISK LOCKOUT" in content
    assert "Manual intervention" in content


def test_veto_blocks_when_locked_out(tmp_path):
    lock_path = str(tmp_path / "RISK_LOCKOUT")
    mgr = _make_manager(tmp_path)
    # Create lock file manually
    with open(lock_path, "w") as f:
        f.write("RISK LOCKOUT\n")
    veto = mgr.veto_new_entry("AAPL", 5.0)
    assert not veto.allowed
    assert "lock" in veto.reason.lower()


def test_veto_blocks_when_entries_halted(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._peak_nav = 100_000
    mgr.check_circuit_breakers(95_000)  # triggers halt
    veto = mgr.veto_new_entry("AAPL", 5.0)
    assert not veto.allowed


def test_veto_reduces_size_during_reduce_state(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._peak_nav = 100_000
    mgr.check_circuit_breakers(97_000)  # triggers size reduction
    veto = mgr.veto_new_entry("AAPL", 5.0)
    assert veto.allowed
    assert veto.size_multiplier == 0.5


def test_veto_passes_in_normal_state(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    veto = mgr.veto_new_entry("AAPL", 5.0)
    assert veto.allowed
    assert veto.size_multiplier == 1.0


def test_validate_order_rejects_sector_capped(tmp_path):
    mgr = _make_manager(tmp_path, max_sector_pct=30.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=5.0, sector="Technology",
        sector_allocation={"Technology": 35.0},  # over cap
        position_size_usd=5_000, adv_usd=None,
    )
    assert not veto.allowed
    assert "Sector cap" in veto.reason


def test_validate_order_rejects_illiquid(tmp_path):
    mgr = _make_manager(tmp_path, max_adv_pct=10.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="ILLIQ", position_pct=5.0, sector="Technology",
        sector_allocation={},
        position_size_usd=50_000,   # $50k position
        adv_usd=200_000,            # $200k ADV → 25% of ADV > 10%
    )
    assert not veto.allowed
    assert "Illiquid" in veto.reason


def test_daily_circuit_breaker_resets_at_new_day(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr._peak_nav = 100_000
    mgr.check_circuit_breakers(97_000)
    assert mgr.state == RiskState.SIZES_REDUCED

    # Simulate new day
    import datetime
    from unittest.mock import patch
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    with patch("risk.risk_manager.date") as mock_date:
        mock_date.today.return_value = datetime.date.fromisoformat(tomorrow)
        mock_date.fromisoformat = datetime.date.fromisoformat
        mgr.start_of_day(100_000)

    assert mgr.state == RiskState.NORMAL


def test_is_locked_out_property(tmp_path):
    lock_path = str(tmp_path / "RISK_LOCKOUT")
    mgr = _make_manager(tmp_path)
    assert not mgr.is_locked_out
    with open(lock_path, "w") as f:
        f.write("locked\n")
    assert mgr.is_locked_out


def test_status_dict_includes_max_invested_pct(tmp_path):
    mgr = _make_manager(tmp_path, max_invested_pct=75.0)
    mgr.start_of_day(100_000)
    d = mgr.status_dict()
    assert "max_invested_pct" in d
    assert d["max_invested_pct"] == 75.0


def test_stale_data_blocks_new_entries(tmp_path):
    """After set_stale_data(True), validate_order must return allowed=False with STALE_DATA reason."""
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr.set_stale_data(True)
    veto = mgr.validate_order(
        ticker="AAPL",
        position_pct=5.0,
        sector="Technology",
        sector_allocation={},
        position_size_usd=5_000,
        adv_usd=None,
    )
    assert not veto.allowed
    assert "STALE_DATA" in veto.reason


def test_stale_data_cleared_allows_entries(tmp_path):
    """After set_stale_data(False), validate_order must proceed normally."""
    mgr = _make_manager(tmp_path)
    mgr.start_of_day(100_000)
    mgr.set_stale_data(True)
    mgr.set_stale_data(False)
    veto = mgr.validate_order(
        ticker="AAPL",
        position_pct=5.0,
        sector="Technology",
        sector_allocation={},
        position_size_usd=5_000,
        adv_usd=None,
    )
    assert veto.allowed


def test_validate_order_vetoes_when_invested_cap_would_be_breached(tmp_path):
    mgr = _make_manager(tmp_path, max_invested_pct=80.0, max_position_pct=8.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=5.0, sector="Tech",
        sector_allocation={}, position_size_usd=5_000, adv_usd=1e9,
        current_invested_pct=78.0,  # 78 + 5 = 83 > 80
    )
    assert veto.allowed is False
    assert "invested" in veto.reason.lower()


def test_validate_order_allows_when_under_invested_cap(tmp_path):
    mgr = _make_manager(tmp_path, max_invested_pct=80.0, max_position_pct=8.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=5.0, sector="Tech",
        sector_allocation={}, position_size_usd=5_000, adv_usd=1e9,
        current_invested_pct=50.0,  # 50 + 5 = 55 < 80
    )
    assert veto.allowed is True


def test_validate_order_allows_sector_exactly_at_cap(tmp_path):
    """A sector landing exactly at max_sector_pct (not over) must not be rejected."""
    mgr = _make_manager(tmp_path, max_sector_pct=30.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=1.0, sector="Technology",
        sector_allocation={"Technology": 29.0},  # 29 + 1 = 30, exactly at cap
        position_size_usd=5_000, adv_usd=None,
    )
    assert veto.allowed


def test_validate_order_rejects_when_incoming_position_pushes_sector_over_cap(tmp_path):
    """A sector at 29% plus an incoming 8% position (cap 30%) must be rejected.

    Regression guard for the off-by-one: the check must include the incoming
    position's size, not just the sector's pre-existing allocation.
    """
    mgr = _make_manager(tmp_path, max_sector_pct=30.0)
    mgr.start_of_day(100_000)
    veto = mgr.validate_order(
        ticker="AAPL", position_pct=8.0, sector="Technology",
        sector_allocation={"Technology": 29.0},  # 29 + 8 = 37 > 30
        position_size_usd=5_000, adv_usd=None,
    )
    assert veto.allowed is False
    assert "sector" in veto.reason.lower()


def test_validate_order_logs_warning_when_adv_missing(tmp_path, caplog):
    import logging
    mgr = _make_manager(tmp_path, max_adv_pct=10.0)
    mgr.start_of_day(100_000)
    with caplog.at_level(logging.WARNING, logger="risk.risk_manager"):
        veto = mgr.validate_order(
            ticker="AAPL", position_pct=5.0, sector="Technology",
            sector_allocation={}, position_size_usd=5_000, adv_usd=None,
        )
    assert veto.allowed  # missing ADV data does not block the order
    assert any("ADV" in rec.message or "liquidity" in rec.message.lower()
               for rec in caplog.records)


def test_validate_order_logs_warning_when_adv_zero(tmp_path, caplog):
    import logging
    mgr = _make_manager(tmp_path, max_adv_pct=10.0)
    mgr.start_of_day(100_000)
    with caplog.at_level(logging.WARNING, logger="risk.risk_manager"):
        veto = mgr.validate_order(
            ticker="AAPL", position_pct=5.0, sector="Technology",
            sector_allocation={}, position_size_usd=5_000, adv_usd=0,
        )
    assert veto.allowed
    assert any("ADV" in rec.message or "liquidity" in rec.message.lower()
               for rec in caplog.records)
