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


def test_process_signal_applies_correlation_multiplier(mocker, orch):
    """correlation multiplier of 0.5 should halve the opened position size."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    orch._regime_state = None  # no regime → final_pct = AI position_pct directly

    mocker.patch("orchestration.main_loop.get_committees_for_politician",
                 return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
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
                 return_value=MagicMock(info={"regularMarketPrice": 100.0}))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=0.5)

    disc = {
        "id": "d1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] == pytest.approx(2.0)  # 4.0 * 0.5


def test_process_fundamental_candidate_applies_correlation_multiplier(mocker, orch):
    """correlation multiplier of 0.5 should halve the opened position size."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    orch._regime_state = None  # no regime → final_pct = AI position_pct directly

    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=4.0,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=MagicMock(info={"regularMarketPrice": 100.0}))
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
    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] == pytest.approx(2.0)  # 4.0 * 0.5
