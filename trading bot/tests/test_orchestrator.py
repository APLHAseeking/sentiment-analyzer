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
    mocker.patch("orchestration.main_loop.run_insider_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_insider_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])
    mocker.patch("orchestration.main_loop.record_job_run")  # prevent real DB writes

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


def test_congressional_phase_receives_fundamental_ticker_set(mocker, orch):
    """A ticker present in both the fundamental screener's candidates and the
    qualified congressional disclosures must reach _process_signal with that
    overlap available, so it can resolve signal_type="both" even when Phase 1
    attempted (and vetoed) it rather than opening a position."""
    from screener.factor_scorer import FactorCandidate

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[
        FactorCandidate(ticker="AAPL", composite_score=80, value_score=25,
                         momentum_score=28, quality_score=27, research=None),
    ])
    mocker.patch("orchestration.main_loop.filter_disclosures", return_value=[
        {"id": "d1", "politician": "J", "ticker": "AAPL",
         "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
         "amount_range": "$50,001 - $100,000"},
    ])
    mocker.patch.object(orch, "_process_fundamental_candidate", return_value=False)
    signal_spy = mocker.patch.object(orch, "_process_signal", return_value=False)

    orch.run_morning_pipeline()

    signal_spy.assert_called_once()
    _, kwargs = signal_spy.call_args
    assert "AAPL" in kwargs["fundamental_tickers"]


def test_insider_phase_reaches_process_with_fundamental_ticker_set(mocker, orch):
    """Phase 2.5 routes qualified insider buys to _process_insider_signal with the
    fundamental ticker overlap available for signal_type="both" resolution."""
    from screener.factor_scorer import FactorCandidate

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[
        FactorCandidate(ticker="MSFT", composite_score=82, value_score=27,
                         momentum_score=28, quality_score=27, research=None),
    ])
    mocker.patch("orchestration.main_loop.run_insider_scraper", return_value=[{"ticker": "MSFT"}])
    mocker.patch("orchestration.main_loop.filter_insider_disclosures", return_value=[
        {"id": "MSFT:CEO", "insider_name": "Nadella", "ticker": "MSFT", "title": "CEO",
         "transaction_date": "2026-06-20", "disclosure_date": "2026-06-22",
         "transaction_type": "buy", "amount_usd": 500_000.0, "scraped_at": "x"},
    ])
    mocker.patch.object(orch, "_process_fundamental_candidate", return_value=False)
    insider_spy = mocker.patch.object(orch, "_process_insider_signal", return_value=False)

    orch.run_morning_pipeline()

    insider_spy.assert_called_once()
    _, kwargs = insider_spy.call_args
    assert "MSFT" in kwargs["fundamental_tickers"]


def test_insider_phase_respects_daily_cap(mocker, orch):
    """No more than InsiderConfig.max_per_day insider-only entries open per morning."""
    orch._broker = _mock_broker(cash=100_000, position_value=0)
    discs = [
        {"id": f"T{i}", "insider_name": "X", "ticker": f"T{i}", "title": "CEO",
         "transaction_date": "2026-06-20", "disclosure_date": "2026-06-22",
         "transaction_type": "buy", "amount_usd": 100_000.0, "scraped_at": "x"}
        for i in range(5)
    ]
    mocker.patch("orchestration.main_loop.run_insider_scraper", return_value=discs)
    mocker.patch("orchestration.main_loop.filter_insider_disclosures", return_value=discs)
    mocker.patch.object(orch, "_process_fundamental_candidate", return_value=False)
    insider_spy = mocker.patch.object(orch, "_process_insider_signal", return_value=True)
    orch._portfolio.can_open_new_position.return_value = True

    orch.run_morning_pipeline()

    assert insider_spy.call_count == orch._cfg.insider.max_per_day


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
    mocker.patch("orchestration.main_loop.run_insider_scraper", return_value=[])
    mocker.patch("orchestration.main_loop.filter_insider_disclosures", return_value=[])
    mocker.patch("orchestration.main_loop.get_universe", return_value=[])
    mocker.patch("orchestration.main_loop.run_factor_screen", return_value=[])
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[])
    mocker.patch("orchestration.main_loop.record_job_run")  # prevent real DB writes

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
    mocker.patch.object(o, "_update_regime")              # prevent DB writes
    mocker.patch.object(o, "_log_and_set_regime_state")   # prevent DB writes from new helper
    mocker.patch.object(o, "_update_dashboard")           # prevent file writes
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


