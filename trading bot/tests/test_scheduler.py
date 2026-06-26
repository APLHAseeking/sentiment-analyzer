from unittest.mock import MagicMock, patch
from bot.scheduler import run_morning_pipeline, run_exit_review, run_eod_snapshot
from bot.ai_analyst import EntryScore, ExitDecision

def _make_portfolio(mocker):
    p = MagicMock()
    p.can_open_new_position.return_value = True
    p.get_cash.return_value = 100_000.0
    p.is_sector_capped.return_value = False
    p.broker.get_positions.return_value = []
    return p

def test_morning_skips_on_non_trading_day(mocker):
    mocker.patch("bot.scheduler._is_trading_day", return_value=False)
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_not_called()

def test_morning_no_signals(mocker):
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler.get_universe", return_value=set())
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[])
    mocker.patch("bot.scheduler.get_open_positions", return_value=[])
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_not_called()

def test_morning_opens_on_buy_signal(mocker, db):
    disc = {
        "id": "x1", "politician": "Jane Doe", "ticker": "XOM",
        "transaction_type": "buy",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["House Energy and Commerce"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Energy")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=1)
    mocker.patch("bot.scheduler.gather_research", return_value=None)
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 100.0}
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_called_once()

def test_exit_review_closes_on_exit(mocker, db):
    db.insert_disclosures([{
        "id": "pos1", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "buy", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }])
    sid = db.insert_signal("pos1", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 150.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 155.0}
    mocker.patch("bot.scheduler.gather_research", return_value=None)
    mocker.patch("bot.scheduler.review_exit", return_value=ExitDecision("exit", "Take profit"))
    portfolio = _make_portfolio(mocker)
    run_exit_review(portfolio)
    assert portfolio.close_position.call_count == 1
    call_kwargs = portfolio.close_position.call_args
    assert call_kwargs[0][0] == "AAPL"  # ticker
    assert call_kwargs[1]["exit_reason"] == "ai_exit"

def test_morning_calls_gather_research_per_qualified_signal(mocker, db):
    disc = {
        "id": "x1", "politician": "Jane Doe", "ticker": "XOM",
        "transaction_type": "buy",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["Energy"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Energy")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=1)
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 100.0}
    mock_research = mocker.patch("bot.scheduler.gather_research", return_value=None)
    portfolio = _make_portfolio(mocker)

    run_morning_pipeline(portfolio)

    mock_research.assert_called_once_with("XOM")


def test_exit_review_calls_gather_research(mocker, db):
    db.insert_disclosures([{
        "id": "pos1", "politician": "Jane", "ticker": "AAPL",
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "buy", "amount_range": "$15,001 - $50,000",
        "scraped_at": "2026-04-22T08:00:00",
    }])
    sid = db.insert_signal("pos1", "AAPL", 8, 5.0, "Good", [])
    db.insert_position("AAPL", 150.0, 10.0, 5.0, "2026-04-01", sid, "Test")
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 155.0}
    mocker.patch("bot.scheduler.review_exit", return_value=ExitDecision("hold", "Ok"))
    mock_research = mocker.patch("bot.scheduler.gather_research", return_value=None)
    portfolio = _make_portfolio(mocker)

    run_exit_review(portfolio)

    mock_research.assert_called_once_with("AAPL")


def test_eod_snapshot(mocker, db):
    portfolio = _make_portfolio(mocker)
    portfolio.broker = MagicMock()
    portfolio.broker.get_cash.return_value = 95_000.0
    portfolio.broker.get_positions.return_value = [
        {"ticker": "AAPL", "qty": 10.0, "current_price": 155.0, "avg_entry_price": 150.0}
    ]
    from bot.portfolio import Portfolio
    real_portfolio = Portfolio(broker=portfolio.broker)
    run_eod_snapshot(real_portfolio)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM portfolio_log").fetchall()
    assert len(rows) == 1
    import pytest
    assert rows[0]["total_nav"] == pytest.approx(95_000.0 + 1_550.0)


def test_morning_pipeline_skips_sector_capped_trade(mocker, db):
    disc = {
        "id": "sc-001", "politician": "Jane Doe", "ticker": "NVDA",
        "transaction_type": "buy",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["Senate Commerce"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Technology")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=1)
    mocker.patch("bot.scheduler.gather_research", return_value=None)
    portfolio = mocker.MagicMock()
    portfolio.can_open_new_position.return_value = True
    portfolio.get_cash.return_value = 100_000.0
    portfolio.broker.get_positions.return_value = []
    # Sector already capped at 35% (above 30% cap)
    mocker.patch("bot.scheduler._compute_sector_allocation", return_value={"Technology": 35.0})
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_not_called()


def test_morning_pipeline_passes_cluster_count_to_score_entry(mocker, db):
    disc = {
        "id": "cl-001", "politician": "Jane Doe", "ticker": "MSFT",
        "transaction_type": "buy",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["Senate Commerce"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Technology")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=3)
    mocker.patch("bot.scheduler.gather_research", return_value=None)
    mocker.patch("bot.scheduler._compute_sector_allocation", return_value={})
    from bot.ai_analyst import EntryScore
    mock_score_entry = mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=3, position_pct=0.0, rationale="Skip", entry="skip", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    portfolio = mocker.MagicMock()
    portfolio.can_open_new_position.return_value = True
    portfolio.get_cash.return_value = 100_000.0
    portfolio.broker.get_positions.return_value = []
    portfolio.is_sector_capped.return_value = False
    run_morning_pipeline(portfolio)
    call_kwargs = mock_score_entry.call_args[1]
    assert call_kwargs["cluster_count"] == 3


def test_morning_pipeline_skips_illiquid_trade(mocker, db):
    from bot.researcher import ResearchReport
    disc = {
        "id": "lq-001", "politician": "Jane Doe", "ticker": "ILLIQ",
        "transaction_type": "buy",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["Senate Banking"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Financial Services")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=1)
    mocker.patch("bot.scheduler._compute_sector_allocation", return_value={})
    from bot.ai_analyst import EntryScore
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=()
    ))
    mocker.patch("bot.scheduler.insert_signal", return_value=1)
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 100.0}
    # Research with tiny ADV: position is $5K, ADV is $30K → 16.7% > 10% → blocked
    illiquid_report = ResearchReport(
        ticker="ILLIQ", company_name="Illiquid Corp", sector="Financial Services",
        market_cap=1e8, pe_trailing=None, pe_forward=None, pb_ratio=None,
        ps_ratio=None, peg_ratio=None, ev_ebitda=None, roe=None, roa=None,
        profit_margin=None, debt_to_equity=None, current_ratio=None,
        free_cash_flow=None, revenue_growth=None, earnings_growth=None,
        beta=None, week52_high=None, week52_low=None, momentum_1m=None,
        momentum_3m=None, short_interest_pct=None,
        avg_daily_volume_usd=30_000.0,  # $30K ADV
        analyst_target=None, analyst_rating=None, num_analysts=None,
        headlines=(),
    )
    mocker.patch("bot.scheduler.gather_research", return_value=illiquid_report)
    portfolio = mocker.MagicMock()
    portfolio.can_open_new_position.return_value = True
    portfolio.get_cash.return_value = 100_000.0
    portfolio.broker.get_positions.return_value = []
    portfolio.is_sector_capped.return_value = False
    portfolio.is_liquid_enough.return_value = False  # explicitly say not liquid
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_not_called()


def test_phase2_opens_fundamental_position(mocker, db):
    from screener.factor_scorer import FactorCandidate
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler.get_universe", return_value={"MSFT"})
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[
        FactorCandidate(ticker="MSFT", composite_score=85, value_score=30,
                        momentum_score=28, quality_score=27, research=None),
    ])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Technology")
    mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=8, position_pct=5.0, rationale="Good", entry="buy", risk_flags=()
    ))
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 300.0}
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    portfolio.open_position.assert_called_once()
    call_kwargs = portfolio.open_position.call_args[1]
    assert call_kwargs["signal_source"] == "fundamental"


