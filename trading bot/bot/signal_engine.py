from datetime import date
import yfinance as yf
from bot.universe import is_in_universe
from bot.committee import get_committees_for_politician, sector_has_committee_overlap

MAX_LAG_DAYS = 45

def compute_lag_days(transaction_date: str, disclosure_date: str) -> int:
    t = date.fromisoformat(transaction_date)
    d = date.fromisoformat(disclosure_date)
    return (d - t).days

def get_sector_for_ticker(ticker: str) -> str:
    return yf.Ticker(ticker).info.get("sector", "Unknown")

def is_qualified_signal(disclosure: dict) -> bool:
    if disclosure["transaction_type"] != "purchase":
        return False
    lag = compute_lag_days(disclosure["transaction_date"], disclosure["disclosure_date"])
    if lag > MAX_LAG_DAYS:
        return False
    try:
        if not is_in_universe(disclosure["ticker"]):
            return False
    except RuntimeError:
        return False
    committees = get_committees_for_politician(disclosure["politician"])
    if not committees:
        return False
    sector = get_sector_for_ticker(disclosure["ticker"])
    return sector_has_committee_overlap(sector, committees)

def filter_disclosures(disclosures: list[dict]) -> list[dict]:
    return [d for d in disclosures if is_qualified_signal(d)]