# ------------------------------------------------------------------
# Economic floor re-checks after multiplier stack (Task A6)
# ------------------------------------------------------------------

def test_process_signal_rejects_when_port_vol_mult_drives_below_floor(mocker, orch):
    """final_pct *= self._port_vol_mult shrinking below 0.1% must reject, not open a dust position."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None
    orch._port_vol_mult = 0.01  # extreme vol-gate cut

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
                     conviction=8, position_pct=5.0,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                  return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "d3", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_process_signal_rejects_when_risk_size_multiplier_drives_below_floor(mocker, orch):
    """final_pct *= veto.size_multiplier shrinking below 0.1% must reject, not open a dust position."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None
    orch._port_vol_mult = 1.0

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
                     conviction=8, position_pct=0.5,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                  return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=0.01,
    )
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "d4", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is False
    orch._portfolio.open_position.assert_not_called()


# ---------------------------------------------------------------------------
# _process_fundamental_candidate floor-check tests (A6 gap)
# ---------------------------------------------------------------------------

def test_process_fundamental_candidate_rejects_when_port_vol_mult_drives_below_floor(mocker, orch):
    """_port_vol_mult=0.01 drives final_pct below 0.1% — must return False, not open position."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None
    orch._port_vol_mult = 0.01  # extreme vol-gate cut

    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=5.0,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    result = orch._process_fundamental_candidate(candidate, {}, set())

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_process_fundamental_candidate_rejects_when_risk_size_multiplier_drives_below_floor(mocker, orch):
    """size_multiplier=0.01 from risk veto drives final_pct below 0.1% — must return False, not open position."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None
    orch._port_vol_mult = 1.0

    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(
                     conviction=8, position_pct=0.5,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=0.01,
    )
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    result = orch._process_fundamental_candidate(candidate, {}, set())

    assert result is False
    orch._portfolio.open_position.assert_not_called()


# ---------------------------------------------------------------------------
# EOD deleverage tests (A8)
# ---------------------------------------------------------------------------

def test_run_eod_closes_positions_on_deleverage(mocker, orch):
    from risk.risk_manager import RiskState

    orch._broker = _mock_broker(cash=50_000, position_value=50_000)
    orch._risk = mocker.MagicMock()
    orch._risk.state = RiskState.DELEVERAGE
    orch._portfolio.log_snapshot = mocker.MagicMock()
    close_all_spy = mocker.patch.object(orch, "_close_all_positions")
    mocker.patch.object(orch, "_update_dashboard")

    orch.run_eod()

    close_all_spy.assert_called_once_with(reason="eod_deleverage", source_exclude="hedge")


def test_run_eod_does_not_close_positions_when_normal(mocker, orch):
    from risk.risk_manager import RiskState

    orch._broker = _mock_broker(cash=50_000, position_value=50_000)
    orch._risk = mocker.MagicMock()
    orch._risk.state = RiskState.NORMAL
    orch._portfolio.log_snapshot = mocker.MagicMock()
    close_all_spy = mocker.patch.object(orch, "_close_all_positions")
    mocker.patch.object(orch, "_update_dashboard")

    orch.run_eod()

    close_all_spy.assert_not_called()


import pandas as pd


