import pytest
from bot.direction_math import (
    pnl_pct, stop_trigger_price, is_stop_triggered,
)


def test_pnl_pct_long_gain():
    assert pnl_pct("long", entry_price=100.0, current_price=110.0) == pytest.approx(10.0)


def test_pnl_pct_long_loss():
    assert pnl_pct("long", entry_price=100.0, current_price=90.0) == pytest.approx(-10.0)


def test_pnl_pct_short_gain_when_price_falls():
    assert pnl_pct("short", entry_price=100.0, current_price=90.0) == pytest.approx(10.0)


def test_pnl_pct_short_loss_when_price_rises():
    assert pnl_pct("short", entry_price=100.0, current_price=110.0) == pytest.approx(-10.0)


def test_stop_trigger_price_long_is_below_extreme():
    # Long trails the peak downward
    assert stop_trigger_price("long", extreme_price=120.0, stop_pct=15.0) == pytest.approx(102.0)


def test_stop_trigger_price_short_is_above_extreme():
    # Short trails the trough upward
    assert stop_trigger_price("short", extreme_price=80.0, stop_pct=15.0) == pytest.approx(92.0)


def test_is_stop_triggered_long_true_when_current_at_or_below_stop():
    assert is_stop_triggered("long", current_price=100.0, stop_price=102.0) is True
    assert is_stop_triggered("long", current_price=105.0, stop_price=102.0) is False


def test_is_stop_triggered_short_true_when_current_at_or_above_stop():
    assert is_stop_triggered("short", current_price=95.0, stop_price=92.0) is True
    assert is_stop_triggered("short", current_price=90.0, stop_price=92.0) is False


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        pnl_pct("sideways", entry_price=100.0, current_price=100.0)
