"""Deterministic technical-indicator pipeline (hand-rolled, no TA-Lib/pandas-ta).

All functions are causal — they only use data up to the last row passed in.
Series are oldest -> newest. Built incrementally; see compute_snapshot() at the
bottom of this module (added last) for how everything wires together.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from risk.position_sizing import atr_pct_from_ohlc


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
    """Wilder-smoothed RSI via ewm(alpha=1/window).

    avg_loss==0 and avg_gain>0 (only ever rose) -> 100 (max overbought).
    avg_loss==0 and avg_gain==0 (flat/halted, no movement at all) -> 50 (neutral) —
    distinct from the all-gains case above, not max overbought.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    flat = (avg_loss == 0.0) & (avg_gain == 0.0)
    rsi = rsi.where(~flat, 50.0)
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
    momentum = (
        "expanding" if len(arr) >= 2 and abs(arr[-1]) > abs(arr[-2]) else "fading"
    )
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


def htf_trend_from_weekly(daily_close: pd.Series, window_weeks: int = 30) -> str:
    """Resample to weekly closes, take a window_weeks SMA, compare now vs 4 weeks ago."""
    weekly = daily_close.resample("W").last().dropna()
    sma = weekly.rolling(window_weeks).mean()
    valid = sma.dropna()
    if len(valid) < 5:
        return "flat"
    now = valid.iloc[-1]
    past = valid.iloc[-5]
    if now > past:
        return "up"
    if now < past:
        return "down"
    return "flat"


def dist_to_52w_extremes_pct(close: pd.Series, last_close: float) -> tuple[float, float]:
    window = close.iloc[-252:] if len(close) >= 252 else close
    high_52w = float(window.max())
    low_52w = float(window.min())
    dist_to_high = (last_close - high_52w) / high_52w * 100.0 if high_52w != 0 else 0.0
    dist_to_low = (last_close - low_52w) / low_52w * 100.0 if low_52w != 0 else 0.0
    return dist_to_high, dist_to_low


def relative_strength_pct(asset_close: pd.Series, bench_close: pd.Series, window: int) -> float:
    """Asset's window-bar return minus the benchmark's window-bar return.
    Aligned by date first — a missing/extra row on one side (a halt, a
    stock-specific gap) must not silently shift the other series' bars out
    of calendar sync."""
    asset_aligned, bench_aligned = asset_close.align(bench_close, join="inner")
    asset_ret = pct_return(asset_aligned, bars_back=window)
    bench_ret = pct_return(bench_aligned, bars_back=window)
    return float(asset_ret - bench_ret)


def rs_line_slope(asset_close: pd.Series, bench_close: pd.Series, window: int = 20) -> str:
    n = min(len(asset_close), len(bench_close))
    if n <= window:
        return "flat"
    asset_tail = asset_close.iloc[-n:].to_numpy()
    bench_tail = bench_close.iloc[-n:].to_numpy()
    rs_line = asset_tail / bench_tail
    past = rs_line[-1 - window]
    now = rs_line[-1]
    if now > past:
        return "rising"
    if now < past:
        return "falling"
    return "flat"


@dataclass(frozen=True)
class TechnicalSnapshot:
    ticker: str
    as_of: str
    last_close: float
    htf_trend: str
    htf_above_200d: bool
    dist_to_52w_high_pct: float
    dist_to_52w_low_pct: float
    sma20: float
    sma50: float
    sma200: float
    ma_alignment: str
    sma200_slope_pct_20d: float
    price_vs_sma20_pct: float
    price_vs_sma50_pct: float
    market_structure: str
    ret_1m_pct: float
    ret_3m_pct: float
    ret_6m_pct: float
    ret_12m_1m_pct: float
    tsmom_composite: float
    rsi14: float
    rsi_regime: str
    rsi_divergence: str
    macd_hist: float
    macd_state: str
    atr_pct: float
    atr_pct_percentile_1y: float
    bb_percent_b: float
    bb_bandwidth_percentile_1y: float
    rel_volume_20d: float
    obv_trend: str
    volume_confirms_move: bool
    rs_vs_spy_3m_pct: float
    rs_vs_spy_6m_pct: float
    rs_vs_sector_3m_pct: float | None
    rs_line_slope: str
    nearest_support: float
    nearest_resistance: float
    dist_to_support_pct: float
    dist_to_resistance_pct: float
    fib_levels: dict[str, float]
    anchored_vwap_from_low: float
    bars_available: int
    data_complete: bool
    bb_bands_flat: bool = False