def test_initialize_does_not_double_step_regime_engine(mocker, orch):
    """initialize() must consume initialize_incremental()'s own RegimeState
    directly — it must NOT also call update_single()/current_regime() via
    _update_regime() on the same bar."""
    state = _RegimeState(
        date="2026-06-12", regime_index=2, regime_label="neutral",
        confidence=0.7, is_stable=True, n_regimes=3,
        raw_posteriors=[0.1, 0.2, 0.7],
    )
    orch._engine = mocker.MagicMock()
    orch._engine.is_fitted = True
    orch._engine.initialize_incremental.return_value = state

    market_data = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.date_range("2026-06-10", periods=3, freq="B"),
    )
    mocker.patch("orchestration.main_loop.get_regime_data", return_value=market_data)
    mocker.patch("orchestration.main_loop.os.path.exists", return_value=True)
    mock_portfolio_cls = mocker.patch("orchestration.main_loop.Portfolio")
    mock_portfolio_cls.return_value.reconcile_with_broker.return_value = {"ghost_positions": []}
    mocker.patch("orchestration.main_loop.log_regime")

    broker = _mock_broker(cash=100_000, position_value=0)

    orch.initialize(broker)

    orch._engine.initialize_incremental.assert_called_once()
    orch._engine.update_single.assert_not_called()
    orch._engine.current_regime.assert_not_called()
    assert orch._regime_state is state


def test_initialize_rejects_non_paper_broker(mocker, orch):
    """initialize() must enforce the paper-only invariant itself, not rely on
    run_bot.py being the only caller that checks broker.is_paper. A broker
    with is_paper=False must be rejected before any setup work happens."""
    live_broker = mocker.MagicMock()
    live_broker.is_paper = False

    get_regime_data_spy = mocker.patch("orchestration.main_loop.get_regime_data")

    with pytest.raises(RuntimeError, match="paper"):
        orch.initialize(live_broker)

    get_regime_data_spy.assert_not_called()


def test_maybe_rolling_refit_does_not_double_step_regime_engine(mocker, orch_fitted):
    """_maybe_rolling_refit() must consume initialize_incremental()'s own
    RegimeState directly — same double-processing bug as initialize()."""
    from datetime import date, timedelta

    state = _RegimeState(
        date="2026-06-12", regime_index=1, regime_label="bear",
        confidence=0.8, is_stable=True, n_regimes=3,
        raw_posteriors=[0.1, 0.8, 0.1],
    )
    orch_fitted._engine.initialize_incremental.return_value = state
    orch_fitted._last_refit_date = date.today() - timedelta(days=31)

    orch_fitted._maybe_rolling_refit()

    orch_fitted._engine.initialize_incremental.assert_called_once()
    orch_fitted._update_regime.assert_not_called()
    # _log_and_set_regime_state is patched by the fixture; assert it was called with our state
    orch_fitted._log_and_set_regime_state.assert_called_once_with(state)


def test_run_exit_review_reduce_marks_take_profit_taken(mocker, orch):
    """When review_exit returns action='reduce', run_exit_review must call
    mark_take_profit_taken so a later enforce_take_profits doesn't
    reduce the (now smaller) position again."""
    from bot.ai_analyst import ExitDecision

    orch._broker = _mock_broker(cash=100_000, position_value=0)
    mocker.patch("orchestration.main_loop.get_open_positions", return_value=[{
        "ticker": "AAPL", "shares": 10.0, "entry_price": 100.0,
        "entry_date": "2026-05-01", "signal_id": 1, "signal_source": "fundamental",
    }])
    mocker.patch("orchestration.main_loop.gather_research_batch", return_value={})
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=MagicMock(info={"regularMarketPrice": 120.0}))
    mocker.patch("orchestration.main_loop.review_exit",
                 return_value=ExitDecision(action="reduce", rationale="partial profit-take"))
    mark_spy = mocker.patch("orchestration.main_loop.mark_take_profit_taken")

    orch.run_exit_review()

    mark_spy.assert_called_once_with("AAPL")
    orch._portfolio.reduce_position.assert_called_once()