def test_phase2_skips_already_opened_ticker(mocker, db):
    from screener.factor_scorer import FactorCandidate
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[])
    mocker.patch("bot.scheduler.get_open_positions", return_value=[
        {"ticker": "MSFT", "entry_price": 300.0, "shares": 10.0,
         "entry_date": "2026-04-01", "signal_id": 1, "signal_source": "congressional"},
    ])
    mocker.patch("bot.scheduler.get_universe", return_value={"MSFT"})
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[
        FactorCandidate(ticker="MSFT", composite_score=85, value_score=30,
                        momentum_score=28, quality_score=27, research=None),
    ])
    mock_score = mocker.patch("bot.scheduler.score_entry")
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    mock_score.assert_not_called()
    portfolio.open_position.assert_not_called()


def test_phase2_uses_both_signal_type_when_congress_skipped(mocker, db):
    from screener.factor_scorer import FactorCandidate
    disc = {
        "id": "both-001", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_type": "buy",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    mocker.patch("bot.scheduler._is_trading_day", return_value=True)
    mocker.patch("bot.scheduler.run_scraper", return_value=[disc])
    mocker.patch("bot.scheduler.filter_disclosures", return_value=[disc])
    mocker.patch("bot.scheduler.get_committees_for_politician", return_value=["House Energy"])
    mocker.patch("bot.scheduler.get_sector_for_ticker", return_value="Technology")
    mocker.patch("bot.scheduler.compute_lag_days", return_value=2)
    mocker.patch("bot.scheduler.get_cluster_count", return_value=1)
    mocker.patch("bot.scheduler.gather_research", return_value=None)
    mocker.patch("bot.scheduler.get_universe", return_value={"AAPL"})
    mocker.patch("bot.scheduler.run_factor_screen", return_value=[
        FactorCandidate(ticker="AAPL", composite_score=82, value_score=29,
                        momentum_score=27, quality_score=26, research=None),
    ])
    mock_score = mocker.patch("bot.scheduler.score_entry", return_value=EntryScore(
        conviction=3, position_pct=0.0, rationale="Skip", entry="skip", risk_flags=()
    ))
    mocker.patch("bot.scheduler.yf.Ticker").return_value.info = {"regularMarketPrice": 170.0}
    portfolio = _make_portfolio(mocker)
    run_morning_pipeline(portfolio)
    # Phase 2 call should use signal_type="both"
    assert mock_score.call_count >= 2
    phase2_call = mock_score.call_args_list[-1]
    assert phase2_call[1].get("signal_type") == "both"
