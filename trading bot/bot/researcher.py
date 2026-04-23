from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchReport:
    ticker: str
    company_name: str
    sector: str
    market_cap: float
    # Valuation multiples
    pe_trailing: float | None
    pe_forward: float | None
    pb_ratio: float | None
    ps_ratio: float | None
    peg_ratio: float | None
    ev_ebitda: float | None
    # Financial health
    roe: float | None
    roa: float | None
    profit_margin: float | None
    debt_to_equity: float | None
    current_ratio: float | None
    free_cash_flow: float | None
    revenue_growth: float | None
    earnings_growth: float | None
    # Market context
    beta: float | None
    week52_high: float | None
    week52_low: float | None
    momentum_1m: float | None
    momentum_3m: float | None
    # Analyst consensus
    analyst_target: float | None
    analyst_rating: str | None
    # News
    headlines: tuple[str, ...]


def _fmt(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:{spec}}{suffix}"


def format_research_for_prompt(report: ResearchReport) -> str:
    mcap = report.market_cap / 1e9 if report.market_cap else None
    fcf = report.free_cash_flow / 1e9 if report.free_cash_flow else None

    mom_1m = f"{report.momentum_1m:+.1f}%" if report.momentum_1m is not None else "n/a"
    mom_3m = f"{report.momentum_3m:+.1f}%" if report.momentum_3m is not None else "n/a"
    rev_g = f"{report.revenue_growth * 100:+.1f}%" if report.revenue_growth is not None else "n/a"
    earn_g = f"{report.earnings_growth * 100:+.1f}%" if report.earnings_growth is not None else "n/a"
    roe_s = f"{report.roe * 100:.1f}%" if report.roe is not None else "n/a"
    margin_s = f"{report.profit_margin * 100:.1f}%" if report.profit_margin is not None else "n/a"

    headline_lines = "\n".join(f"- {h}" for h in report.headlines) or "- None"

    return (
        "--- INDEPENDENT RESEARCH ---\n"
        f"Company: {report.company_name} | Sector: {report.sector} | "
        f"Market cap: ${_fmt(mcap, '.1f')}B\n"
        f"Valuation: P/E {_fmt(report.pe_trailing, '.1f')}x "
        f"(fwd {_fmt(report.pe_forward, '.1f')}x) | "
        f"P/B {_fmt(report.pb_ratio, '.1f')}x | "
        f"EV/EBITDA {_fmt(report.ev_ebitda, '.1f')}x | "
        f"PEG {_fmt(report.peg_ratio, '.2f')}\n"
        f"Financial health: ROE {roe_s} | Margin {margin_s} | "
        f"D/E {_fmt(report.debt_to_equity, '.2f')} | "
        f"FCF ${_fmt(fcf, '.1f')}B\n"
        f"Momentum: {mom_1m} (1m) | {mom_3m} (3m) | "
        f"52w ${_fmt(report.week52_low, '.2f')}–${_fmt(report.week52_high, '.2f')} | "
        f"Beta {_fmt(report.beta, '.2f')}\n"
        f"Growth: Revenue {rev_g} YoY | Earnings {earn_g} YoY\n"
        f"Analyst consensus: {report.analyst_rating or 'n/a'} | "
        f"Target ${_fmt(report.analyst_target, '.2f')}\n"
        f"Recent headlines:\n{headline_lines}\n"
        "---"
    )