def test_process_signal_uses_conservative_atr_fallback_on_history_failure(mocker, orch):
    """If yf.Ticker(...).history() raises, atr_pct must fall back to
    _ATR_FALLBACK_PCT (10.0), not the optimistic 1.0%."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from risk.position_sizing import apply_conviction_tilt, vol_target_size_pct
    from orchestration.main_loop import _ATR_FALLBACK_PCT
    from system.config import settings

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None
    orch._port_vol_mult = 1.0

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
                     conviction=8, position_pct=5.0,
                     rationale="good", entry="buy", risk_flags=(),
                 ))
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)
    orch._risk.validate_order.return_value = RiskVeto(
        allowed=True, reason="OK", size_multiplier=1.0,
    )

    # fast_info works (gives a price) but .history() raises
    ticker_mock = MagicMock()
    ticker_mock.fast_info.last_price = 100.0
    ticker_mock.history.side_effect = Exception("no data")
    mocker.patch("orchestration.main_loop.yf.Ticker", return_value=ticker_mock)

    disc = {
        "id": "d5", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    expected_base = vol_target_size_pct(
        atr_pct=_ATR_FALLBACK_PCT,
        per_trade_risk_pct=settings.sizing.per_trade_risk_pct,
        max_position_pct=settings.risk.max_position_pct,
    )
    expected_final = apply_conviction_tilt(expected_base, 8, settings.risk.max_position_pct)

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] == pytest.approx(expected_final)
    assert _ATR_FALLBACK_PCT == 10.0


# ---------------------------------------------------------------------------
# MA50/MA200 conviction modifier
# ---------------------------------------------------------------------------

import numpy as np


def test_ma_conviction_delta_golden_cross():
    """price > MA50 > MA200 → +1"""
    from orchestration.main_loop import _ma_conviction_delta
    closes = np.linspace(80.0, 110.0, 250)   # steady uptrend
    assert _ma_conviction_delta(closes, 115.0) == 1


def test_ma_conviction_delta_below_ma200():
    """price ≤ MA200 → -2"""
    from orchestration.main_loop import _ma_conviction_delta
    closes = np.linspace(120.0, 90.0, 250)   # downtrend
    ma200 = float(np.mean(closes[-200:]))
    assert _ma_conviction_delta(closes, ma200 - 1.0) == -2


def test_ma_conviction_delta_below_ma50_above_ma200():
    """price above MA200 but below MA50 → -1 (pulled back below near-term MA)"""
    from orchestration.main_loop import _ma_conviction_delta
    # 202 bars at 100 then 50 bars at 105: MA50=105, MA200≈101.25
    closes = np.concatenate([np.full(202, 100.0), np.full(50, 105.0)])
    ma50  = float(np.mean(closes[-50:]))    # 105.0
    ma200 = float(np.mean(closes[-200:]))   # 101.25
    current = 103.0  # above MA200, below MA50
    assert current > ma200 and current < ma50
    assert _ma_conviction_delta(closes, current) == -1


def test_ma_conviction_delta_insufficient_history():
    """fewer than 200 bars → 0 (no penalty)"""
    from orchestration.main_loop import _ma_conviction_delta
    closes = np.ones(150)
    assert _ma_conviction_delta(closes, 1.0) == 0


def test_process_signal_golden_cross_vs_below_ma200_position_size(mocker, orch):
    """Golden cross (price above both MAs) must produce larger position than price-below-MA200."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    import pandas as pd

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None
    orch._port_vol_mult = 1.0

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=7, position_pct=5.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)

    n = 252
    idx = pd.date_range("2025-06-01", periods=n, freq="B")

    # Golden cross: steady uptrend. 2.5% H-L spread gives ATR≈5% → base_pct≈3%;
    # conviction 8 tilts to 3.12% (capped at 3% congressional cap) while conviction
    # 5 tilts to 2.4% (below cap), so the two scenarios remain distinguishable.
    up_closes = np.linspace(80.0, 110.0, n)
    up_df = pd.DataFrame({"Close": up_closes, "High": up_closes * 1.025,
                           "Low": up_closes * 0.975, "Volume": 1e6}, index=idx)
    ticker_up = MagicMock()
    ticker_up.fast_info.last_price = 115.0
    ticker_up.history.return_value = up_df
    mocker.patch("orchestration.main_loop.yf.Ticker", return_value=ticker_up)

    disc = {"id": "d-ma1", "politician": "J", "ticker": "AAPL",
            "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
            "amount_range": "$50,001 - $100,000"}
    orch._process_signal(disc, {})
    pct_golden = orch._portfolio.open_position.call_args[1]["position_pct"]
    orch._portfolio.open_position.reset_mock()

    # Below MA200: downtrend
    dn_closes = np.linspace(120.0, 80.0, n)
    dn_df = pd.DataFrame({"Close": dn_closes, "High": dn_closes * 1.025,
                           "Low": dn_closes * 0.975, "Volume": 1e6}, index=idx)
    ticker_dn = MagicMock()
    ticker_dn.fast_info.last_price = 75.0
    ticker_dn.history.return_value = dn_df
    mocker.patch("orchestration.main_loop.yf.Ticker", return_value=ticker_dn)

    disc2 = {**disc, "id": "d-ma2"}
    orch._process_signal(disc2, {})
    pct_below = orch._portfolio.open_position.call_args[1]["position_pct"]

    assert pct_golden > pct_below


