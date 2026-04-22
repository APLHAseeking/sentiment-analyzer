import io
import pandas as pd
import requests

_UNIVERSE: set[str] = set()


def _fetch_sp500() -> pd.DataFrame:
    tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    return tables[0][["Symbol"]]


def _fetch_russell1000() -> pd.DataFrame:
    url = (
        "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), skiprows=9)
    return df[["Ticker"]].dropna()


def _build_universe() -> set[str]:
    sp500 = set(_fetch_sp500()["Symbol"].str.strip().str.upper())
    russell = set(_fetch_russell1000()["Ticker"].str.strip().str.upper())
    return sp500 | russell


def refresh_universe() -> None:
    global _UNIVERSE
    _UNIVERSE = _build_universe()


def is_in_universe(ticker: str) -> bool:
    return ticker.upper() in _UNIVERSE
