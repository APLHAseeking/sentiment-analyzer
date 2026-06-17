import numpy as np
import pandas as pd
import pytest

from technical.indicators import rolling_sma, ma_alignment, sma_slope_pct, price_vs_sma_pct


class TestRollingSma:
    def test_sma_of_constant_series_equals_constant(self):
        close = pd.Series(np.full(30, 100.0))
        sma = rolling_sma(close, window=20)
        assert sma.iloc[-1] == pytest.approx(100.0)

    def test_sma_window_matches_manual_mean(self):
        close = pd.Series(np.arange(1, 31, dtype=float))  # 1..30
        sma = rolling_sma(close, window=5)
        # last 5 values: 26,27,28,29,30 -> mean 28
        assert sma.iloc[-1] == pytest.approx(28.0)


class TestMaAlignment:
    def test_bullish_when_strictly_descending_smas(self):
        assert ma_alignment(sma20=110.0, sma50=105.0, sma200=100.0) == "bullish"

    def test_bearish_when_strictly_ascending_smas(self):
        assert ma_alignment(sma20=90.0, sma50=95.0, sma200=100.0) == "bearish"

    def test_mixed_when_not_monotonic(self):
        assert ma_alignment(sma20=100.0, sma50=90.0, sma200=95.0) == "mixed"


class TestSmaSlopePct:
    def test_rising_sma_gives_positive_slope(self):
        sma = pd.Series(np.linspace(100.0, 110.0, 25))
        assert sma_slope_pct(sma, lookback=20) > 0

    def test_flat_sma_gives_zero_slope(self):
        sma = pd.Series(np.full(25, 100.0))
        assert sma_slope_pct(sma, lookback=20) == pytest.approx(0.0)

    def test_too_short_series_returns_zero_not_crash(self):
        sma = pd.Series(np.full(5, 100.0))
        assert sma_slope_pct(sma, lookback=20) == pytest.approx(0.0)


class TestPriceVsSmaPct:
    def test_price_above_sma_is_positive(self):
        assert price_vs_sma_pct(price=110.0, sma_value=100.0) == pytest.approx(10.0)

    def test_price_below_sma_is_negative(self):
        assert price_vs_sma_pct(price=90.0, sma_value=100.0) == pytest.approx(-10.0)


from technical.indicators import pct_return, momentum_12m_1m, tsmom_composite


class TestPctReturn:
    def test_positive_return(self):
        close = pd.Series(np.linspace(100.0, 121.0, 22))
        result = pct_return(close, bars_back=21)
        assert result == pytest.approx((121.0 - 100.0) / 100.0 * 100.0)

    def test_flat_series_zero_return(self):
        close = pd.Series(np.full(30, 100.0))
        assert pct_return(close, bars_back=20) == pytest.approx(0.0)

    def test_insufficient_history_returns_zero(self):
        close = pd.Series([100.0, 101.0])
        assert pct_return(close, bars_back=20) == pytest.approx(0.0)


class TestMomentum12m1m:
    def test_uptrend_gives_positive_momentum(self):
        close = pd.Series(np.linspace(100.0, 200.0, 260))
        assert momentum_12m_1m(close) > 0

    def test_flat_series_gives_zero_momentum(self):
        close = pd.Series(np.full(260, 100.0))
        assert momentum_12m_1m(close) == pytest.approx(0.0)

    def test_short_history_returns_zero(self):
        close = pd.Series(np.full(50, 100.0))
        assert momentum_12m_1m(close) == pytest.approx(0.0)


class TestTsmomComposite:
    def test_all_positive_returns_positive_composite(self):
        assert tsmom_composite(10.0, 20.0, 30.0) == pytest.approx(0.2)

    def test_clips_to_one(self):
        assert tsmom_composite(200.0, 200.0, 200.0) == pytest.approx(1.0)

    def test_clips_to_negative_one(self):
        assert tsmom_composite(-200.0, -200.0, -200.0) == pytest.approx(-1.0)


from technical.indicators import compute_rsi, compute_macd, macd_state_from_hist