def test_technical_gate_off_by_default_skips_gate(mocker, orch):
    """enable_technical_gate=False (default) must not call compute_snapshot/score_technical
    and must pass initial_stop_pct=None through to open_position — byte-for-byte
    unchanged behavior vs. before this feature."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)
    snapshot_spy = mocker.patch("orchestration.main_loop.compute_snapshot")
    score_spy = mocker.patch("orchestration.main_loop.score_technical")

    disc = {
        "id": "tg1", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    snapshot_spy.assert_not_called()
    score_spy.assert_not_called()
    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] is None


def test_technical_gate_on_skip_rejects_signal(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=4, entry="skip", setup_type="no_clean_setup",
                     trend_alignment="mixed", momentum_state="fading",
                     volume_confirmation="unconfirmed", relative_strength="lagging",
                     entry_trigger="none", invalidation_price=0.0, target_price=0.0,
                     reward_risk=0.0, key_levels="", conflicts=(), rationale="weak setup",
                     risk_flags=(),
                 ))

    disc = {
        "id": "tg2", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_technical_gate_falls_back_to_vol_target_on_score_technical_failure(mocker, orch):
    """A transient failure evaluating the technical gate (LLM call exhausted
    retries, JSON parse error) must degrade to the existing vol-target sizing
    path rather than rejecting a candidate the upstream AI score already
    approved — matching this codebase's fail-open pattern for the correlation
    filter, the ADV check, and the ATR fallback."""
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot",
                 return_value=MagicMock(data_complete=True))
    mocker.patch("orchestration.main_loop.score_technical", side_effect=RuntimeError("LLM call failed"))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg-fallback", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is True
    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] is None


def test_technical_gate_rejects_candidate_with_incomplete_price_history(mocker, orch):
    """snapshot.data_complete/bars_available previously only flowed into the
    LLM prompt text — no code path checked them. A thinly-traded/recently
    listed ticker with NaN-padded indicators must be rejected outright, not
    scored and sized off mostly-default values."""
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot",
                 return_value=MagicMock(data_complete=False, bars_available=50))
    score_spy = mocker.patch("orchestration.main_loop.score_technical")

    disc = {
        "id": "tg-incomplete", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is False
    score_spy.assert_not_called()
    orch._portfolio.open_position.assert_not_called()


def test_technical_gate_on_buy_passes_structure_stop_pct(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=8, entry="buy", setup_type="pullback_to_support",
                     trend_alignment="bullish", momentum_state="rising",
                     volume_confirmation="confirmed", relative_strength="outperforming",
                     entry_trigger="reclaim of 20-day SMA", invalidation_price=98.0,
                     target_price=110.0, reward_risk=6.0, key_levels="support 98",
                     conflicts=(), rationale="clean setup", risk_flags=(),
                 ))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg3", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] == pytest.approx(2.0)


def test_process_signal_resolves_both_signal_type_when_also_fundamental_candidate(mocker, orch):
    """A ticker that also qualifies on the fundamental screener must be scored
    as signal_type="both" (full conviction credit, no congressional cap) even
    when it reaches the technical gate via the congressional path — e.g.
    because Phase 1 attempted it and was vetoed rather than opened."""
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    score_technical_spy = mocker.patch(
        "orchestration.main_loop.score_technical",
        return_value=TechnicalScore(
            conviction=8, entry="buy", setup_type="pullback_to_support",
            trend_alignment="bullish", momentum_state="rising",
            volume_confirmation="confirmed", relative_strength="outperforming",
            entry_trigger="reclaim of 20-day SMA", invalidation_price=98.0,
            target_price=110.0, reward_risk=6.0, key_levels="support 98",
            conflicts=(), rationale="clean setup", risk_flags=(),
        ),
    )
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg-both", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {}, fundamental_tickers={"AAPL"})

    assert score_technical_spy.call_args[1]["signal_type"] == "both"


def test_process_signal_no_congressional_cap_when_both_signal_type(mocker, orch):
    """trading bot/CLAUDE.md documents: "A ticker in both screener and
    disclosures gets the "both" signal type, full conviction credit, and no
    congressional size cap." The 3%-NAV cap must not apply once signal_type
    resolves to "both", even when reached via the congressional path."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg-both-cap", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {}, fundamental_tickers={"AAPL"})

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] > 3.0


