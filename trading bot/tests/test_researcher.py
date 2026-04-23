import pytest
from bot.researcher import ResearchReport, format_research_for_prompt


def _make_report(**overrides) -> ResearchReport:
    defaults = dict(
        ticker="AAPL", company_name="Apple Inc.", sector="Technology",
        market_cap=3_000_000_000_000,
        pe_trailing=28.5, pe_forward=24.0, pb_ratio=45.0, ps_ratio=7.5,
        peg_ratio=1.8, ev_ebitda=22.0,
        roe=1.60, roa=0.25, profit_margin=0.26, debt_to_equity=1.7,
        current_ratio=0.9, free_cash_flow=100_000_000_000,
        revenue_growth=0.05, earnings_growth=0.10,
        beta=1.2, week52_high=260.0, week52_low=165.0,
        momentum_1m=3.5, momentum_3m=12.0,
        analyst_target=230.0, analyst_rating="Buy",
        headlines=("Strong earnings", "New product launch"),
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


def test_format_includes_company_and_sector():
    text = format_research_for_prompt(_make_report())
    assert "Apple Inc." in text
    assert "Technology" in text


def test_format_includes_valuation_multiples():
    text = format_research_for_prompt(_make_report())
    assert "P/E" in text
    assert "EV/EBITDA" in text
    assert "28.5" in text


def test_format_none_fields_render_as_na():
    text = format_research_for_prompt(
        _make_report(pe_trailing=None, ev_ebitda=None, analyst_rating=None)
    )
    assert "n/a" in text


def test_format_none_fields_do_not_raise():
    format_research_for_prompt(
        _make_report(
            pe_trailing=None, pe_forward=None, pb_ratio=None, ps_ratio=None,
            peg_ratio=None, ev_ebitda=None, roe=None, roa=None,
            profit_margin=None, debt_to_equity=None, current_ratio=None,
            free_cash_flow=None, revenue_growth=None, earnings_growth=None,
            beta=None, week52_high=None, week52_low=None,
            momentum_1m=None, momentum_3m=None,
            analyst_target=None, analyst_rating=None,
        )
    )


def test_format_includes_headlines():
    text = format_research_for_prompt(
        _make_report(headlines=("Earnings beat", "New CEO appointed"))
    )
    assert "Earnings beat" in text
    assert "New CEO appointed" in text


def test_format_empty_headlines_shows_none_line():
    text = format_research_for_prompt(_make_report(headlines=()))
    assert "- None" in text


def test_format_includes_momentum_with_sign():
    text = format_research_for_prompt(_make_report(momentum_1m=3.5, momentum_3m=-2.1))
    assert "+3.5%" in text
    assert "-2.1%" in text


def test_format_wrapped_in_research_markers():
    text = format_research_for_prompt(_make_report())
    assert text.startswith("--- INDEPENDENT RESEARCH ---")
    assert text.endswith("---")


import sys
import pandas as pd


def _mock_fincept(mocker):
    """Patch sys.path setup and FinceptTerminal imports."""
    mocker.patch("bot.researcher._setup_fincept_path")

    mock_company = mocker.MagicMock()
    mock_company.name = "Exxon Mobil"
    mock_company.sector = "Energy"
    mock_company.market_cap = 5e11
    mock_company.financial_data = {
        "roe": 0.15, "roa": 0.08, "profit_margin": 0.10,
        "debt_to_equity": 0.3, "current_ratio": 1.2,
        "free_cash_flow": 2e10,
    }
    mock_company.market_data = {
        "pe_ratio": 12.0, "forward_pe": 10.0, "pb_ratio": 2.0,
        "ps_ratio": 1.5, "peg_ratio": 1.2, "beta": 0.9,
        "52_week_high": 120.0, "52_week_low": 85.0,
        "revenue_growth": 0.05, "earnings_growth": 0.08,
    }

    mock_provider_cls = mocker.MagicMock()
    mock_provider_cls.return_value.get_company_data.return_value = mock_company

    mock_dp_module = mocker.MagicMock()
    mock_dp_module.YahooFinanceProvider = mock_provider_cls

    mocker.patch.dict("sys.modules", {
        "equityInvestment": mocker.MagicMock(),
        "equityInvestment.base": mocker.MagicMock(),
        "equityInvestment.base.data_providers": mock_dp_module,
    })
    return mock_provider_cls


def _mock_yf_ticker(mocker, *, rating="buy", target=115.0, ev_ebitda=8.0,
                    news=None, prices=None):
    """Patch yfinance Ticker used inside gather_research."""
    if prices is None:
        prices = [100.0] * 63
    if news is None:
        news = [
            {"content": {"title": "Strong earnings"}},
            {"content": {"title": "New contract won"}},
        ]
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = pd.DataFrame({"Close": prices})
    mock_ticker.info = {
        "recommendationKey": rating,
        "targetMeanPrice": target,
        "enterpriseToEbitda": ev_ebitda,
    }
    mock_ticker.news = news
    mocker.patch("bot.researcher.yf.Ticker", return_value=mock_ticker)
    return mock_ticker


def test_gather_research_returns_report(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker)

    from bot.researcher import gather_research
    report = gather_research("XOM")

    assert report is not None
    assert report.ticker == "XOM"
    assert report.company_name == "Exxon Mobil"
    assert report.sector == "Energy"
    assert report.pe_trailing == 12.0
    assert report.ev_ebitda == 8.0
    assert report.analyst_rating == "Buy"
    assert report.analyst_target == 115.0
    assert "Strong earnings" in report.headlines


def test_gather_research_normalises_strong_buy_to_buy(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker, rating="strong_buy")

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.analyst_rating == "Buy"


def test_gather_research_normalises_strong_sell_to_sell(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker, rating="strong_sell")

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.analyst_rating == "Sell"


def test_gather_research_normalises_unknown_rating_to_none(mocker):
    _mock_fincept(mocker)
    _mock_yf_ticker(mocker, rating="underperform")

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.analyst_rating is None


def test_gather_research_computes_momentum(mocker):
    _mock_fincept(mocker)
    prices = [100.0] * 42 + [110.0] * 21
    _mock_yf_ticker(mocker, prices=prices)

    from bot.researcher import gather_research
    report = gather_research("XOM")
    assert report.momentum_3m is not None
    assert report.momentum_3m > 0


def test_gather_research_returns_none_on_provider_error(mocker):
    mocker.patch("bot.researcher._setup_fincept_path")
    mock_dp_module = mocker.MagicMock()
    mock_dp_module.YahooFinanceProvider.side_effect = Exception("Network error")
    mocker.patch.dict("sys.modules", {
        "equityInvestment": mocker.MagicMock(),
        "equityInvestment.base": mocker.MagicMock(),
        "equityInvestment.base.data_providers": mock_dp_module,
    })

    from bot.researcher import gather_research
    result = gather_research("BADTICKER")
    assert result is None


def test_gather_research_returns_none_on_yf_error(mocker):
    _mock_fincept(mocker)
    mocker.patch("bot.researcher.yf.Ticker", side_effect=Exception("yf down"))

    from bot.researcher import gather_research
    result = gather_research("XOM")
    assert result is None
