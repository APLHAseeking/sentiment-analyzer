from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sys
from dataclasses import dataclass

import yfinance as yf

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
    # Short interest and liquidity
    short_interest_pct: float | None  # % of float sold short
    avg_daily_volume_usd: float | None  # average daily dollar volume
    # Analyst consensus
    analyst_target: float | None
    analyst_rating: str | None
    num_analysts: int | None
    # News
    headlines: tuple[str, ...]
    # Sentiment (populated by _score_sentiment; None if scoring failed)
    sentiment_label: str | None = None
    sentiment_strength: int | None = None
    sentiment_themes: tuple[str, ...] = ()
    sentiment_news_count: int = 0
    momentum_12m: float | None = None  # 12-month return (factor screener signal)


def _fmt(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:{spec}}{suffix}"


def format_research_for_prompt(report: ResearchReport) -> str:
    mcap = report.market_cap / 1e9 if report.market_cap else None
    fcf = report.free_cash_flow / 1e9 if report.free_cash_flow else None
    adv = report.avg_daily_volume_usd / 1e6 if report.avg_daily_volume_usd else None

    mom_1m = f"{report.momentum_1m:+.1f}%" if report.momentum_1m is not None else "n/a"
    mom_3m = f"{report.momentum_3m:+.1f}%" if report.momentum_3m is not None else "n/a"
    mom_12m = f"{report.momentum_12m:+.1f}%" if report.momentum_12m is not None else "n/a"
    rev_g = f"{report.revenue_growth * 100:+.1f}%" if report.revenue_growth is not None else "n/a"
    earn_g = f"{report.earnings_growth * 100:+.1f}%" if report.earnings_growth is not None else "n/a"
    roe_s = f"{report.roe * 100:.1f}%" if report.roe is not None else "n/a"
    margin_s = f"{report.profit_margin * 100:.1f}%" if report.profit_margin is not None else "n/a"
    si_s = f"{report.short_interest_pct:.1f}%" if report.short_interest_pct is not None else "n/a"
    headline_lines = "\n".join(f"- {h}" for h in report.headlines) or "- None"

    out = (
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
        f"Momentum: {mom_1m} (1m) | {mom_3m} (3m) | {mom_12m} (12m) | "
        f"52w ${_fmt(report.week52_low, '.2f')}–${_fmt(report.week52_high, '.2f')} | "
        f"Beta {_fmt(report.beta, '.2f')}\n"
        f"Growth: Revenue {rev_g} YoY | Earnings {earn_g} YoY\n"
        f"Analyst consensus: {report.analyst_rating or 'n/a'} | "
        f"Target ${_fmt(report.analyst_target, '.2f')} | "
        f"Coverage: {report.num_analysts or 'n/a'} analysts\n"
        f"Short interest: {si_s} of float | ADV: ${_fmt(adv, '.0f')}M/day\n"
    )
    if report.sentiment_label is not None:
        sentiment_str = f"{report.sentiment_label}/{report.sentiment_strength}"
        if report.sentiment_themes:
            sentiment_str += f" — themes: {', '.join(report.sentiment_themes)}"
        out += f"Sentiment ({report.sentiment_news_count} headlines, AI-scored): {sentiment_str}\n"
    out += f"Recent headlines:\n<external_data>\n{headline_lines}\n</external_data>\n---"
    return out


_RATING_MAP: dict[str, str] = {
    "strong_buy": "Buy", "buy": "Buy",
    "hold": "Hold", "neutral": "Hold",
    "sell": "Sell", "strong_sell": "Sell",
}

_sentiment_client: "OpenAI | None" = None


def _get_sentiment_client() -> "OpenAI":
    global _sentiment_client
    if _sentiment_client is None:
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("Missing required env var: OPENAI_API_KEY")
        _sentiment_client = OpenAI(api_key=api_key)
    return _sentiment_client


_SENTIMENT_SYSTEM = (
    "You are a financial news sentiment analyzer. "
    "Respond with ONLY valid JSON matching exactly: "
    '{"sentiment": "bullish"|"neutral"|"bearish", "strength": 1|2|3, '
    '"key_themes": ["theme1", "theme2"]}'
)