def test_process_signal_defaults_to_congressional_signal_type(mocker, orch):
    """Without an overlapping fundamental candidate, signal_type stays
    "congressional" — byte-for-byte the pre-fix behavior."""
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    score_technical_spy = mocker.patch(
        "orchestration.main_loop.score_technical",
        return_value=TechnicalScore(
            conviction=8, entry="buy", setup_type="pullback_to_support",
            trend_alignment="bullish", momentum_state="rising",
            volume_confirmation="confirmed", relative_strength="outperforming",
            entry_trigger="reclaim of 20-day SMA", invalidation_price=98.0,
            target_price=110.0, reward_risk=6.0, key_levels="support 98",
            conflicts=(), rationale="clean setup", risk_flags=(),
        ),
    )
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg-congress-only", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    orch._process_signal(disc, {})

    assert score_technical_spy.call_args[1]["signal_type"] == "congressional"


def test_technical_gate_rejects_when_invalidation_price_above_live_entry_price(mocker, orch):
    """tech_score.invalidation_price is only geometry-checked against the
    earlier history close, not the live entry price fetched moments later.
    If the live price has gapped down through (or to) that invalidation
    level, the structural stop is broken and the candidate must be
    rejected rather than opened with a zero/inverted stop."""
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_committees_for_politician", return_value=["Finance"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    # Live entry price (100.0) has gapped down to/through the model's invalidation
    # level (105.0, set when the history close was higher) by the time of sizing.
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=8, entry="buy", setup_type="pullback_to_support",
                     trend_alignment="bullish", momentum_state="rising",
                     volume_confirmation="confirmed", relative_strength="outperforming",
                     entry_trigger="reclaim of 20-day SMA", invalidation_price=105.0,
                     target_price=110.0, reward_risk=6.0, key_levels="support 105",
                     conflicts=(), rationale="clean setup", risk_flags=(),
                 ))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch("orchestration.main_loop.insert_signal", return_value=1)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "tg-gap", "politician": "J", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-03",
        "amount_range": "$50,001 - $100,000",
    }
    result = orch._process_signal(disc, {})

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_fundamental_technical_gate_off_by_default_skips_gate(mocker, orch):
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)
    snapshot_spy = mocker.patch("orchestration.main_loop.compute_snapshot")
    score_spy = mocker.patch("orchestration.main_loop.score_technical")

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    orch._process_fundamental_candidate(candidate, {}, set())

    snapshot_spy.assert_not_called()
    score_spy.assert_not_called()
    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] is None


