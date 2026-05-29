"""Unit tests for risk/position_sizing.py — vol_target_size_pct and apply_conviction_tilt."""
import pytest
from risk.position_sizing import vol_target_size_pct, apply_conviction_tilt


class TestVolTargetSizePct:
    """vol_target_size_pct(atr_pct, per_trade_risk_pct, max_position_pct)."""

    def test_normal_calculation(self):
        # 0.5 / 2.0 = 0.25%
        assert vol_target_size_pct(2.0, 0.5, 8.0) == pytest.approx(0.25)

    def test_higher_atr_gives_smaller_size(self):
        """More volatile name → smaller position."""
        low_vol = vol_target_size_pct(1.0, 0.5, 8.0)   # 0.5 / 1.0 = 0.5%
        high_vol = vol_target_size_pct(2.0, 0.5, 8.0)  # 0.5 / 2.0 = 0.25%
        assert low_vol > high_vol

    def test_respects_max_ceiling(self):
        """Low ATR should not exceed max_position_pct."""
        # 0.5 / 0.05 = 10.0, capped at 8.0
        result = vol_target_size_pct(0.05, 0.5, 8.0)
        assert result == pytest.approx(8.0)

    def test_ceiling_at_exact_boundary(self):
        # 0.5 / 0.0625 = 8.0 exactly
        result = vol_target_size_pct(0.0625, 0.5, 8.0)
        assert result == pytest.approx(8.0)

    def test_normal_case_below_ceiling(self):
        # 0.5 / 0.1 = 5.0 — well under 8%
        result = vol_target_size_pct(0.1, 0.5, 8.0)
        assert result == pytest.approx(5.0)

    def test_zero_atr_uses_fallback(self):
        """Zero ATR should not raise ZeroDivisionError; uses 1.0 fallback."""
        result = vol_target_size_pct(0.0, 0.5, 8.0)
        assert result == pytest.approx(0.5)  # 0.5 / 1.0 = 0.5

    def test_negative_atr_uses_fallback(self):
        result = vol_target_size_pct(-1.0, 0.5, 8.0)
        assert result == pytest.approx(0.5)

    def test_result_is_always_non_negative(self):
        for atr in [0.01, 0.5, 1.0, 5.0, 10.0]:
            assert vol_target_size_pct(atr, 0.5, 8.0) >= 0.0

    def test_different_per_trade_risk(self):
        # 1.0 / 2.0 = 0.5%
        assert vol_target_size_pct(2.0, 1.0, 8.0) == pytest.approx(0.5)

    def test_different_ceiling(self):
        # 0.5 / 0.01 = 50% but ceiling is 5%
        assert vol_target_size_pct(0.01, 0.5, 5.0) == pytest.approx(5.0)

    def test_very_high_atr_gives_tiny_size(self):
        # 0.5 / 10.0 = 0.05%
        assert vol_target_size_pct(10.0, 0.5, 8.0) == pytest.approx(0.05)


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
