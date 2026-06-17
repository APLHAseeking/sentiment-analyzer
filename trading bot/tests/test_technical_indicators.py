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
