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
