# backtesting/pit_constituents.py
"""PIT S&P 500 membership from the free, actively-maintained fja05680/sp500
historical-components dataset (verified live 2026-07-14: date,tickers
snapshots 1996-01-02 through 2026-06-30). Converts to the long-format
schema backtesting/pit_data.py's CSVPITProvider expects (date, ticker).

This is the constituents piece of the Phase 0 PIT-data blocker
(docs/PHASE0_FINDINGS.md) — used here for the SUE PIT backtest only.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

_RAW_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes(Updated).csv"
)


def _download_raw_csv() -> str:
    resp = requests.get(_RAW_URL, timeout=60)
    resp.raise_for_status()
    return resp.text


def fetch_sp500_pit_constituents(cache_path: Path) -> pd.DataFrame:
    """Return long-format (date, ticker) PIT membership, cached to parquet."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    raw = _download_raw_csv()
    wide = pd.read_csv(io.StringIO(raw), parse_dates=["date"])
    rows = []
    for _, row in wide.iterrows():
        d = row["date"].date()
        for ticker in str(row["tickers"]).split(","):
            ticker = ticker.strip().upper()
            if ticker:
                rows.append((d, ticker))
    long_df = pd.DataFrame(rows, columns=["date", "ticker"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    long_df.to_parquet(cache_path)
    log.info("PIT S&P 500 constituents: %d (date,ticker) rows cached to %s", len(long_df), cache_path)
    return long_df