class TestComputeRsi:
    def test_monotonic_uptrend_gives_rsi_100(self):
        close = pd.Series(np.arange(1.0, 31.0))
        rsi = compute_rsi(close, window=14)
        assert rsi.iloc[-1] == pytest.approx(100.0)

    def test_monotonic_downtrend_gives_rsi_0(self):
        close = pd.Series(np.arange(30.0, 0.0, -1.0))
        rsi = compute_rsi(close, window=14)
        assert rsi.iloc[-1] == pytest.approx(0.0)

    def test_alternating_moves_give_rsi_near_50(self):
        vals = [100.0]
        for i in range(100):
            vals.append(vals[-1] + (1.0 if i % 2 == 0 else -1.0))
        close = pd.Series(vals)
        rsi = compute_rsi(close, window=14)
        assert rsi.iloc[-1] == pytest.approx(50.0, abs=5.0)


class TestComputeMacd:
    def test_returns_three_arrays_of_equal_length(self):
        close = pd.Series(np.linspace(100.0, 150.0, 60))
        macd_line, signal_line, hist = compute_macd(close)
        assert len(macd_line) == len(signal_line) == len(hist) == 60

    def test_uptrend_gives_positive_macd_line(self):
        close = pd.Series(np.linspace(100.0, 150.0, 60))
        macd_line, _, _ = compute_macd(close)
        assert macd_line[-1] > 0

    def test_downtrend_gives_negative_macd_line(self):
        close = pd.Series(np.linspace(150.0, 100.0, 60))
        macd_line, _, _ = compute_macd(close)
        assert macd_line[-1] < 0


class TestMacdStateFromHist:
    def test_bullish_expanding(self):
        assert macd_state_from_hist([0.1, 0.2, 0.5]) == "bullish_expanding"

    def test_bullish_fading(self):
        assert macd_state_from_hist([0.1, 0.5, 0.2]) == "bullish_fading"

    def test_bearish_expanding(self):
        assert macd_state_from_hist([-0.1, -0.2, -0.5]) == "bearish_expanding"

    def test_bearish_fading(self):
        assert macd_state_from_hist([-0.1, -0.5, -0.2]) == "bearish_fading"


from risk.position_sizing import atr_pct_from_ohlc
from technical.indicators import rolling_atr_pct, bollinger_bands, _percentile_rank


class TestRollingAtrPct:
    def test_constant_range_gives_expected_pct(self):
        n = 30
        close = pd.Series(np.full(n, 100.0))
        high = close + 1.0
        low = close - 1.0
        result = rolling_atr_pct(high, low, close, window=14)
        assert result.iloc[-1] == pytest.approx(2.0, abs=0.1)

    def test_matches_scalar_atr_pct_from_ohlc(self):
        n = 30
        close = pd.Series(np.full(n, 100.0))
        high = close + 1.0
        low = close - 1.0
        rolling_result = rolling_atr_pct(high, low, close, window=14)
        scalar_result = atr_pct_from_ohlc(high.values, low.values, close.values, window=14)
        assert rolling_result.iloc[-1] == pytest.approx(scalar_result, abs=0.05)


class TestBollingerBands:
    def test_noisy_series_gives_finite_percent_b_and_bandwidth(self):
        np.random.seed(0)
        close = pd.Series(100.0 + np.random.normal(0, 1.0, 30))
        percent_b, bandwidth = bollinger_bands(close, window=20, num_std=2.0)
        assert not np.isnan(percent_b[-1])
        assert not np.isnan(bandwidth[-1])

    def test_flat_series_gives_zero_bandwidth(self):
        close = pd.Series(np.full(30, 100.0))
        _, bandwidth = bollinger_bands(close, window=20, num_std=2.0)
        assert bandwidth[-1] == pytest.approx(0.0, abs=1e-6)


class TestPercentileRank:
    def test_max_value_gives_100th_percentile(self):
        history = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert _percentile_rank(history, value=5.0, lookback=5) == pytest.approx(100.0)

    def test_min_value_gives_lowest_percentile(self):
        history = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert _percentile_rank(history, value=1.0, lookback=5) == pytest.approx(20.0)

    def test_empty_history_returns_50(self):
        assert _percentile_rank(np.array([]), value=1.0, lookback=5) == pytest.approx(50.0)


from technical.indicators import compute_obv, rel_volume, obv_trend_from_series, volume_confirms_move