def _score_sentiment(
    news_block: str,
) -> tuple[str | None, int | None, tuple[str, ...]]:
    try:
        client = _get_sentiment_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=128,
            temperature=0,
            seed=0,
            messages=[
                {"role": "system", "content": _SENTIMENT_SYSTEM},
                {"role": "user", "content": news_block},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        label = data.get("sentiment")
        strength = int(data.get("strength", 1))
        themes = tuple(str(t) for t in data.get("key_themes", [])[:3])
        if label not in ("bullish", "neutral", "bearish"):
            return None, None, ()
        if strength not in (1, 2, 3):
            return None, None, ()
        return label, strength, themes
    except Exception:
        return None, None, ()


def _try_fincept(ticker: str) -> dict | None:
    """Attempt to load extended data from FinceptTerminal if available."""
    from system.config import settings
    fincept_path = settings.credentials.fincept_scripts_path
    if not fincept_path:  # guard: empty string would insert CWD into sys.path
        return None
    if fincept_path not in sys.path:
        sys.path.insert(0, fincept_path)
    try:
        from equityInvestment.base.data_providers import YahooFinanceProvider
        company = YahooFinanceProvider().get_company_data(ticker)
        return {
            "fd": company.financial_data,
            "md": company.market_data,
            "name": company.name,
            "sector": company.sector,
            "market_cap": company.market_cap,
        }
    except Exception:
        return None


def gather_research(
    ticker: str,
    momentum_1m_override: float | None = None,
    momentum_3m_override: float | None = None,
    momentum_12m_override: float | None = None,
) -> ResearchReport | None:
    try:
        t = yf.Ticker(ticker)
        info = t.info

        # Skip the history download if the caller already computed momentum
        # (factor screener pre-fetches 12 months; re-downloading 3 months here wastes time)
        if momentum_1m_override is not None and momentum_3m_override is not None:
            momentum_1m = momentum_1m_override
            momentum_3m = momentum_3m_override
        else:
            hist = t.history(period="3mo")
            momentum_1m = momentum_3m = None
            if not hist.empty and len(hist) >= 2:
                current = hist["Close"].iloc[-1]
                price_1m = hist["Close"].iloc[max(0, len(hist) - 21)]
                price_3m = hist["Close"].iloc[0]
                momentum_1m = (current / price_1m - 1) * 100
                momentum_3m = (current / price_3m - 1) * 100
            if momentum_1m_override is not None:
                momentum_1m = momentum_1m_override
            if momentum_3m_override is not None:
                momentum_3m = momentum_3m_override

        momentum_12m = momentum_12m_override

        raw_rating = (info.get("recommendationKey") or "").lower()
        analyst_rating = _RATING_MAP.get(raw_rating)

        avg_volume = info.get("averageVolume") or 0
        current_price = info.get("regularMarketPrice") or 0
        avg_daily_volume_usd = avg_volume * current_price if avg_volume and current_price else None

        short_float = info.get("shortPercentOfFloat")
        short_interest_pct = float(short_float) * 100 if short_float else None

        news_items = t.news[:28]

        # Build rich block (title + summary) for sentiment scoring
        news_texts = []
        for item in news_items:
            title = (item.get("content") or {}).get("title", "")
            summary = (item.get("content") or {}).get("summary", "")
            if title:
                entry = f"- {title}"
                if summary:
                    entry += f": {summary}"
                news_texts.append(entry)
        scored_count = len(news_texts)
        news_block = "\n".join(news_texts)

        # headlines field: titles only (first 8, for prompt display)
        headlines = tuple(
            (item.get("content") or {}).get("title", "")
            for item in news_items[:8]
            if (item.get("content") or {}).get("title")
        )

        sentiment_label, sentiment_strength, sentiment_themes = (
            _score_sentiment(news_block) if news_block else (None, None, ())
        )

        def _f(val: object) -> float | None:
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        fincept = _try_fincept(ticker)
        if fincept:
            fd, md = fincept["fd"], fincept["md"]
            name = fincept["name"]
            sector = fincept["sector"]
            market_cap = fincept["market_cap"]
            roe = _f(fd.get("roe"))
            roa = _f(fd.get("roa"))
            profit_margin = _f(fd.get("profit_margin"))
            debt_to_equity = _f(fd.get("debt_to_equity"))
            current_ratio = _f(fd.get("current_ratio"))
            free_cash_flow = _f(fd.get("free_cash_flow"))
            pe_trailing = _f(md.get("pe_ratio"))
            pe_forward = _f(md.get("forward_pe"))
            pb_ratio = _f(md.get("pb_ratio"))
            ps_ratio = _f(md.get("ps_ratio"))
            peg_ratio = _f(md.get("peg_ratio"))
            revenue_growth = _f(md.get("revenue_growth"))
            earnings_growth = _f(md.get("earnings_growth"))
            beta = _f(md.get("beta"))
            week52_high = _f(md.get("52_week_high"))
            week52_low = _f(md.get("52_week_low"))
        else:
            name = info.get("shortName") or info.get("longName") or ticker
            sector = info.get("sector", "Unknown")
            market_cap = _f(info.get("marketCap")) or 0
            roe = _f(info.get("returnOnEquity"))
            roa = _f(info.get("returnOnAssets"))
            profit_margin = _f(info.get("profitMargins"))
            debt_to_equity = _f(info.get("debtToEquity"))
            current_ratio = _f(info.get("currentRatio"))
            free_cash_flow = _f(info.get("freeCashflow"))
            pe_trailing = _f(info.get("trailingPE"))
            pe_forward = _f(info.get("forwardPE"))
            pb_ratio = _f(info.get("priceToBook"))
            ps_ratio = _f(info.get("priceToSalesTrailing12Months"))
            peg_ratio = _f(info.get("pegRatio"))
            revenue_growth = _f(info.get("revenueGrowth"))
            earnings_growth = _f(info.get("earningsGrowth"))
            beta = _f(info.get("beta"))
            week52_high = _f(info.get("fiftyTwoWeekHigh"))
            week52_low = _f(info.get("fiftyTwoWeekLow"))

        return ResearchReport(
            ticker=ticker.upper(),
            company_name=name,
            sector=sector,
            market_cap=market_cap,
            pe_trailing=pe_trailing,
            pe_forward=pe_forward,
            pb_ratio=pb_ratio,
            ps_ratio=ps_ratio,
            peg_ratio=peg_ratio,
            ev_ebitda=_f(info.get("enterpriseToEbitda")),  # always from yfinance; not in Fincept provider
            roe=roe,
            roa=roa,
            profit_margin=profit_margin,
            debt_to_equity=debt_to_equity,
            current_ratio=current_ratio,
            free_cash_flow=free_cash_flow,
            revenue_growth=revenue_growth,
            earnings_growth=earnings_growth,
            beta=beta,
            week52_high=week52_high,
            week52_low=week52_low,
            momentum_1m=momentum_1m,
            momentum_3m=momentum_3m,
            momentum_12m=momentum_12m,
            short_interest_pct=short_interest_pct,
            avg_daily_volume_usd=avg_daily_volume_usd,
            analyst_target=_f(info.get("targetMeanPrice")),
            analyst_rating=analyst_rating,
            num_analysts=info.get("numberOfAnalystOpinions"),
            headlines=headlines,
            sentiment_label=sentiment_label,
            sentiment_strength=sentiment_strength,
            sentiment_themes=sentiment_themes,
            sentiment_news_count=scored_count,
        )

    except Exception as exc:
        log.warning("gather_research(%s) failed — skipping research: %s", ticker, exc)
        return None


def gather_research_batch(
    tickers: list[str],
    max_workers: int = 5,
) -> dict[str, "ResearchReport | None"]:
    """Fetch ResearchReport for multiple tickers concurrently.

    Returns a dict keyed by ticker in the same order as the input list.
    Tickers that fail (or where gather_research returns None) map to None.
    """
    if not tickers:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {t: pool.submit(gather_research, t) for t in tickers}
    result: dict[str, "ResearchReport | None"] = {}
    for t in tickers:
        try:
            result[t] = futures[t].result()
        except Exception as exc:
            log.warning("gather_research_batch: failed for %s: %s", t, exc)
            result[t] = None
    return result
