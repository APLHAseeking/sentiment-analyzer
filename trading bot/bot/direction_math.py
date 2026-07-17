"""Direction-aware P&L/stop/take-profit math shared by long and short positions.

Every direction-conditional calculation in the portfolio lives here — nowhere
else should branch on `direction`. Long: profit when price rises, stop trails
the peak downward, take-profit fires above target. Short: the mirror image.
"""
from __future__ import annotations

_VALID_DIRECTIONS = {"long", "short"}


def _check_direction(direction: str) -> None:
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}")


def pnl_pct(direction: str, entry_price: float, current_price: float) -> float:
    """Unrealized P&L as a percentage of entry price."""
    _check_direction(direction)
    if direction == "long":
        return (current_price - entry_price) / entry_price * 100
    return (entry_price - current_price) / entry_price * 100


def stop_trigger_price(direction: str, extreme_price: float, stop_pct: float) -> float:
    """The trailing-stop price for the current best-case extreme.

    `extreme_price` is the peak (long) or trough (short) seen since entry.
    """
    _check_direction(direction)
    if direction == "long":
        return extreme_price * (1 - stop_pct / 100)
    return extreme_price * (1 + stop_pct / 100)


def is_stop_triggered(direction: str, current_price: float, stop_price: float) -> bool:
    _check_direction(direction)
    if direction == "long":
        return current_price <= stop_price
    return current_price >= stop_price


def is_take_profit_triggered(direction: str, current_price: float, target_price: float) -> bool:
    _check_direction(direction)
    if direction == "long":
        return current_price >= target_price
    return current_price <= target_price
