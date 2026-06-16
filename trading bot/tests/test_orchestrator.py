"""Tests for RegimeAwareOrchestrator — invested-pct capacity gate."""
from unittest.mock import MagicMock
import pytest
from orchestration.main_loop import RegimeAwareOrchestrator


def _mock_broker(cash: float, position_value: float) -> MagicMock:
    broker = MagicMock()
    broker.get_cash.return_value = cash
    broker.get_equity.return_value = cash + position_value
    if position_value > 0:
        broker.get_positions.return_value = [
            {"ticker": "SPY", "qty": 1, "current_price": position_value}
        ]
    else:
        broker.get_positions.return_value = []
    return broker


@pytest.fixture
def orch(mocker):
    mocker.patch("orchestration.main_loop._NYSE.is_session", return_value=True)
    mocker.patch("orchestration.main_loop.get_regime_data", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.run_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])

    from system.config import settings
    o = RegimeAwareOrchestrator(settings)
    o._portfolio = MagicMock()
    o._risk = MagicMock()
    o._store = MagicMock()
    o._market_data = MagicMock()
    o._regime_state = None
    o._engine = MagicMock()
    o._engine.is_fitted = False
    return o


def test_pipeline_skips_entries_when_at_capacity(mocker, orch):
    orch._broker = _mock_broker(cash=15_000, position_value=85_000)  # 85% invested
    process_spy = mocker.patch.object(orch, "_process_signal")
    fundamental_spy = mocker.patch.object(orch, "_process_fundamental_candidate")

    orch.run_morning_pipeline()

    process_spy.assert_not_called()
    fundamental_spy.assert_not_called()
    assert orch._portfolio.enforce_stop_losses.call_count >= 1


def test_pipeline_enforces_stop_losses_even_at_capacity(mocker, orch):
    orch._broker = _mock_broker(cash=5_000, position_value=95_000)  # 95% invested
    mocker.patch.object(orch, "_process_signal")

    orch.run_morning_pipeline()

    assert orch._portfolio.enforce_stop_losses.call_count >= 1
    assert orch._portfolio.enforce_take_profits.call_count >= 1


from datetime import date, timedelta


@pytest.fixture
def orch_fitted(mocker):
    """Orchestrator with a fitted engine for refit scheduling tests."""
    mocker.patch("orchestration.main_loop._NYSE.is_session", return_value=True)
    mocker.patch("orchestration.main_loop.get_regime_data", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.run_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])

    from system.config import settings
    o = RegimeAwareOrchestrator(settings)
    o._portfolio = MagicMock()
    o._risk = MagicMock()
    o._store = MagicMock()
    o._market_data = MagicMock()
    o._regime_state = None
    o._engine = MagicMock()
    o._engine.is_fitted = True
    o._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch.object(o, "_update_regime")    # prevent DB writes
    mocker.patch.object(o, "_update_dashboard") # prevent file writes
    return o


def test_rolling_refit_triggered_when_interval_exceeded(orch_fitted):
    orch_fitted._last_refit_date = date.today() - timedelta(days=31)
    orch_fitted.run_morning_pipeline()
    orch_fitted._engine.rolling_refit.assert_called_once()


def test_rolling_refit_not_triggered_when_recent(orch_fitted):
    orch_fitted._last_refit_date = date.today() - timedelta(days=5)
    orch_fitted.run_morning_pipeline()
    orch_fitted._engine.rolling_refit.assert_not_called()


from regime.hmm_engine import RegimeState as _RegimeState


def _bear_regime() -> _RegimeState:
    return _RegimeState(
        date="2026-05-08", regime_index=1, regime_label="bear",
        confidence=0.85, is_stable=True, n_regimes=5,
        raw_posteriors=[0.05, 0.85, 0.05, 0.03, 0.02],
    )


def _neutral_regime() -> _RegimeState:
    return _RegimeState(
        date="2026-05-08", regime_index=2, regime_label="neutral",
        confidence=0.85, is_stable=True, n_regimes=5,
        raw_posteriors=[0.05, 0.05, 0.85, 0.03, 0.02],
    )


def test_hedge_pass_called_when_regime_is_bear(mocker, orch_fitted):
    orch_fitted._regime_state = _bear_regime()
    hedge_pass_spy = mocker.patch.object(orch_fitted, "_run_hedge_pass")
    mocker.patch.object(orch_fitted, "_run_hedge_exits")
    orch_fitted.run_morning_pipeline()
    hedge_pass_spy.assert_called_once()


