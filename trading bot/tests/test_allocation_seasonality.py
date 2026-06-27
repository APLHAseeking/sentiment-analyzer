"""Tests for the Halloween seasonality overlay in AllocationEngine."""
from __future__ import annotations

from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

from regime.allocation_engine import AllocationDecision, AllocationEngine
from regime.hmm_engine import RegimeState


def _make_regime(label="bull", confidence=0.80, is_stable=True):
    state = MagicMock(spec=RegimeState)
    state.regime_label = label
    state.confidence = confidence
    state.is_stable = is_stable
    return state


def _make_engine(enable_seasonality=True, active=1.10, inactive=0.90):
    cfg = MagicMock()
    cfg.allocation.regime_size_multiplier = {"bull": 1.0, "neutral": 0.7, "bear": 0.5}
    cfg.allocation.min_confidence_to_trade = 0.40
    cfg.allocation.confidence_scale = False
    cfg.allocation.instability_penalty = 0.5
    cfg.allocation.enable_seasonality = enable_seasonality
    cfg.allocation.halloween_mult_active = active
    cfg.allocation.halloween_mult_inactive = inactive
    cfg.risk.max_position_pct = 8.0
    return AllocationEngine(cfg)


# ---------------------------------------------------------------------------
# _seasonality_mult
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("month", [11, 12, 1, 2, 3, 4])
def test_seasonality_mult_active_months(month):
    engine = _make_engine()
    date = datetime(2026, month, 15, tzinfo=UTC)
    assert engine._seasonality_mult(date) == pytest.approx(1.10)


@pytest.mark.parametrize("month", [5, 6, 7, 8, 9, 10])
def test_seasonality_mult_inactive_months(month):
    engine = _make_engine()
    date = datetime(2026, month, 15, tzinfo=UTC)
    assert engine._seasonality_mult(date) == pytest.approx(0.90)


def test_seasonality_mult_disabled_always_one():
    engine = _make_engine(enable_seasonality=False)
    for month in range(1, 13):
        date = datetime(2026, month, 15, tzinfo=UTC)
        assert engine._seasonality_mult(date) == pytest.approx(1.0)


def test_seasonality_mult_custom_values():
    engine = _make_engine(active=1.20, inactive=0.80)
    assert engine._seasonality_mult(datetime(2026, 1, 1, tzinfo=UTC)) == pytest.approx(1.20)
    assert engine._seasonality_mult(datetime(2026, 6, 1, tzinfo=UTC)) == pytest.approx(0.80)


def test_seasonality_mult_no_date_uses_current_time():
    """Calling without a date argument should not raise."""
    engine = _make_engine()
    result = engine._seasonality_mult()
    assert result in (pytest.approx(1.10), pytest.approx(0.90))


# ---------------------------------------------------------------------------
# compute() integration
# ---------------------------------------------------------------------------

def test_compute_applies_active_seasonal_mult():
    engine = _make_engine()
    regime = _make_regime(label="bull", confidence=1.0, is_stable=True)
    date = datetime(2026, 1, 15, tzinfo=UTC)  # January → active (1.10)
    decision = engine.compute("AAPL", 5.0, regime, date=date)
    # bull mult=1.0, conf_scale=False → conf_mult=1.0, stab=1.0, seasonal=1.10
    assert decision.seasonal_multiplier == pytest.approx(1.10)
    assert decision.final_position_pct == pytest.approx(5.0 * 1.0 * 1.0 * 1.0 * 1.10)


def test_compute_applies_inactive_seasonal_mult():
    engine = _make_engine()
    regime = _make_regime(label="bull", confidence=1.0, is_stable=True)
    date = datetime(2026, 7, 15, tzinfo=UTC)  # July → inactive (0.90)
    decision = engine.compute("AAPL", 5.0, regime, date=date)
    assert decision.seasonal_multiplier == pytest.approx(0.90)
    assert decision.final_position_pct == pytest.approx(5.0 * 1.0 * 1.0 * 1.0 * 0.90)


def test_compute_seasonal_disabled_no_effect():
    engine = _make_engine(enable_seasonality=False)
    regime = _make_regime(label="bull", confidence=1.0, is_stable=True)
    date = datetime(2026, 1, 15, tzinfo=UTC)
    decision = engine.compute("AAPL", 5.0, regime, date=date)
    assert decision.seasonal_multiplier == pytest.approx(1.0)
    assert decision.final_position_pct == pytest.approx(5.0)


def test_compute_seasonal_capped_by_max_position():
    engine = _make_engine(active=1.10)
    regime = _make_regime(label="bull", confidence=1.0, is_stable=True)
    date = datetime(2026, 1, 15, tzinfo=UTC)
    # Ask for 8.0%; with 1.10× that would be 8.8% but cap is 8.0%
    decision = engine.compute("AAPL", 8.0, regime, date=date)
    assert decision.final_position_pct == pytest.approx(8.0)


def test_zero_decision_has_seasonal_multiplier_one():
    """_zero_decision (confidence too low) should record seasonal_multiplier=1.0."""
    engine = _make_engine()
    regime = _make_regime(label="bull", confidence=0.10, is_stable=True)  # below threshold
    date = datetime(2026, 1, 15, tzinfo=UTC)
    decision = engine.compute("AAPL", 5.0, regime, date=date)
    assert decision.final_position_pct == pytest.approx(0.0)
    assert decision.seasonal_multiplier == pytest.approx(1.0)
