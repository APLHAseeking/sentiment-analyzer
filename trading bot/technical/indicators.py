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


def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def rel_volume(volume: pd.Series, window: int = 20) -> float:
    if len(volume) < window + 1:
        return 1.0
    baseline = volume.iloc[-window - 1:-1].mean()
    if baseline == 0:
        return 1.0
    return float(volume.iloc[-1] / baseline)


def obv_trend_from_series(obv: pd.Series, window: int = 20) -> str:
    if len(obv) <= window:
        return "flat"
    past = obv.iloc[-1 - window]
    now = obv.iloc[-1]
    if now > past:
        return "rising"
    if now < past:
        return "falling"
    return "flat"


def volume_confirms_move(close: pd.Series, rel_vol: float) -> bool:
    if len(close) < 2:
        return False
    directional = close.iloc[-1] != close.iloc[-2]
    return bool(directional and rel_vol > 1.0)


def find_pivots(values, k: int = 3, kind: str = "high") -> list[int]:
    """Fixed-lookback local extrema. An index needs k bars on both sides to be
    confirmed, so the most recent k bars never produce a pivot (expected/causal)."""
    arr = np.asarray(values, dtype=float)
    pivots: list[int] = []
    n = len(arr)
    for i in range(k, n - k):
        window = arr[i - k: i + k + 1]
        center = arr[i]
        if kind == "high" and center == window.max():
            pivots.append(i)
        elif kind == "low" and center == window.min():
            pivots.append(i)
    return pivots


def market_structure_from_pivots(
    pivot_highs: list[int], pivot_lows: list[int], high: pd.Series, low: pd.Series
) -> str:
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "range"
    h_prev, h_last = high.iloc[pivot_highs[-2]], high.iloc[pivot_highs[-1]]
    l_prev, l_last = low.iloc[pivot_lows[-2]], low.iloc[pivot_lows[-1]]
    if h_last > h_prev and l_last > l_prev:
        return "HH_HL"
    if h_last < h_prev and l_last < l_prev:
        return "LH_LL"
    return "range"


def rsi_divergence_from_pivots(
    pivot_highs: list[int], pivot_lows: list[int],
    high: pd.Series, low: pd.Series, rsi: pd.Series,
) -> str:
    if len(pivot_lows) >= 2:
        l_prev_idx, l_last_idx = pivot_lows[-2], pivot_lows[-1]
        price_lower_low = low.iloc[l_last_idx] < low.iloc[l_prev_idx]
        rsi_higher_low = rsi.iloc[l_last_idx] > rsi.iloc[l_prev_idx]
        if price_lower_low and rsi_higher_low:
            return "bullish"
    if len(pivot_highs) >= 2:
        h_prev_idx, h_last_idx = pivot_highs[-2], pivot_highs[-1]
        price_higher_high = high.iloc[h_last_idx] > high.iloc[h_prev_idx]
        rsi_lower_high = rsi.iloc[h_last_idx] < rsi.iloc[h_prev_idx]
        if price_higher_high and rsi_lower_high:
            return "bearish"
    return "none"


def support_resistance_from_pivots(
    pivot_highs: list[int], pivot_lows: list[int],
    high: pd.Series, low: pd.Series, last_close: float,
) -> tuple[float, float]:
    """Nearest support (highest pivot low below price) and resistance (lowest pivot
    high above price); falls back to series extremes if no pivot qualifies."""
    low_values = [float(low.iloc[i]) for i in pivot_lows if low.iloc[i] < last_close]
    high_values = [float(high.iloc[i]) for i in pivot_highs if high.iloc[i] > last_close]
    support = max(low_values) if low_values else float(low.min())
    resistance = min(high_values) if high_values else float(high.max())
    return support, resistance


def fib_levels_from_swing(swing_high: float, swing_low: float) -> dict[str, float]:
    diff = swing_high - swing_low
    return {
        "38.2": swing_high - 0.382 * diff,
        "50.0": swing_high - 0.500 * diff,
        "61.8": swing_high - 0.618 * diff,
    }


def anchored_vwap(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, anchor_idx: int,
) -> float:
    typical = (high.iloc[anchor_idx:] + low.iloc[anchor_idx:] + close.iloc[anchor_idx:]) / 3.0
    vol = volume.iloc[anchor_idx:]
    total_vol = vol.sum()
    if total_vol == 0:
        return float(close.iloc[-1])
    return float((typical * vol).sum() / total_vol)
