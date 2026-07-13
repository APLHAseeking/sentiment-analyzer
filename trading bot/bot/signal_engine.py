import functools
import re
from datetime import date
import pandas as pd
import yfinance as yf

import bot.db as db
from market_data.yf_session import get_shared_yf_session
from bot.universe import is_in_universe
from bot.committee import get_committees_for_politician, sector_has_committee_overlap
from system.config import settings

MIN_TRADE_USD = 15_000  # lower bound of the "$15,001 – $50,000" Capitol Trades bracket


def compute_lag_days(transaction_date: str, disclosure_date: str) -> int:
    t = date.fromisoformat(transaction_date)
    d = date.fromisoformat(disclosure_date)
    return (d - t).days


@functools.lru_cache(maxsize=2000)
def get_sector_for_ticker(ticker: str) -> str:
    return yf.Ticker(ticker, session=get_shared_yf_session()).info.get("sector", "Unknown")


def clear_sector_cache() -> None:
    get_sector_for_ticker.cache_clear()


@functools.lru_cache(maxsize=32)
def get_etf_close_history(etf_ticker: str, period: str = "2y") -> pd.Series:
    return yf.Ticker(etf_ticker, session=get_shared_yf_session()).history(period=period)["Close"]


def clear_etf_cache() -> None:
    get_etf_close_history.cache_clear()


def parse_amount_min_usd(amount_range: str) -> int:
    """Extract the lower bound of the Capitol Trades amount bracket in USD."""
    digits = re.sub(r"[^\d]", "", amount_range.split("-")[0].split("–")[0])
    return int(digits) if digits else 0


def is_large_enough_trade(amount_range: str, min_usd: int = MIN_TRADE_USD) -> bool:
    return parse_amount_min_usd(amount_range) >= min_usd


def get_cluster_count(ticker: str, since_date: str) -> int:
    """Count distinct politicians with "buy" disclosures for ticker since since_date."""
    rows = db.get_recent_disclosures_for_ticker(ticker, since_date)
    return len({r["politician"] for r in rows if r["transaction_type"] == "buy"})


def is_qualified_signal(disclosure: dict) -> bool:
    if disclosure["transaction_type"] != "buy":
        return False
    if not is_large_enough_trade(disclosure.get("amount_range", "")):
        return False
    lag = compute_lag_days(disclosure["transaction_date"], disclosure["disclosure_date"])
    if lag < 0 or lag > settings.universe.max_lag_days:
        return False
    if not is_in_universe(disclosure["ticker"]):
        return False
    committees = get_committees_for_politician(disclosure["politician"])
    if not committees:
        return False
    sector = get_sector_for_ticker(disclosure["ticker"])
    return sector_has_committee_overlap(sector, committees)


def filter_disclosures(disclosures: list[dict]) -> list[dict]:
    return [d for d in disclosures if is_qualified_signal(d)]
