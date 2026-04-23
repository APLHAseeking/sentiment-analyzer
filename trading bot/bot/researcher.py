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


import sys
import os
import yfinance as yf


def _setup_fincept_path() -> None:
    from bot.config import FINCEPT_SCRIPTS_PATH
    if FINCEPT_SCRIPTS_PATH not in sys.path:
        sys.path.insert(0, FINCEPT_SCRIPTS_PATH)


_RATING_MAP: dict[str, str] = {
    "strong_buy": "Buy", "buy": "Buy",
    "hold": "Hold", "neutral": "Hold",
    "sell": "Sell", "strong_sell": "Sell",
}


def gather_research(ticker: str) -> ResearchReport | None:
    try:
        _setup_fincept_path()
        from equityInvestment.base.data_providers import YahooFinanceProvider

        company = YahooFinanceProvider().get_company_data(ticker)
        fd = company.financial_data
        md = company.market_data

        hist = yf.Ticker(ticker).history(period="3mo")
        momentum_1m = momentum_3m = None
        if not hist.empty and len(hist) >= 2:
            current = hist["Close"].iloc[-1]
            price_1m = hist["Close"].iloc[max(0, len(hist) - 21)]
            price_3m = hist["Close"].iloc[0]
            momentum_1m = (current / price_1m - 1) * 100
            momentum_3m = (current / price_3m - 1) * 100

        info = yf.Ticker(ticker).info
        raw_rating = (info.get("recommendationKey") or "").lower()
        analyst_rating = _RATING_MAP.get(raw_rating)
        analyst_target = info.get("targetMeanPrice")
        ev_ebitda_raw = info.get("enterpriseToEbitda")

        news_items = yf.Ticker(ticker).news[:8]
        headlines = tuple(
            item.get("content", {}).get("title", "")
            for item in news_items
            if item.get("content", {}).get("title")
        )

        def _f(val: object) -> float | None:
            return float(val) if val else None

        return ResearchReport(
            ticker=ticker.upper(),
            company_name=company.name,
            sector=company.sector,
            market_cap=company.market_cap,
            pe_trailing=_f(md.get("pe_ratio")),
            pe_forward=_f(md.get("forward_pe")),
            pb_ratio=_f(md.get("pb_ratio")),
            ps_ratio=_f(md.get("ps_ratio")),
            peg_ratio=_f(md.get("peg_ratio")),
            ev_ebitda=_f(ev_ebitda_raw),
            roe=_f(fd.get("roe")),
            roa=_f(fd.get("roa")),
            profit_margin=_f(fd.get("profit_margin")),
            debt_to_equity=_f(fd.get("debt_to_equity")),
            current_ratio=_f(fd.get("current_ratio")),
            free_cash_flow=_f(fd.get("free_cash_flow")),
            revenue_growth=_f(md.get("revenue_growth")),
            earnings_growth=_f(md.get("earnings_growth")),
            beta=_f(md.get("beta")),
            week52_high=_f(md.get("52_week_high")),
            week52_low=_f(md.get("52_week_low")),
            momentum_1m=momentum_1m,
            momentum_3m=momentum_3m,
            analyst_target=_f(analyst_target),
            analyst_rating=analyst_rating,
            headlines=headlines,
        )

    except Exception as exc:
        log.warning("gather_research(%s) failed — skipping research: %s", ticker, exc)
        return None
