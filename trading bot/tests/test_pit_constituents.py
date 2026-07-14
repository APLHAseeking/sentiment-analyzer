# tests/test_pit_constituents.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtesting.pit_constituents import fetch_sp500_pit_constituents

_SAMPLE_CSV = (
    "date,tickers\n"
    '2012-01-03,"AAPL,MSFT,XOM"\n'
    '2012-02-01,"AAPL,MSFT,XOM,GOOGL"\n'
)


def test_fetch_sp500_pit_constituents_melts_wide_to_long(tmp_path):
    with patch("backtesting.pit_constituents._download_raw_csv", return_value=_SAMPLE_CSV):
        df = fetch_sp500_pit_constituents(cache_path=tmp_path / "constituents.parquet")

    assert list(df.columns) == ["date", "ticker"]
    assert len(df) == 7  # 3 + 4
    row = df[(df["ticker"] == "GOOGL")]
    assert str(row.iloc[0]["date"]) == "2012-02-01"
    assert "GOOGL" not in set(df[df["date"] == pd.Timestamp("2012-01-03").date()]["ticker"])


def test_fetch_sp500_pit_constituents_uses_cache(tmp_path):
    cache_path = tmp_path / "constituents.parquet"
    with patch("backtesting.pit_constituents._download_raw_csv", return_value=_SAMPLE_CSV) as mock_dl:
        fetch_sp500_pit_constituents(cache_path=cache_path)
        mock_dl.assert_called_once()
        mock_dl.reset_mock()
        fetch_sp500_pit_constituents(cache_path=cache_path)
        mock_dl.assert_not_called()
