"""Tests for earnings/FOMC event calendar exclusion window."""
from datetime import date
from unittest.mock import MagicMock
import pytest


def test_fomc_date_within_window_blocks(mocker):
    from utils.event_calendar import has_upcoming_event
    # May 7 2026 is a scheduled FOMC announcement; today = May 5 → 2 days away
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=MagicMock(calendar={}))
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 5, 5))
    assert result is True
    assert "FOMC" in reason


def test_fomc_date_outside_window_passes(mocker):
    from utils.event_calendar import has_upcoming_event
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=MagicMock(calendar={}))
    # April 1 — no FOMC within 2 days
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 1))
    assert result is False
    assert reason == ""


def test_earnings_within_window_blocks(mocker):
    from utils.event_calendar import has_upcoming_event
    mock_ticker = MagicMock()
    mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 10)]}
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=mock_ticker)
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 9))
    assert result is True
    assert "earnings" in reason.lower()


def test_earnings_outside_window_passes(mocker):
    from utils.event_calendar import has_upcoming_event
    mock_ticker = MagicMock()
    mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 20)]}
    mocker.patch("utils.event_calendar.yf.Ticker", return_value=mock_ticker)
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 9))
    assert result is False


def test_yfinance_failure_does_not_raise(mocker):
    from utils.event_calendar import has_upcoming_event
    mocker.patch("utils.event_calendar.yf.Ticker", side_effect=Exception("network"))
    # yfinance failure skips earnings check silently; FOMC check still runs
    result, reason = has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 1))
    assert isinstance(result, bool)


def test_earnings_lookup_passes_shared_session(mocker):
    """yf.Ticker() with no session leaks a fresh curl_cffi session per call
    and never times out — _get_next_earnings must pass the shared, timeout-
    bound session (market_data/yf_session.py) instead of yfinance's default."""
    from utils.event_calendar import has_upcoming_event
    mock_ticker = MagicMock()
    mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 20)]}
    mock_yf_ticker = mocker.patch("utils.event_calendar.yf.Ticker", return_value=mock_ticker)
    has_upcoming_event("AAPL", window_days=2, today=date(2026, 4, 9))
    assert mock_yf_ticker.call_args.kwargs.get("session") is not None


def test_process_signal_skips_on_upcoming_event(mocker):
    """_process_signal must return early without calling score_entry_with_debate."""
    from unittest.mock import MagicMock
    from orchestration.main_loop import RegimeAwareOrchestrator
    from system.config import settings

    mocker.patch("orchestration.main_loop.get_committees_for_politician",
                 return_value=["House Energy"])
    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.compute_lag_days", return_value=5)
    mocker.patch("orchestration.main_loop.get_cluster_count", return_value=1)  # avoids DB hit
    mocker.patch("orchestration.main_loop.has_upcoming_event",
                 return_value=(True, "FOMC 2026-05-07"))
    score_spy = mocker.patch("orchestration.main_loop.score_entry_with_debate")
    mocker.patch("orchestration.main_loop.write_status_file")  # prevent clobbering the live bot's status file

    o = RegimeAwareOrchestrator(settings)
    o._broker = MagicMock()
    o._broker.get_cash.return_value = 100_000.0
    o._regime_state = None

    disc = {
        "id": "d1", "politician": "Jane Doe", "ticker": "AAPL",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }
    o._process_signal(disc, {})
    score_spy.assert_not_called()


def test_process_fundamental_candidate_skips_on_upcoming_event(mocker):
    """_process_fundamental_candidate must return False without calling score_entry_with_debate."""
    from orchestration.main_loop import RegimeAwareOrchestrator
    from system.config import settings
    from screener.factor_scorer import FactorCandidate

    mocker.patch("orchestration.main_loop.get_sector_for_ticker",
                 return_value="Technology")
    mocker.patch("orchestration.main_loop.has_upcoming_event",
                 return_value=(True, "earnings 2026-05-09"))
    score_spy = mocker.patch("orchestration.main_loop.score_entry_with_debate")
    mocker.patch("orchestration.main_loop.write_status_file")  # prevent clobbering the live bot's status file

    o = RegimeAwareOrchestrator(settings)
    o._regime_state = None

    candidate = FactorCandidate(
        ticker="MSFT",
        composite_score=75,
        value_score=25,
        momentum_score=25,
        quality_score=25,
        research=None,
    )
    result = o._process_fundamental_candidate(candidate, {}, set())
    assert result is False
    score_spy.assert_not_called()
