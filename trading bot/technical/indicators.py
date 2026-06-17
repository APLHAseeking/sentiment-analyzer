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


def rolling_atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Rolling ATR% array — for the percentile field. The single latest ATR% value used
    for sizing/snapshot should come from risk.position_sizing.atr_pct_from_ohlc instead;
    this function exists only to provide the historical series for _percentile_rank."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window).mean()
    return atr / close * 100.0


def bollinger_bands(close: pd.Series, window: int = 20, num_std: float = 2.0) -> tuple[np.ndarray, np.ndarray]:
    mid = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    band_range = (upper - lower).replace(0.0, np.nan)
    percent_b = (close - lower) / band_range
    bandwidth = (upper - lower) / mid.replace(0.0, np.nan) * 100.0
    return percent_b.to_numpy(), bandwidth.to_numpy()


def _percentile_rank(history, value: float, lookback: int = 252) -> float:
    arr = np.asarray(history, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) == 0:
        return 50.0
    window = arr[-lookback:]
    if len(window) == 0:
        return 50.0
    return float(np.mean(window <= value)) * 100.0
