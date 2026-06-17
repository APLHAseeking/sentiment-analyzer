"""Deterministic technical-indicator pipeline (hand-rolled, no TA-Lib/pandas-ta).

All functions are causal — they only use data up to the last row passed in.
Series are oldest -> newest. Built incrementally; see compute_snapshot() at the
bottom of this module (added last) for how everything wires together.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def ma_alignment(sma20: float, sma50: float, sma200: float) -> str:
    if sma20 > sma50 > sma200:
        return "bullish"
    if sma20 < sma50 < sma200:
        return "bearish"
    return "mixed"


def sma_slope_pct(sma_series: pd.Series, lookback: int = 20) -> float:
    if len(sma_series) <= lookback:
        return 0.0
    past = sma_series.iloc[-1 - lookback]
    now = sma_series.iloc[-1]
    if past == 0:
        return 0.0
    return float((now - past) / past * 100.0)


def price_vs_sma_pct(price: float, sma_value: float) -> float:
    if sma_value == 0:
        return 0.0
    return float((price - sma_value) / sma_value * 100.0)