class TestComputeObv:
    def test_rising_close_gives_rising_obv(self):
        close = pd.Series(np.linspace(100.0, 110.0, 10))
        volume = pd.Series(np.full(10, 1000.0))
        obv = compute_obv(close, volume)
        assert obv.iloc[-1] > obv.iloc[0]

    def test_falling_close_gives_falling_obv(self):
        close = pd.Series(np.linspace(110.0, 100.0, 10))
        volume = pd.Series(np.full(10, 1000.0))
        obv = compute_obv(close, volume)
        assert obv.iloc[-1] < 0


class TestRelVolume:
    def test_spike_volume_gives_high_rel_volume(self):
        volume = pd.Series(np.full(25, 1000.0))
        volume.iloc[-1] = 5000.0
        assert rel_volume(volume, window=20) == pytest.approx(5.0, abs=0.1)

    def test_insufficient_history_returns_one(self):
        volume = pd.Series(np.full(5, 1000.0))
        assert rel_volume(volume, window=20) == pytest.approx(1.0)


class TestObvTrendFromSeries:
    def test_rising_obv_detected(self):
        obv = pd.Series(np.linspace(0.0, 1000.0, 30))
        assert obv_trend_from_series(obv, window=20) == "rising"

    def test_falling_obv_detected(self):
        obv = pd.Series(np.linspace(1000.0, 0.0, 30))
        assert obv_trend_from_series(obv, window=20) == "falling"

    def test_flat_obv_detected(self):
        obv = pd.Series(np.full(30, 500.0))
        assert obv_trend_from_series(obv, window=20) == "flat"


class TestVolumeConfirmsMove:
    def test_directional_bar_with_high_volume_confirms(self):
        close = pd.Series([100.0, 102.0])
        assert volume_confirms_move(close, rel_vol=1.5) is True

    def test_directional_bar_with_low_volume_does_not_confirm(self):
        close = pd.Series([100.0, 102.0])
        assert volume_confirms_move(close, rel_vol=0.8) is False

    def test_flat_bar_does_not_confirm_even_with_high_volume(self):
        close = pd.Series([100.0, 100.0])
        assert volume_confirms_move(close, rel_vol=1.5) is False


from technical.indicators import find_pivots, market_structure_from_pivots, rsi_divergence_from_pivots


class TestFindPivots:
    def test_finds_single_peak(self):
        values = [1, 2, 3, 5, 3, 2, 1, 1, 1, 1]
        assert 3 in find_pivots(values, k=3, kind="high")

    def test_finds_single_trough(self):
        values = [9, 8, 7, 1, 7, 8, 9, 9, 9, 9]
        assert 3 in find_pivots(values, k=3, kind="low")

    def test_no_pivots_in_monotonic_series(self):
        values = list(range(20))
        assert find_pivots(values, k=3, kind="high") == []


class TestMarketStructureFromPivots:
    def test_higher_highs_and_higher_lows_is_uptrend_structure(self):
        high = pd.Series([10.0, 12.0, 11.0, 15.0, 13.0])
        low = pd.Series([8.0, 9.0, 9.5, 11.0, 12.0])
        result = market_structure_from_pivots(
            pivot_highs=[1, 3], pivot_lows=[0, 2], high=high, low=low
        )
        assert result == "HH_HL"

    def test_lower_highs_and_lower_lows_is_downtrend_structure(self):
        high = pd.Series([15.0, 13.0, 12.0, 10.0, 9.0])
        low = pd.Series([12.0, 11.0, 9.0, 8.0, 7.0])
        result = market_structure_from_pivots(
            pivot_highs=[0, 1], pivot_lows=[2, 3], high=high, low=low
        )
        assert result == "LH_LL"

    def test_fewer_than_two_pivots_each_side_is_range(self):
        high = pd.Series([10.0, 12.0])
        low = pd.Series([8.0, 9.0])
        assert market_structure_from_pivots([1], [0], high, low) == "range"


