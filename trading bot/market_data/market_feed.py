"""Market data fetching for regime detection.

Fetches daily bars for SPY and VIX via yfinance. This is the data layer
for the regime engine — not for individual stock signal generation (that
uses bot/researcher.py).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

_CACHE: dict[str, pd.DataFrame] = {}


def fetch_daily_bars(
    ticker: str,
    years: int = 5,
    end: date | None = None,
) -> pd.DataFrame:
    """Return a DataFrame with columns: Open, High, Low, Close, Volume.

    Index is DatetimeIndex (UTC-naive dates). Forward-fills any missing bars
    (weekends are not returned by yfinance for equities, which is correct).
    """
    end_date = end or date.today()
    start_date = end_date - timedelta(days=years * 365 + 30)

    try:
        df = yf.download(
            ticker,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        log.error("Failed to fetch %s: %s", ticker, exc)
        return pd.DataFrame()

    if df.empty:
        log.warning("No data returned for %s", ticker)
        return df

    # Flatten MultiIndex columns if present (yfinance >=0.2.40 quirk)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    df.index = pd.to_datetime(df.index).normalize()
    df = df.sort_index()
    log.info("Fetched %d bars for %s (%s → %s)", len(df), ticker,
             df.index[0].date(), df.index[-1].date())
    return df


def get_regime_data(
    regime_ticker: str = "SPY",
    vix_ticker: str = "^VIX",
    years: int = 5,
    end: date | None = None,
) -> pd.DataFrame:
    """Return a merged DataFrame suitable for regime feature engineering.

    Columns: Close (regime_ticker), Volume (regime_ticker), VIX_Close.
    Rows with NaN VIX are forward-filled from the previous trading day.
    """
    spy = fetch_daily_bars(regime_ticker, years=years, end=end)
    vix = fetch_daily_bars(vix_ticker, years=years, end=end)

    if spy.empty:
        raise RuntimeError(f"Cannot fetch regime data: {regime_ticker} returned empty")

    result = spy[["Close", "Volume"]].copy()
    result.columns = ["close", "volume"]

    if not vix.empty:
        result["vix"] = vix["Close"].reindex(result.index).ffill()
    else:
        log.warning("VIX data unavailable — regime features will use only price/volume")
        result["vix"] = float("nan")

    result = result.dropna(subset=["close"])
    return result
