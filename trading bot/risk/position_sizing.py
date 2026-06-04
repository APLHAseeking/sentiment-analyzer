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

import numpy as np


def vol_target_size_pct(
    atr_pct: float,
    per_trade_risk_pct: float,
    max_position_pct: float,
) -> float:
    """Return position size as % of NAV using volatility targeting.

    Both ``atr_pct`` and ``per_trade_risk_pct`` are percentages. Risking
    ``per_trade_risk_pct`` of NAV with a stop ≈ 1 ATR away implies a position of
    ``per_trade_risk_pct / atr_pct`` of NAV — a *fraction*, so we multiply by 100
    to return a percentage. Smaller size for more volatile names.

    Parameters
    ----------
    atr_pct:
        14-bar ATR as a percentage of price (e.g. 2.0 means 2%). Must be
        positive; zero/negative falls back to 1.0 to avoid division by zero.
    per_trade_risk_pct:
        % of NAV risked per trade (e.g. 0.15).
    max_position_pct:
        Hard ceiling on position size as % of NAV (e.g. 8.0).

    Returns
    -------
    float : position size in [0, max_position_pct].

    Examples
    --------
    >>> vol_target_size_pct(2.0, 0.15, 8.0)   # 0.15/2.0*100 = 7.5%
    7.5
    >>> vol_target_size_pct(1.0, 0.15, 8.0)   # 0.15/1.0*100 = 15% → capped
    8.0
    >>> vol_target_size_pct(20.0, 0.15, 8.0)  # 0.15/20.0*100 = 0.75%
    0.75
    """
    if atr_pct <= 0:
        atr_pct = 1.0  # safe fallback
    raw = per_trade_risk_pct / atr_pct * 100.0
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


def atr_pct_from_ohlc(
    high,
    low,
    close,
    window: int = 14,
    fallback: float = 1.0,
) -> float:
    """Average True Range as a percentage of the latest close.

    Parameters
    ----------
    high, low, close : array-likes of equal length (oldest → newest).
    window : ATR lookback in bars.
    fallback : returned when there is insufficient history or a non-positive
        last price (keeps sizing conservative rather than crashing).
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if len(close) < window + 1:
        return fallback
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])),
    )
    atr = float(tr[-window:].mean())
    last = close[-1]
    return atr / last * 100.0 if last > 0 else fallback