class TestRsiDivergenceFromPivots:
    def test_bullish_divergence_detected(self):
        low = pd.Series([10.0, 9.0, 8.0, 7.0])
        high = pd.Series([20.0, 21.0, 22.0, 23.0])
        rsi = pd.Series([30.0, 25.0, 35.0, 40.0])
        result = rsi_divergence_from_pivots(
            pivot_highs=[], pivot_lows=[1, 3], high=high, low=low, rsi=rsi
        )
        assert result == "bullish"

    def test_bearish_divergence_detected(self):
        high = pd.Series([20.0, 22.0, 24.0, 26.0])
        low = pd.Series([10.0, 11.0, 12.0, 13.0])
        rsi = pd.Series([70.0, 75.0, 65.0, 60.0])
        result = rsi_divergence_from_pivots(
            pivot_highs=[1, 3], pivot_lows=[], high=high, low=low, rsi=rsi
        )
        assert result == "bearish"

    def test_no_divergence_when_price_and_rsi_agree(self):
        low = pd.Series([10.0, 9.0, 8.0, 7.0])
        high = pd.Series([20.0, 21.0, 22.0, 23.0])
        rsi = pd.Series([30.0, 25.0, 20.0, 15.0])
        result = rsi_divergence_from_pivots(
            pivot_highs=[], pivot_lows=[1, 3], high=high, low=low, rsi=rsi
        )
        assert result == "none"


from technical.indicators import support_resistance_from_pivots, fib_levels_from_swing, anchored_vwap


class TestSupportResistanceFromPivots:
    def test_picks_nearest_pivot_below_and_above_price(self):
        high = pd.Series([110.0, 120.0, 130.0])
        low = pd.Series([90.0, 80.0, 70.0])
        support, resistance = support_resistance_from_pivots(
            pivot_highs=[0, 1, 2], pivot_lows=[0, 1, 2],
            high=high, low=low, last_close=100.0,
        )
        assert support == pytest.approx(90.0)
        assert resistance == pytest.approx(110.0)

    def test_falls_back_to_series_extremes_when_no_pivot_qualifies(self):
        high = pd.Series([50.0, 60.0])     # all below last_close -> no resistance pivot
        low = pd.Series([200.0, 210.0])    # all above last_close -> no support pivot
        support, resistance = support_resistance_from_pivots(
            pivot_highs=[0, 1], pivot_lows=[0, 1],
            high=high, low=low, last_close=100.0,
        )
        assert support == pytest.approx(200.0)   # low.min()
        assert resistance == pytest.approx(60.0)  # high.max()


class TestFibLevelsFromSwing:
    def test_levels_between_high_and_low(self):
        levels = fib_levels_from_swing(swing_high=200.0, swing_low=100.0)
        assert levels["38.2"] == pytest.approx(161.8)
        assert levels["50.0"] == pytest.approx(150.0)
        assert levels["61.8"] == pytest.approx(138.2)


class TestAnchoredVwap:
    def test_constant_price_gives_same_vwap(self):
        n = 10
        high = pd.Series(np.full(n, 101.0))
        low = pd.Series(np.full(n, 99.0))
        close = pd.Series(np.full(n, 100.0))
        volume = pd.Series(np.full(n, 1000.0))
        result = anchored_vwap(high, low, close, volume, anchor_idx=0)
        assert result == pytest.approx(100.0)

    def test_zero_volume_falls_back_to_last_close(self):
        n = 5
        high = pd.Series(np.full(n, 101.0))
        low = pd.Series(np.full(n, 99.0))
        close = pd.Series(np.full(n, 100.0))
        volume = pd.Series(np.zeros(n))
        result = anchored_vwap(high, low, close, volume, anchor_idx=0)
        assert result == pytest.approx(100.0)


from technical.indicators import htf_trend_from_weekly, dist_to_52w_extremes_pct


class TestHtfTrendFromWeekly:
    def test_uptrend_daily_series_gives_up(self):
        idx = pd.date_range("2024-01-01", periods=500, freq="D")
        close = pd.Series(np.linspace(100.0, 300.0, 500), index=idx)
        assert htf_trend_from_weekly(close) == "up"

    def test_downtrend_daily_series_gives_down(self):
        idx = pd.date_range("2024-01-01", periods=500, freq="D")
        close = pd.Series(np.linspace(300.0, 100.0, 500), index=idx)
        assert htf_trend_from_weekly(close) == "down"

    def test_short_history_gives_flat(self):
        idx = pd.date_range("2024-01-01", periods=20, freq="D")
        close = pd.Series(np.full(20, 100.0), index=idx)
        assert htf_trend_from_weekly(close) == "flat"