def test_hedge_pass_not_called_when_regime_is_neutral(mocker, orch_fitted):
    orch_fitted._regime_state = _neutral_regime()
    hedge_pass_spy = mocker.patch.object(orch_fitted, "_run_hedge_pass")
    mocker.patch.object(orch_fitted, "_run_hedge_exits")
    orch_fitted.run_morning_pipeline()
    hedge_pass_spy.assert_not_called()


def test_hedge_exits_called_when_regime_is_not_hedge(mocker, orch_fitted):
    orch_fitted._regime_state = _neutral_regime()
    exits_spy = mocker.patch.object(orch_fitted, "_run_hedge_exits")
    mocker.patch.object(orch_fitted, "_run_hedge_pass")
    orch_fitted.run_morning_pipeline()
    exits_spy.assert_called_once()


def test_hedge_exits_not_called_when_regime_is_bear(mocker, orch_fitted):
    orch_fitted._regime_state = _bear_regime()
    exits_spy = mocker.patch.object(orch_fitted, "_run_hedge_exits")
    mocker.patch.object(orch_fitted, "_run_hedge_pass")
    orch_fitted.run_morning_pipeline()
    exits_spy.assert_not_called()


def test_run_exit_review_pre_fetches_research_in_batch(mocker, orch):
    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[
        {
            "ticker": "AAPL", "entry_price": 100.0, "entry_date": "2026-04-01",
            "shares": 10.0, "signal_id": 1, "signal_source": "congressional",
        },
        {
            "ticker": "MSFT", "entry_price": 200.0, "entry_date": "2026-04-01",
            "shares": 5.0, "signal_id": 2, "signal_source": "congressional",
        },
    ])
    batch_spy = mocker.patch(
        "orchestration.main_loop.gather_research_batch",
        return_value={"AAPL": None, "MSFT": None},
    )
    mocker.patch(
        "orchestration.main_loop.yf.Ticker",
        return_value=MagicMock(info={"regularMarketPrice": 110.0}),
    )
    mocker.patch(
        "orchestration.main_loop.review_exit",
        return_value=MagicMock(action="hold", rationale="hold"),
    )
    orch.run_exit_review()
    batch_spy.assert_called_once()
    tickers_fetched = set(batch_spy.call_args[0][0])
    assert tickers_fetched == {"AAPL", "MSFT"}


def _make_yf_ticker_mock(price: float = 100.0, history_rows: int = 20):
    """Build a yf.Ticker mock with fast_info.last_price and history() support.

    The history DataFrame stub returns a constant ATR of ~2% of price so that
    vol_target_size_pct produces a predictable, finite base size.
    """
    import pandas as pd
    import numpy as np

    # Construct a minimal OHLC history with ~2% daily ATR
    n = history_rows
    closes = np.full(n, price)
    hist_df = pd.DataFrame({
        "High": closes * 1.01,
        "Low": closes * 0.99,
        "Close": closes,
    })

    ticker_mock = MagicMock()
    ticker_mock.fast_info.last_price = price
    ticker_mock.history.return_value = hist_df
    return ticker_mock