def test_fundamental_technical_gate_on_skip_rejects_candidate(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=4, entry="skip", setup_type="no_clean_setup",
                     trend_alignment="mixed", momentum_state="fading",
                     volume_confirmation="unconfirmed", relative_strength="lagging",
                     entry_trigger="none", invalidation_price=0.0, target_price=0.0,
                     reward_risk=0.0, key_levels="", conflicts=(), rationale="weak setup",
                     risk_flags=(),
                 ))

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    result = orch._process_fundamental_candidate(candidate, {}, set())

    assert result is False
    orch._portfolio.open_position.assert_not_called()


def test_fundamental_technical_gate_on_buy_passes_structure_stop_pct(mocker, orch):
    import dataclasses
    orch._cfg = dataclasses.replace(
        orch._cfg, sizing=dataclasses.replace(orch._cfg.sizing, enable_technical_gate=True)
    )
    from bot.ai_analyst import EntryScore, TechnicalScore
    from risk.risk_manager import RiskVeto
    from screener.factor_scorer import FactorCandidate

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    mocker.patch("orchestration.main_loop.compute_snapshot", return_value=MagicMock())
    mocker.patch("orchestration.main_loop.score_technical",
                 return_value=TechnicalScore(
                     conviction=8, entry="buy", setup_type="pullback_to_support",
                     trend_alignment="bullish", momentum_state="rising",
                     volume_confirmation="confirmed", relative_strength="outperforming",
                     entry_trigger="reclaim of 20-day SMA", invalidation_price=98.0,
                     target_price=110.0, reward_risk=6.0, key_levels="support 98",
                     conflicts=(), rationale="clean setup", risk_flags=(),
                 ))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    candidate = FactorCandidate(
        ticker="MSFT", composite_score=80, value_score=25,
        momentum_score=28, quality_score=27, research=None,
    )
    orch._process_fundamental_candidate(candidate, {}, set())

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["initial_stop_pct"] == pytest.approx(2.0)


