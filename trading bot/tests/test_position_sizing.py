"""Unit tests for risk/position_sizing.py — vol_target_size_pct and apply_conviction_tilt."""
import pytest
from risk.position_sizing import vol_target_size_pct, apply_conviction_tilt


class TestVolTargetSizePct:
    """vol_target_size_pct(atr_pct, per_trade_risk_pct, max_position_pct).

    Position % of NAV = per_trade_risk_pct / atr_pct * 100, clamped to max.
    Both inputs are percentages (e.g. atr_pct=2.0 means 2%).
    """

    def test_two_pct_atr_is_a_real_position_not_microscopic(self):
        # Regression for the units bug: 0.15 / 2.0 * 100 = 7.5% (NOT 0.075%)
        assert vol_target_size_pct(2.0, 0.15, 8.0) == pytest.approx(7.5)

    def test_three_pct_atr_below_ceiling(self):
        # 0.15 / 3.0 * 100 = 5.0%
        assert vol_target_size_pct(3.0, 0.15, 8.0) == pytest.approx(5.0)

    def test_higher_atr_gives_smaller_size(self):
        low_vol = vol_target_size_pct(3.0, 0.15, 8.0)   # 5.0%
        high_vol = vol_target_size_pct(6.0, 0.15, 8.0)  # 2.5%
        assert low_vol > high_vol

    def test_low_vol_name_caps_at_ceiling(self):
        # 0.15 / 1.0 * 100 = 15% → capped at 8.0
        assert vol_target_size_pct(1.0, 0.15, 8.0) == pytest.approx(8.0)

    def test_respects_custom_ceiling(self):
        # 0.15 / 1.0 * 100 = 15% → capped at 5.0
        assert vol_target_size_pct(1.0, 0.15, 5.0) == pytest.approx(5.0)

    def test_zero_atr_uses_fallback_not_crash(self):
        # atr<=0 → fallback 1.0 → 0.05 / 1.0 * 100 = 5.0% (no ZeroDivisionError)
        assert vol_target_size_pct(0.0, 0.05, 8.0) == pytest.approx(5.0)

    def test_negative_atr_uses_fallback(self):
        assert vol_target_size_pct(-1.0, 0.05, 8.0) == pytest.approx(5.0)

    def test_result_is_always_non_negative(self):
        for atr in [0.01, 0.5, 1.0, 5.0, 10.0]:
            assert vol_target_size_pct(atr, 0.15, 8.0) >= 0.0

    def test_higher_risk_budget_gives_bigger_size(self):
        # 0.30 / 6.0 * 100 = 5.0% vs 0.15 / 6.0 * 100 = 2.5%
        assert vol_target_size_pct(6.0, 0.30, 8.0) == pytest.approx(5.0)
        assert vol_target_size_pct(6.0, 0.15, 8.0) == pytest.approx(2.5)

    def test_very_high_atr_gives_small_but_nonzero_size(self):
        # 0.15 / 20.0 * 100 = 0.75%
        assert vol_target_size_pct(20.0, 0.15, 8.0) == pytest.approx(0.75)


class TestApplyConvictionTilt:
    """apply_conviction_tilt must never exceed max_position_pct."""

    def test_conviction_10_tilts_up(self):
        base = 2.0
        result = apply_conviction_tilt(base, 10, 8.0, tilt_band=0.20)
        assert result > base

    def test_conviction_5_tilts_down(self):
        base = 2.0
        result = apply_conviction_tilt(base, 5, 8.0, tilt_band=0.20)
        assert result < base

    def test_never_exceeds_max(self):
        # Even conviction=10 with large base must not exceed max_position_pct
        result = apply_conviction_tilt(8.0, 10, 8.0, tilt_band=0.20)
        assert result <= 8.0

    def test_never_exceeds_max_with_high_base(self):
        result = apply_conviction_tilt(7.5, 10, 8.0, tilt_band=0.20)
        assert result <= 8.0

    def test_never_negative(self):
        result = apply_conviction_tilt(0.1, 5, 8.0, tilt_band=0.20)
        assert result >= 0.0

    def test_conviction_out_of_range_is_clamped(self):
        """Conviction > 10 should be treated as 10."""
        r10 = apply_conviction_tilt(2.0, 10, 8.0)
        r99 = apply_conviction_tilt(2.0, 99, 8.0)
        assert r10 == pytest.approx(r99)

    def test_conviction_below_5_clamped_to_5(self):
        r5 = apply_conviction_tilt(2.0, 5, 8.0)
        r1 = apply_conviction_tilt(2.0, 1, 8.0)
        assert r5 == pytest.approx(r1)

    def test_tilt_is_symmetric_around_neutral(self):
        """conviction=10 tilt up ≈ conviction=5 tilt down (symmetric around 7.5)."""
        base = 3.0
        up = apply_conviction_tilt(base, 10, 8.0, tilt_band=0.20)
        down = apply_conviction_tilt(base, 5, 8.0, tilt_band=0.20)
        assert abs((up - base) + (down - base)) < 1e-9


import numpy as np
from risk.position_sizing import atr_pct_from_ohlc


class TestAtrPctFromOhlc:
    def test_constant_range_gives_expected_atr_pct(self):
        # Each bar has a true range of 2.0 around a ~100 close → ATR=2, atr_pct≈2%
        n = 20
        close = np.full(n, 100.0)
        high = close + 1.0
        low = close - 1.0
        result = atr_pct_from_ohlc(high, low, close, window=14)
        assert result == pytest.approx(2.0, abs=0.1)

    def test_insufficient_history_returns_fallback(self):
        close = np.array([100.0, 101.0, 102.0])  # < window+1
        assert atr_pct_from_ohlc(close + 1, close - 1, close, window=14) == pytest.approx(1.0)

    def test_zero_last_price_returns_fallback(self):
        n = 20
        close = np.concatenate([np.full(n - 1, 100.0), [0.0]])
        high = close + 1.0
        low = close - 1.0
        assert atr_pct_from_ohlc(high, low, close, window=14, fallback=1.0) == pytest.approx(1.0)


from risk.position_sizing import vol_pct_from_close


class TestVolPctFromClose:
    def test_flat_prices_have_zero_vol(self):
        close = np.full(30, 100.0)
        assert vol_pct_from_close(close, window=14) == pytest.approx(0.0)

    def test_one_pct_daily_moves_give_one_pct_vol(self):
        # Alternating +1% / -1% closes → mean abs daily return ≈ 1%
        close = [100.0]
        for i in range(30):
            close.append(close[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
        result = vol_pct_from_close(np.array(close), window=14)
        assert result == pytest.approx(1.0, abs=0.1)

    def test_insufficient_history_returns_fallback(self):
        assert vol_pct_from_close(np.array([100.0, 101.0]), window=14) == pytest.approx(1.0)