def test_process_signal_applies_correlation_multiplier(mocker, orch):
    """Correlation multiplier halves the opened position size.

    After Task 1.1 the base size comes from vol targeting, NOT from score.position_pct.
    We verify:
      (a) size does NOT equal score.position_pct × NAV
      (b) size does not exceed max_position_pct × NAV
      (c) correlation multiplier is still applied (result < uncapped deterministic size)
    """
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None  # no regime → final_pct = deterministic base directly

    mocker.patch("orchestration.main_loop.get_committees_for_politician",
                 return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    llm_position_pct = 4.0
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=llm_position_pct,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    corr_mult = 0.5
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=corr_mult)

    disc = {
        "id": "d1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    from system.config import settings
    call_kwargs = orch._portfolio.open_position.call_args[1]
    final_pct = call_kwargs["position_pct"]
    position_size_usd = final_pct / 100 * nav

    # (a) size does NOT equal the LLM's number × NAV
    llm_size_usd = llm_position_pct / 100 * nav
    assert position_size_usd != pytest.approx(llm_size_usd), (
        "position_size_usd must NOT equal LLM position_pct × NAV after Task 1.1"
    )

    # (b) size does not exceed max_position_pct × NAV
    max_size_usd = settings.risk.max_position_pct / 100 * nav
    assert position_size_usd <= max_size_usd + 1e-9, (
        f"position_size_usd {position_size_usd:.2f} exceeds max {max_size_usd:.2f}"
    )

    # (c) position was actually opened
    orch._portfolio.open_position.assert_called_once()


def test_process_signal_sizes_on_nav_not_cash(mocker, orch):
    """Sizing uses NAV (cash + positions), not just cash alone.

    With NAV=$100k and the deterministic vol-target formula, the resulting
    position size must be ≤ max_position_pct of NAV and must not equal the
    LLM's position_pct × NAV.
    """
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician",
                 return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    llm_position_pct = 5.0
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=llm_position_pct,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "d2", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    from system.config import settings
    call_kwargs = orch._portfolio.open_position.call_args[1]
    final_pct = call_kwargs["position_pct"]
    position_size_usd = final_pct / 100 * nav

    # Size does NOT equal the LLM's number
    llm_size_usd = llm_position_pct / 100 * nav
    assert position_size_usd != pytest.approx(llm_size_usd), (
        "size must come from vol-targeting, not from LLM position_pct"
    )

    # Size never exceeds max_position_pct × NAV
    max_size_usd = settings.risk.max_position_pct / 100 * nav
    assert position_size_usd <= max_size_usd + 1e-9


def test_process_fundamental_candidate_applies_correlation_multiplier(mocker, orch):
    """Correlation multiplier halves the opened position size.

    After Task 1.1 the base size comes from vol targeting, NOT from score.position_pct.
    We verify:
      (a) size does NOT equal score.position_pct × NAV
      (b) size does not exceed max_position_pct × NAV
      (c) position was opened (returns True)
    """
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None  # no regime → final_pct = deterministic base directly

    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    llm_position_pct = 4.0
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=llm_position_pct,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=0.5)

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    result = orch._process_fundamental_candidate(candidate, {}, set())

    assert result is True

    from system.config import settings
    call_kwargs = orch._portfolio.open_position.call_args[1]
    final_pct = call_kwargs["position_pct"]
    position_size_usd = final_pct / 100 * nav

    # (a) size does NOT equal the LLM's number × NAV
    llm_size_usd = llm_position_pct / 100 * nav
    assert position_size_usd != pytest.approx(llm_size_usd), (
        "position_size_usd must NOT equal LLM position_pct × NAV after Task 1.1"
    )

    # (b) size does not exceed max_position_pct × NAV
    max_size_usd = settings.risk.max_position_pct / 100 * nav
    assert position_size_usd <= max_size_usd + 1e-9


# ------------------------------------------------------------------
# Portfolio vol gate tests (Task 2.1)
# ------------------------------------------------------------------

def test_port_vol_mult_below_one_when_vol_exceeds_target(mocker, orch):
    """When realized portfolio vol > target, _port_vol_mult must be < 1.0."""
    import numpy as np

    orch._broker = _mock_broker(cash=100_000, position_value=0)

    # Construct a NAV history with ~30% annualised vol (default target is 15%)
    np.random.seed(42)
    daily_rets = np.random.normal(0, 0.30 / np.sqrt(252), 30)
    nav_series = [100_000.0]
    for r in daily_rets:
        nav_series.append(nav_series[-1] * (1 + r))

    mocker.patch("orchestration.main_loop.get_nav_history", return_value=nav_series)

    orch.run_morning_pipeline()

    assert orch._port_vol_mult < 1.0, (
        f"_port_vol_mult should be < 1.0 when realized vol > target, got {orch._port_vol_mult}"
    )


def test_port_vol_mult_stays_one_when_vol_below_target(mocker, orch):
    """When realized portfolio vol ≤ target, _port_vol_mult must remain 1.0."""
    import numpy as np

    orch._broker = _mock_broker(cash=100_000, position_value=0)

    # NAV history with ~5% annualised vol (target is 15%)
    np.random.seed(0)
    daily_rets = np.random.normal(0, 0.05 / np.sqrt(252), 30)
    nav_series = [100_000.0]
    for r in daily_rets:
        nav_series.append(nav_series[-1] * (1 + r))

    mocker.patch("orchestration.main_loop.get_nav_history", return_value=nav_series)

    orch.run_morning_pipeline()

    assert orch._port_vol_mult == pytest.approx(1.0)


def test_port_vol_mult_stays_one_when_history_too_short(mocker, orch):
    """With fewer than 20 NAV data points, _port_vol_mult must remain 1.0."""
    orch._broker = _mock_broker(cash=100_000, position_value=0)

    mocker.patch("orchestration.main_loop.get_nav_history", return_value=[100_000.0] * 5)

    orch.run_morning_pipeline()

    assert orch._port_vol_mult == pytest.approx(1.0)


