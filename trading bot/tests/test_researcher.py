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