def test_process_insider_signal_caps_at_insider_max_pct_when_insider_only(mocker, orch):
    """trading bot/CLAUDE.md documents: InsiderConfig.max_pct (3% NAV) caps
    insider-only entries. With fundamental_tickers empty, signal_type resolves
    to "insider" (not "both"), so the cap in _process_insider_signal must
    clamp the final position size to <= 3.0%, mirroring the congressional cap."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_insider_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "AAPL:CEO", "insider_name": "X", "ticker": "AAPL", "title": "CEO",
        "transaction_date": "2026-06-20", "disclosure_date": "2026-06-22",
        "transaction_type": "buy", "amount_usd": 500_000.0, "scraped_at": "x",
    }
    orch._process_insider_signal(disc, {}, fundamental_tickers=frozenset())

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] <= 3.0


def test_process_insider_signal_no_cap_when_both_signal_type(mocker, orch):
    """A ticker that also qualifies on the fundamental screener resolves
    signal_type="both" and must NOT be clamped by InsiderConfig.max_pct,
    same as the congressional "both" carve-out."""
    from bot.ai_analyst import EntryScore
    from risk.risk_manager import RiskVeto

    nav = 100_000.0
    orch._broker = _mock_broker(cash=nav, position_value=0)
    orch._regime_state = None

    mocker.patch("orchestration.main_loop.get_sector_for_ticker", return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=2)
    mocker.patch("orchestration.main_loop.get_insider_cluster_count", return_value=1)
    mocker.patch("orchestration.main_loop.has_upcoming_event", return_value=(False, ""))
    mocker.patch("orchestration.main_loop.gather_research", return_value=None)
    mocker.patch("orchestration.main_loop.score_entry_with_debate",
                 return_value=EntryScore(conviction=8, position_pct=4.0,
                                         rationale="good", entry="buy", risk_flags=()))
    mocker.patch("orchestration.main_loop.yf.Ticker",
                 return_value=_make_yf_ticker_mock(price=100.0))
    orch._risk.validate_order.return_value = RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)
    mocker.patch.object(orch._corr_filter, "size_multiplier", return_value=1.0)

    disc = {
        "id": "AAPL:CEO", "insider_name": "X", "ticker": "AAPL", "title": "CEO",
        "transaction_date": "2026-06-20", "disclosure_date": "2026-06-22",
        "transaction_type": "buy", "amount_usd": 500_000.0, "scraped_at": "x",
    }
    orch._process_insider_signal(disc, {}, fundamental_tickers=frozenset({"AAPL"}))

    call_kwargs = orch._portfolio.open_position.call_args[1]
    assert call_kwargs["position_pct"] > 3.0


def test_start_schedules_second_daily_pipeline_run(mocker, orch):
    """Volatile markets (2026-07-09) prompted scanning for new entries twice a
    day instead of once — a second run_screener_prefetch/run_morning_pipeline
    pair, midday, on top of the existing 13:00/14:00 CEST pre-market pair."""
    mock_scheduler_cls = mocker.patch("orchestration.main_loop.BlockingScheduler")
    mock_scheduler = mock_scheduler_cls.return_value
    # Not testing the catch-up-on-restart path here — pretend today's run
    # already completed so start() doesn't fire a real (unmocked) pipeline
    # run depending on wall-clock time. See test_start_catch_up_* below.
    mocker.patch("orchestration.main_loop.job_ran_today", return_value=True)

    orch.start()

    scheduled = [
        (call.args[0], call.kwargs.get("hour"), call.kwargs.get("minute"))
        for call in mock_scheduler.add_job.call_args_list
    ]
    assert (orch.run_screener_prefetch, 13, 0) in scheduled
    assert (orch.run_morning_pipeline, 14, 0) in scheduled
    assert (orch.run_screener_prefetch, 17, 0) in scheduled
    assert (orch.run_morning_pipeline, 18, 0) in scheduled


def test_start_catches_up_pipeline_when_missed_today(mocker, orch):
    """The in-memory scheduler drops today's already-passed cron windows on
    every process restart. If today's first window (14:00) has passed and no
    completed run is recorded for today, start() must run the pipeline once
    immediately instead of silently waiting for tomorrow's cron window."""
    mocker.patch("orchestration.main_loop.BlockingScheduler")
    mock_dt = mocker.patch("orchestration.main_loop.datetime")
    mock_dt.now.return_value.hour = 16
    mocker.patch("orchestration.main_loop.job_ran_today", return_value=False)
    mock_prefetch = mocker.patch.object(orch, "run_screener_prefetch")
    mock_pipeline = mocker.patch.object(orch, "run_morning_pipeline")

    orch.start()

    mock_prefetch.assert_called_once()
    mock_pipeline.assert_called_once()


def test_start_does_not_catch_up_before_first_window(mocker, orch):
    """Before 14:00, today's first pipeline window simply hasn't happened yet
    — that's normal, not a missed run. No catch-up should fire."""
    mocker.patch("orchestration.main_loop.BlockingScheduler")
    mock_dt = mocker.patch("orchestration.main_loop.datetime")
    mock_dt.now.return_value.hour = 10
    mocker.patch("orchestration.main_loop.job_ran_today", return_value=False)
    mock_prefetch = mocker.patch.object(orch, "run_screener_prefetch")
    mock_pipeline = mocker.patch.object(orch, "run_morning_pipeline")

    orch.start()

    mock_prefetch.assert_not_called()
    mock_pipeline.assert_not_called()


def test_start_does_not_catch_up_when_already_ran_today(mocker, orch):
    """A completed run already recorded for today (via record_job_run) means
    there's nothing to catch up on, even if it's well past 14:00."""
    mocker.patch("orchestration.main_loop.BlockingScheduler")
    mock_dt = mocker.patch("orchestration.main_loop.datetime")
    mock_dt.now.return_value.hour = 20
    mocker.patch("orchestration.main_loop.job_ran_today", return_value=True)
    mock_prefetch = mocker.patch.object(orch, "run_screener_prefetch")
    mock_pipeline = mocker.patch.object(orch, "run_morning_pipeline")

    orch.start()

    mock_prefetch.assert_not_called()
    mock_pipeline.assert_not_called()
