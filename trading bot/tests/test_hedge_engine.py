"""Unit tests for HedgeEngine — compute_hedge_plan and get_exits_needed."""
import pytest
from regime.hmm_engine import RegimeState


def _regime(label: str, confidence: float = 0.85) -> RegimeState:
    return RegimeState(
        date="2026-05-08",
        regime_index=0,
        regime_label=label,
        confidence=confidence,
        is_stable=True,
        n_regimes=5,
        raw_posteriors=[0.85, 0.05, 0.05, 0.03, 0.02],
    )


def _engine(enable: bool = True):
    from system.config import Settings, RiskConfig
    cfg = Settings(risk=RiskConfig(enable_inverse_hedging=enable))
    from hedge.hedge_engine import HedgeEngine
    return HedgeEngine(cfg)


def test_is_hedge_regime_true_for_bear_crash_deep_bear():
    e = _engine()
    assert e.is_hedge_regime(_regime("bear")) is True
    assert e.is_hedge_regime(_regime("crash")) is True
    assert e.is_hedge_regime(_regime("deep-bear")) is True


def test_is_hedge_regime_false_for_neutral_bull():
    e = _engine()
    assert e.is_hedge_regime(_regime("neutral")) is False
    assert e.is_hedge_regime(_regime("bull")) is False
    assert e.is_hedge_regime(_regime("euphoria")) is False


def test_compute_hedge_plan_returns_empty_when_kill_switch_off():
    e = _engine(enable=False)
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    assert result == []


def test_compute_hedge_plan_returns_empty_when_not_hedge_regime():
    e = _engine()
    result = e.compute_hedge_plan(_regime("neutral"), [], {}, 100_000)
    assert result == []


def test_compute_hedge_plan_reduces_alloc_by_existing_short_pct():
    """An existing 10% of NAV in per-stock shorts must reduce the regime's
    30% inverse-allocation cap to 20% before sizing hedge ETFs — the two
    mechanisms (per-stock shorts, broad ETF hedge) shouldn't double-hedge
    the same bearish thesis (design spec's open question 2)."""
    e = _engine()
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000, existing_short_pct=10.0)
    assert len(result) == 5
    for order in result:
        assert order.position_pct == pytest.approx(4.0)  # min(20/5, 15)


def test_compute_hedge_plan_existing_short_fully_covers_regime_cap():
    """Existing short exposure at or above the regime's inverse-allocation cap
    must skip opening any new ETF hedge entirely, not just shrink it to zero
    per-ETF (which would still attempt orders below the economic floor)."""
    e = _engine()
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000, existing_short_pct=30.0)
    assert result == []


def test_compute_hedge_plan_existing_short_pct_defaults_to_zero():
    """Omitting existing_short_pct must behave exactly as before (unchanged
    default hedge sizing) — proves the new parameter is additive, not a
    behavior change for existing call sites that don't pass it."""
    e = _engine()
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    assert len(result) == 5
    for order in result:
        assert order.position_pct == pytest.approx(6.0)  # min(30/5, 15), unchanged


def test_compute_hedge_plan_excludes_already_open_hedge_tickers():
    e = _engine()
    open_positions = [{"ticker": "SH", "signal_source": "hedge"}]
    result = e.compute_hedge_plan(_regime("bear"), open_positions, {}, 100_000)
    tickers = [o.ticker for o in result]
    assert "SH" not in tickers
    assert len(result) == 4  # 5 ETFs - 1 already open


def test_compute_hedge_plan_excludes_conflicting_etfs():
    e = _engine()
    # PSQ conflicts with Technology + Communication Services
    sector_alloc = {"Technology": 15.0}  # 15% > 10% threshold
    result = e.compute_hedge_plan(_regime("bear"), [], sector_alloc, 100_000)
    tickers = [o.ticker for o in result]
    assert "PSQ" not in tickers
    assert len(result) == 4  # SH, RWM, SBB, EFZ eligible


def test_compute_hedge_plan_equal_weights_eligible_etfs():
    e = _engine()
    # Bear regime: max_alloc=30%, 5 ETFs → alloc_per_etf = min(30/5, 15) = 6%
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    assert len(result) == 5
    for order in result:
        assert order.position_pct == pytest.approx(6.0)


def test_compute_hedge_plan_caps_each_position_at_max_single():
    from system.config import Settings, RiskConfig, HedgeConfig
    cfg = Settings(
        risk=RiskConfig(enable_inverse_hedging=True),
        hedge=HedgeConfig(
            inverse_etf_universe={"SH": [], "PSQ": []},
            max_inverse_pct_by_regime={"crash": 50.0},
            max_single_position_pct=15.0,
        ),
    )
    from hedge.hedge_engine import HedgeEngine
    e = HedgeEngine(cfg)
    # 2 ETFs, crash: alloc_per_etf = min(50/2, 15) = min(25, 15) = 15%
    result = e.compute_hedge_plan(_regime("crash"), [], {}, 100_000)
    assert len(result) == 2
    for order in result:
        assert order.position_pct == pytest.approx(15.0)


def test_compute_hedge_plan_total_allocation_does_not_exceed_regime_cap():
    from system.config import Settings, RiskConfig, HedgeConfig
    cfg = Settings(
        risk=RiskConfig(enable_inverse_hedging=True),
        hedge=HedgeConfig(
            inverse_etf_universe={"SH": [], "PSQ": []},
            max_inverse_pct_by_regime={"bear": 25.0},
            max_single_position_pct=20.0,
        ),
    )
    from hedge.hedge_engine import HedgeEngine
    e = HedgeEngine(cfg)
    # 2 ETFs, bear: alloc_per_etf = min(25/2, 20) = 12.5; total = 25%
    result = e.compute_hedge_plan(_regime("bear"), [], {}, 100_000)
    total = sum(o.position_pct for o in result)
    assert total == pytest.approx(25.0)


def test_get_exits_needed_returns_hedge_tickers_only():
    e = _engine()
    open_positions = [
        {"ticker": "SH", "signal_source": "hedge"},
        {"ticker": "AAPL", "signal_source": "congressional"},
        {"ticker": "PSQ", "signal_source": "hedge"},
    ]
    result = e.get_exits_needed(open_positions)
    assert set(result) == {"SH", "PSQ"}


def test_get_exits_needed_returns_empty_when_no_hedges():
    e = _engine()
    open_positions = [{"ticker": "AAPL", "signal_source": "congressional"}]
    result = e.get_exits_needed(open_positions)
    assert result == []