def compute_snapshot(
    ticker: str,
    ohlcv: pd.DataFrame,
    spy_close: pd.Series,
    sector_close: pd.Series | None,
    as_of: str,
) -> TechnicalSnapshot:
    high, low, close, volume = ohlcv["High"], ohlcv["Low"], ohlcv["Close"], ohlcv["Volume"]
    bars_available = len(close)
    data_complete = bars_available >= 250
    last_close = float(close.iloc[-1])

    sma20_series = rolling_sma(close, 20)
    sma50_series = rolling_sma(close, 50)
    sma200_series = rolling_sma(close, 200)
    sma20 = float(sma20_series.iloc[-1]) if not pd.isna(sma20_series.iloc[-1]) else last_close
    sma50 = float(sma50_series.iloc[-1]) if not pd.isna(sma50_series.iloc[-1]) else last_close
    sma200 = float(sma200_series.iloc[-1]) if not pd.isna(sma200_series.iloc[-1]) else last_close

    htf_trend = htf_trend_from_weekly(close)
    dist_to_high_pct, dist_to_low_pct = dist_to_52w_extremes_pct(close, last_close)

    ret_1m = pct_return(close, 22)
    ret_3m = pct_return(close, 65)
    ret_6m = pct_return(close, 130)
    ret_12m_1m = momentum_12m_1m(close)
    tsmom = tsmom_composite(ret_1m, ret_3m, ret_12m_1m)

    rsi_series = compute_rsi(close, window=14)
    rsi14 = float(rsi_series.iloc[-1])
    rsi_regime = "overbought" if rsi14 >= 70 else "oversold" if rsi14 <= 30 else "neutral"

    _, _, macd_hist_arr = compute_macd(close)
    macd_hist = float(macd_hist_arr[-1])
    macd_state = macd_state_from_hist(macd_hist_arr)

    atr_pct = atr_pct_from_ohlc(high.values, low.values, close.values, window=14)
    atr_series = rolling_atr_pct(high, low, close, window=14).to_numpy()
    atr_pct_percentile_1y = _percentile_rank(atr_series, atr_pct, lookback=252)

    percent_b_arr, bandwidth_arr = bollinger_bands(close, window=20, num_std=2.0)
    last_percent_b = percent_b_arr[-1]
    bb_percent_b = float(last_percent_b) if not np.isnan(last_percent_b) else 0.5
    last_bandwidth = bandwidth_arr[-1]
    # A zero-width band (halted/illiquid ticker, zero realized volatility over the
    # window) makes bb_percent_b mathematically degenerate — flag it explicitly so
    # consumers don't mistake the 0.5 default for a genuinely calm/neutral reading.
    bb_bands_flat = bool(not np.isnan(last_bandwidth) and last_bandwidth < 1e-9)
    bb_bandwidth_percentile_1y = _percentile_rank(
        bandwidth_arr, float(last_bandwidth) if not np.isnan(last_bandwidth) else 0.0, lookback=252
    )

    rel_vol = rel_volume(volume, window=20)
    obv_series = compute_obv(close, volume)
    obv_trend = obv_trend_from_series(obv_series, window=20)
    vol_confirms = volume_confirms_move(close, rel_vol)

    pivot_highs = find_pivots(high.values, k=3, kind="high")
    pivot_lows = find_pivots(low.values, k=3, kind="low")
    market_structure = market_structure_from_pivots(pivot_highs, pivot_lows, high, low)
    rsi_divergence = rsi_divergence_from_pivots(pivot_highs, pivot_lows, high, low, rsi_series)
    support, resistance = support_resistance_from_pivots(pivot_highs, pivot_lows, high, low, last_close)
    dist_to_support_pct = (last_close - support) / support * 100.0 if support != 0 else 0.0
    dist_to_resistance_pct = (resistance - last_close) / last_close * 100.0 if last_close != 0 else 0.0

    if pivot_highs and pivot_lows:
        swing_high = float(high.iloc[pivot_highs[-1]])
        swing_low = float(low.iloc[pivot_lows[-1]])
    else:
        swing_high = float(high.max())
        swing_low = float(low.min())
    fib_levels = fib_levels_from_swing(swing_high, swing_low)

    low_idx = int(np.asarray(low.values).argmin())
    anchored_vwap_from_low = anchored_vwap(high, low, close, volume, anchor_idx=low_idx)

    rs_vs_spy_3m_pct = relative_strength_pct(close, spy_close, window=65)
    rs_vs_spy_6m_pct = relative_strength_pct(close, spy_close, window=130)
    rs_vs_sector_3m_pct = (
        relative_strength_pct(close, sector_close, window=65)
        if sector_close is not None else None
    )
    rs_slope = rs_line_slope(close, spy_close, window=20)

    return TechnicalSnapshot(
        ticker=ticker,
        as_of=as_of,
        last_close=last_close,
        htf_trend=htf_trend,
        htf_above_200d=last_close > sma200,
        dist_to_52w_high_pct=dist_to_high_pct,
        dist_to_52w_low_pct=dist_to_low_pct,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        ma_alignment=ma_alignment(sma20, sma50, sma200),
        sma200_slope_pct_20d=sma_slope_pct(sma200_series.dropna(), lookback=20),
        price_vs_sma20_pct=price_vs_sma_pct(last_close, sma20),
        price_vs_sma50_pct=price_vs_sma_pct(last_close, sma50),
        market_structure=market_structure,
        ret_1m_pct=ret_1m,
        ret_3m_pct=ret_3m,
        ret_6m_pct=ret_6m,
        ret_12m_1m_pct=ret_12m_1m,
        tsmom_composite=tsmom,
        rsi14=rsi14,
        rsi_regime=rsi_regime,
        rsi_divergence=rsi_divergence,
        macd_hist=macd_hist,
        macd_state=macd_state,
        atr_pct=atr_pct,
        atr_pct_percentile_1y=atr_pct_percentile_1y,
        bb_percent_b=bb_percent_b,
        bb_bandwidth_percentile_1y=bb_bandwidth_percentile_1y,
        rel_volume_20d=rel_vol,
        obv_trend=obv_trend,
        volume_confirms_move=vol_confirms,
        rs_vs_spy_3m_pct=rs_vs_spy_3m_pct,
        rs_vs_spy_6m_pct=rs_vs_spy_6m_pct,
        rs_vs_sector_3m_pct=rs_vs_sector_3m_pct,
        rs_line_slope=rs_slope,
        nearest_support=support,
        nearest_resistance=resistance,
        dist_to_support_pct=dist_to_support_pct,
        dist_to_resistance_pct=dist_to_resistance_pct,
        fib_levels=fib_levels,
        anchored_vwap_from_low=anchored_vwap_from_low,
        bars_available=bars_available,
        data_complete=data_complete,
        bb_bands_flat=bb_bands_flat,
    )
