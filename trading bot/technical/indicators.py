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


def pct_return(close: pd.Series, bars_back: int) -> float:
    if len(close) <= bars_back:
        return 0.0
    past = close.iloc[-1 - bars_back]
    now = close.iloc[-1]
    if past == 0:
        return 0.0
    return float((now - past) / past * 100.0)


def momentum_12m_1m(close: pd.Series) -> float:
    """Classic 12-month-minus-1-month momentum: return from 253 bars ago to 22 bars ago."""
    if len(close) < 253:
        return 0.0
    past = close.iloc[-253]
    recent = close.iloc[-22]
    if past == 0:
        return 0.0
    return float((recent - past) / past * 100.0)


def tsmom_composite(ret_1m_pct: float, ret_3m_pct: float, ret_12m_1m_pct: float) -> float:
    raw = (ret_1m_pct + ret_3m_pct + ret_12m_1m_pct) / 300.0
    return float(np.clip(raw, -1.0, 1.0))


def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder-smoothed RSI via ewm(alpha=1/window). Forced to 100 where avg_loss==0."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    return rsi


def compute_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line.to_numpy(), signal_line.to_numpy(), histogram.to_numpy()


def macd_state_from_hist(hist) -> str:
    arr = np.asarray(hist, dtype=float)
    direction = "bullish" if arr[-1] > 0 else "bearish"
    momentum = "expanding" if abs(arr[-1]) > abs(arr[-2]) else "fading"
    return f"{direction}_{momentum}"