def test_process_signal_uses_port_vol_mult(mocker, orch):
    """_process_signal must apply _port_vol_mult to final_pct before validate_order."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=[])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=4.0,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "d-voltest-1", "politician": "J", "ticker": "NVDA",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }

    # Run with vol mult = 1.0 for baseline
    orch._port_vol_mult = 1.0
    orch._process_signal(disc, {})
    pct_full = orch._portfolio.open_position.call_args[1]["position_pct"]

    orch._portfolio.open_position.reset_mock()

    # Run with vol mult = 0.5
    orch._port_vol_mult = 0.5
    orch._process_signal(disc, {})
    pct_halved = orch._portfolio.open_position.call_args[1]["position_pct"]

    assert pct_halved == pytest.approx(pct_full * 0.5, rel=1e-3)


def test_hedge_pass_calls_validate_order(mocker, orch):
    """_run_hedge_pass must validate each hedge order through the risk manager."""
    from risk.risk_manager import RiskVeto
    from hedge.hedge_engine import HedgeOrder

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])
    mocker.patch.object(
        orch._hedge_engine, "compute_hedge_plan",
        return_value=[HedgeOrder(ticker="SH", position_pct=10.0, rationale="bear regime")],
    )
    mocker.patch("orchestration.main_loop.yf.Ticker",
                  return_value=_make_yf_ticker_mock(price=20.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )

    orch._run_hedge_pass()

    orch._risk.validate_order.assert_called_once_with(
        ticker="SH",
        position_pct=10.0,
        sector="Hedge",
        sector_allocation={},
        position_size_usd=10_000.0,
        adv_usd=None,
    )
    orch._portfolio.open_position.assert_called_once()


def test_hedge_pass_applies_size_multiplier(mocker, orch):
    """veto.size_multiplier must scale the hedge position pct."""
    from risk.risk_manager import RiskVeto
    from hedge.hedge_engine import HedgeOrder

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])
    mocker.patch.object(
        orch._hedge_engine, "compute_hedge_plan",
        return_value=[HedgeOrder(ticker="SH", position_pct=10.0, rationale="bear regime")],
    )
    mocker.patch("orchestration.main_loop.yf.Ticker",
                  return_value=_make_yf_ticker_mock(price=20.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=0.5,
    )

    orch._run_hedge_pass()

    call_kwargs = orch._portfolio.open_position.call_args
    assert call_kwargs.kwargs["position_pct"] == pytest.approx(5.0)


def test_hedge_pass_skips_order_vetoed_by_risk_manager(mocker, orch):
    from risk.risk_manager import RiskVeto
    from hedge.hedge_engine import HedgeOrder

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])
    mocker.patch.object(
        orch._hedge_engine, "compute_hedge_plan",
        return_value=[HedgeOrder(ticker="SH", position_pct=10.0, rationale="bear regime")],
    )
    mocker.patch("orchestration.main_loop.yf.Ticker",
                  return_value=_make_yf_ticker_mock(price=20.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=False, reason="STALE_DATA: market data too old — new entries blocked",
    )

    orch._run_hedge_pass()

    orch._portfolio.open_position.assert_not_called()


def test_close_all_positions_excludes_hedge_by_default_param(mocker, orch):
    """_close_all_positions(source_exclude='hedge') must skip hedge positions."""
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[
        {"ticker": "AAPL", "shares": 10, "entry_price": 100.0, "entry_date": "2026-01-01",
         "signal_id": 1, "signal_source": "fundamental"},
        {"ticker": "SH", "shares": 5, "entry_price": 20.0, "entry_date": "2026-01-01",
         "signal_id": None, "signal_source": "hedge"},
    ])
    mocker.patch("orchestration.main_loop.yf.Ticker",
                  return_value=_make_yf_ticker_mock(price=50.0))

    orch._close_all_positions(reason="intraday_deleverage", source_exclude="hedge")

    assert orch._portfolio.close_position.call_count == 1
    call_kwargs = orch._portfolio.close_position.call_args
    assert call_kwargs[0][0] == "AAPL"


def test_intraday_check_deleverage_excludes_hedges(mocker, orch):
    from risk.risk_manager import RiskState
    orch._risk = mocker.MagicMock()
    orch._risk.state = RiskState.DELEVERAGE
    orch._broker = _mock_broker(cash=50_000, position_value=50_000)
    close_all_spy = mocker.patch.object(orch, "_close_all_positions")

    orch.run_intraday_check()

    close_all_spy.assert_called_once_with(reason="intraday_deleverage", source_exclude="hedge")