class TestDistTo52wExtremesPct:
    def test_price_at_high_gives_zero_distance_to_high(self):
        close = pd.Series(np.linspace(50.0, 100.0, 252))
        dist_high, dist_low = dist_to_52w_extremes_pct(close, last_close=100.0)
        assert dist_high == pytest.approx(0.0, abs=0.01)
        assert dist_low > 0

    def test_price_at_low_gives_zero_distance_to_low(self):
        close = pd.Series(np.linspace(50.0, 100.0, 252))
        dist_high, dist_low = dist_to_52w_extremes_pct(close, last_close=50.0)
        assert dist_low == pytest.approx(0.0, abs=0.01)
        assert dist_high < 0


from technical.indicators import relative_strength_pct, rs_line_slope


class TestRelativeStrengthPct:
    def test_outperformance_gives_positive_relative_strength(self):
        asset = pd.Series(np.linspace(100.0, 130.0, 70))   # +30%
        bench = pd.Series(np.linspace(100.0, 110.0, 70))   # +10%
        assert relative_strength_pct(asset, bench, window=60) > 0

    def test_underperformance_gives_negative_relative_strength(self):
        asset = pd.Series(np.linspace(100.0, 105.0, 70))   # +5%
        bench = pd.Series(np.linspace(100.0, 120.0, 70))   # +20%
        assert relative_strength_pct(asset, bench, window=60) < 0


class TestRsLineSlope:
    def test_asset_outpacing_benchmark_is_rising(self):
        asset = pd.Series(np.linspace(100.0, 150.0, 40))
        bench = pd.Series(np.linspace(100.0, 110.0, 40))
        assert rs_line_slope(asset, bench, window=20) == "rising"

    def test_asset_lagging_benchmark_is_falling(self):
        asset = pd.Series(np.linspace(100.0, 105.0, 40))
        bench = pd.Series(np.linspace(100.0, 150.0, 40))
        assert rs_line_slope(asset, bench, window=20) == "falling"


from technical.indicators import TechnicalSnapshot, compute_snapshot


def _make_ohlcv(n: int, start_price: float = 100.0, trend: float = 0.0) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame: close compounds by `trend` per bar (e.g. 0.003 = +0.3%/bar)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    closes = start_price * (1.0 + trend) ** np.arange(n)
    high = closes * 1.01
    low = closes * 0.99
    volume = np.full(n, 1_000_000.0)
    return pd.DataFrame({"High": high, "Low": low, "Close": closes, "Volume": volume}, index=idx)


class TestComputeSnapshot:
    def test_uptrend_snapshot_has_expected_shape_and_bullish_signals(self):
        ohlcv = _make_ohlcv(300, start_price=100.0, trend=0.003)
        spy_close = pd.Series(np.linspace(400.0, 420.0, 300))
        snapshot = compute_snapshot(
            ticker="TEST", ohlcv=ohlcv, spy_close=spy_close,
            sector_close=None, as_of="2026-06-17",
        )
        assert isinstance(snapshot, TechnicalSnapshot)
        assert snapshot.ticker == "TEST"
        assert snapshot.bars_available == 300
        assert snapshot.data_complete is True
        assert snapshot.ma_alignment == "bullish"
        assert snapshot.htf_trend == "up"
        assert snapshot.rs_vs_spy_3m_pct > 0
        assert snapshot.rs_vs_sector_3m_pct is None
        assert isinstance(snapshot.fib_levels, dict)

    def test_short_history_marks_data_incomplete_without_crashing(self):
        ohlcv = _make_ohlcv(50, start_price=100.0, trend=0.001)
        spy_close = pd.Series(np.linspace(400.0, 410.0, 50))
        snapshot = compute_snapshot(
            ticker="SHORT", ohlcv=ohlcv, spy_close=spy_close,
            sector_close=None, as_of="2026-06-17",
        )
        assert snapshot.bars_available == 50
        assert snapshot.data_complete is False

    def test_sector_close_provided_computes_relative_strength(self):
        ohlcv = _make_ohlcv(300, start_price=100.0, trend=0.003)
        spy_close = pd.Series(np.linspace(400.0, 420.0, 300))
        sector_close = pd.Series(np.linspace(50.0, 55.0, 300))
        snapshot = compute_snapshot(
            ticker="TEST", ohlcv=ohlcv, spy_close=spy_close,
            sector_close=sector_close, as_of="2026-06-17",
        )
        assert snapshot.rs_vs_sector_3m_pct is not None
