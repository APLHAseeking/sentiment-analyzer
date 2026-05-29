"""Deterministic volatility-targeted position sizing.

Replaces LLM-driven position_pct with a risk-parity-style formula:

    size_pct = clamp(per_trade_risk_pct / atr_pct, 0, max_position_pct)

Where:
- per_trade_risk_pct  : risk budget per trade as % of NAV (e.g. 0.5)
- atr_pct             : 14-bar ATR expressed as % of entry price
- max_position_pct    : hard ceiling from RiskConfig (e.g. 8.0)

A conviction score (1-10) may apply a ±20% tilt but can never push the
result above max_position_pct.
"""
from __future__ import annotations


def vol_target_size_pct(
    atr_pct: float,
    per_trade_risk_pct: float,
    max_position_pct: float,
) -> float:
    """Return position size as % of NAV using volatility targeting.

    Parameters
    ----------
    atr_pct:
        14-bar Average True Range expressed as a percentage of entry price
        (e.g. 2.0 means the ATR is 2% of price). Must be positive; if
        zero or negative, falls back to 1.0 to avoid division-by-zero.
    per_trade_risk_pct:
        Maximum % of NAV we are willing to risk on this trade (e.g. 0.5).
    max_position_pct:
        Hard ceiling on position size as % of NAV (e.g. 8.0).

    Returns
    -------
    float
        Position size in [0, max_position_pct].

    Examples
    --------
    >>> vol_target_size_pct(2.0, 0.5, 8.0)   # 0.5/2.0 = 0.25  → 0.25%
    0.25
    >>> vol_target_size_pct(0.1, 0.5, 8.0)   # 0.5/0.1 = 5.0   → 5.0%
    5.0
    >>> vol_target_size_pct(0.05, 0.5, 8.0)  # 0.5/0.05 = 10.0 → capped at 8.0%
    8.0
    """
    if atr_pct <= 0:
        atr_pct = 1.0  # safe fallback — 1% ATR means full per_trade_risk_pct
    raw = per_trade_risk_pct / atr_pct
    return float(min(max(raw, 0.0), max_position_pct))


def apply_conviction_tilt(
    base_pct: float,
    conviction: int,
    max_position_pct: float,
    tilt_band: float = 0.20,
) -> float:
    """Apply a small conviction-based tilt to a deterministic base size.

    Conviction 5 (minimum buy) → −tilt_band (i.e. −20% of base).
    Conviction 10              → +tilt_band (i.e. +20% of base).
    Conviction 7–8             → near-zero tilt.

    The result is always clamped to [0, max_position_pct].

    Parameters
    ----------
    base_pct:
        Deterministic size from vol_target_size_pct().
    conviction:
        LLM conviction score, expected in [5, 10] for buy signals.
    max_position_pct:
        Hard ceiling — output never exceeds this.
    tilt_band:
        Max fractional tilt (default 0.20 = ±20% of base_pct).
    """
    # Normalise conviction [5, 10] → [-1, +1]
    conv_clamped = max(5, min(10, conviction))
    normalised = (conv_clamped - 7.5) / 2.5   # 5→-1, 7.5→0, 10→+1
    tilt = base_pct * tilt_band * normalised
    tilted = base_pct + tilt
    return float(min(max(tilted, 0.0), max_position_pct))
