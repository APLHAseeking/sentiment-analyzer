"""Point-in-time (PIT) data interface for survivorship-free backtesting.

Problem
-------
yfinance .info returns *current* fundamentals. Historical backtests using it are
look-ahead biased: we would see today's P/E ratio when scoring a trade from 2020.
This module defines a pluggable abstraction so the backtest can be fed PIT data
from any vendor (Sharadar, Compustat, Norgate, Tiingo, etc.) without changing
the scoring logic.

Pluggable design
----------------
Define ``PITDataProvider`` once. Write one concrete implementation per data source.
The runner (``run_strategy_backtest.py``) only calls the abstract interface.

Currently provided
------------------
``CSVPITProvider`` — reads three local CSV/parquet snapshots:
  1. ``constituents.csv``   — index membership by date (survivorship-free)
  2. ``fundamentals.csv``   — PIT fundamental snapshots (one per quarter / filing)
  3. ``prices.csv``         — split/dividend-adjusted daily closes including delisted names

See ``docs/PIT_DATA_REQUIREMENTS.md`` for exact schemas.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


class PITDataProvider(ABC):
    """Abstract interface for point-in-time data. All methods must be PIT-safe.

    "PIT-safe" means: information returned for ``as_of=d`` must have been
    publicly available on date ``d``. For fundamentals this means respecting
    the reporting lag (typically 45–75 days after quarter-end).
    """

    @abstractmethod
    def constituents(self, as_of: date) -> set[str]:
        """Return the set of index members that were in the universe on ``as_of``.

        Callers must not trade a ticker that was not in the universe on that date
        (survivorship bias prevention).
        """

    @abstractmethod
    def fundamentals(self, ticker: str, as_of: date) -> dict | None:
        """Return the most recent PIT fundamental snapshot for ``ticker`` as of ``as_of``.

        Keys mirror yfinance .info so the existing ``_build_factor_df`` can consume
        them without modification: ``trailingPE``, ``priceToBook``, ``freeCashflow``,
        ``marketCap``, ``returnOnEquity``, ``profitMargins``, ``debtToEquity``, ``sector``.

        Returns None if no snapshot is available before ``as_of``.
        """

    @abstractmethod
    def prices(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return split/dividend-adjusted daily closes for ``ticker`` in [start, end].

        Must include delisted names for their live period. Returns an empty Series
        if the ticker has no data in the requested window.
        """


# ── CSV / Parquet implementation ─────────────────────────────────────────────

class CSVPITProvider(PITDataProvider):
    """Reads PIT data from local CSV or parquet files.

    Parameters
    ----------
    constituents_path   : path to constituents file (see PIT_DATA_REQUIREMENTS.md)
    fundamentals_path   : path to fundamentals file
    prices_path         : path to prices file (wide format: date index, ticker columns)

    All three files can be CSV (``.csv``) or parquet (``.parquet``).
    """

    def __init__(
        self,
        constituents_path: str,
        fundamentals_path: str,
        prices_path: str,
    ) -> None:
        self._constituents = self._load_constituents(constituents_path)
        self._fundamentals = self._load_fundamentals(fundamentals_path)
        self._prices = self._load_prices(prices_path)

    # ── Loading helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _read_file(path: str) -> pd.DataFrame:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"PIT data file not found: {path}")
        if p.suffix == ".parquet":
            return pd.read_parquet(p)
        return pd.read_csv(p, parse_dates=["date"])

    def _load_constituents(self, path: str) -> pd.DataFrame:
        """Schema: date (YYYY-MM-DD), ticker (str).
        Each row = ticker was in the universe as of that date snapshot.
        """
        df = self._read_file(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["ticker"] = df["ticker"].str.upper().str.strip()
        df = df.sort_values("date")
        log.info("CSVPITProvider: loaded %d constituent rows from %s", len(df), path)
        return df

    def _load_fundamentals(self, path: str) -> pd.DataFrame:
        """Schema: date, ticker, trailingPE, priceToBook, freeCashflow, marketCap,
        returnOnEquity, profitMargins, debtToEquity, sector.
        date = first date this fundamental snapshot was publicly available
        (i.e. filing date, not quarter-end).
        """
        df = self._read_file(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["ticker"] = df["ticker"].str.upper().str.strip()
        df = df.sort_values(["ticker", "date"])
        log.info("CSVPITProvider: loaded %d fundamental rows from %s", len(df), path)
        return df

    def _load_prices(self, path: str) -> pd.DataFrame:
        """Wide format: date index, one column per ticker (split-adj close).
        date column must be named 'date'.
        """
        df = self._read_file(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.set_index("date").sort_index()
        df.columns = [str(c).upper().strip() for c in df.columns]
        log.info("CSVPITProvider: loaded prices for %d tickers, %d rows from %s",
                 len(df.columns), len(df), path)
        return df

    # ── Public interface ──────────────────────────────────────────────────────

    def constituents(self, as_of: date) -> set[str]:
        """Return the most recent constituent snapshot on or before ``as_of``."""
        before = self._constituents[self._constituents["date"] <= as_of]
        if before.empty:
            return set()
        # Latest snapshot date
        latest_date = before["date"].max()
        return set(before.loc[before["date"] == latest_date, "ticker"])

    def fundamentals(self, ticker: str, as_of: date) -> dict | None:
        """Return the most recent PIT fundamental snapshot for ``ticker`` on or before ``as_of``."""
        t = ticker.upper()
        mask = (self._fundamentals["ticker"] == t) & (self._fundamentals["date"] <= as_of)
        subset = self._fundamentals[mask]
        if subset.empty:
            return None
        row = subset.iloc[-1]  # most recent (df is sorted by ticker, date)
        result = row.drop(["ticker", "date"]).to_dict()
        # Convert numeric-looking strings to float; leave sector as str
        for k, v in result.items():
            if k != "sector":
                try:
                    result[k] = float(v) if v is not None and str(v) != "nan" else None
                except (ValueError, TypeError):
                    result[k] = None
        return result

    def prices(self, ticker: str, start: date, end: date) -> pd.Series:
        """Return split-adjusted daily closes for ``ticker`` in [start, end]."""
        t = ticker.upper()
        if t not in self._prices.columns:
            return pd.Series(dtype=float)
        col = self._prices[t].dropna()
        mask = (col.index >= start) & (col.index <= end)
        result = col[mask]
        if result.empty:
            return pd.Series(dtype=float)
        # Return with DatetimeIndex so simulate_portfolio and metrics can join on it
        return pd.Series(
            result.values,
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in result.index]),
        )

    def all_tickers(self) -> list[str]:
        """Return all tickers present in the price file."""
        return list(self._prices.columns)
