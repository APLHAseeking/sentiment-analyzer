# screener/ff_factors.py
"""Fetch and cache daily Fama-French 3 factors + momentum (Ken French's
Dartmouth data library — free, public, no API key).

Two separate files must be merged by date: the 3-factor file (Mkt-RF, SMB,
HML, RF) and the momentum file (Mom) are published as separate downloads.
Output matches backtesting/attribution.py's load_factor_returns() format:
first column 'Date' (YYYY-MM-DD string), remaining columns are decimal
returns (source files are in percent) — used for factor attribution in the
Phase 0 PIT backtest, per docs/PIT_DATA_REQUIREMENTS.md's ff_factors.csv spec.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

_FACTORS_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)
_MOMENTUM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Momentum_Factor_daily_CSV.zip"
)
_TIMEOUT_SECONDS = 30


def _fetch_zip_csv(url: str) -> str:
    """Download a Ken French zip file and return the inner CSV's raw text."""
    resp = requests.get(url, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        name = z.namelist()[0]
        return z.read(name).decode("utf-8", errors="replace")


def _parse_ff_csv(text: str, value_columns: list[str]) -> pd.DataFrame:
    """Parse a Ken French daily CSV.

    A variable number of free-text description lines precede the real header
    row (first cell blank, e.g. ',Mkt-RF,SMB,HML,RF' or ',Mom,') — found by
    scanning for the first line that starts with ',' and mentions every
    expected column name (verified empirically against the real files:
    factors header at line 4, momentum header at line 13 — not a fixed
    offset). Daily data rows ('YYYYMMDD,val,val,...') continue until a blank
    line, which ends the daily table (annual figures, if present, follow
    after and must not be parsed as daily rows).
    """
    lines = text.splitlines()
    header_idx = next(
        i for i, line in enumerate(lines)
        if line.startswith(",") and all(col in line for col in value_columns)
    )
    data_lines = []
    for line in lines[header_idx + 1:]:
        if not line.strip():
            break
        data_lines.append(line)

    rows = []
    for line in data_lines:
        parts = [p.strip() for p in line.split(",")]
        date_str = parts[0]
        if not (len(date_str) == 8 and date_str.isdigit()):
            continue
        values = [float(v) / 100.0 for v in parts[1:1 + len(value_columns)]]
        rows.append([date_str] + values)

    df = pd.DataFrame(rows, columns=["Date"] + value_columns)
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    return df


def fetch_ff_factors(cache_path: Path) -> pd.DataFrame:
    """Fetch (or load from permanent cache) daily Fama-French 3 factors + momentum.

    Cache never expires — Ken French's historical daily figures don't get
    revised, matching this repo's other permanent PIT caches
    (screener/xbrl_pit_sue.py, backtesting/pit_constituents.py).

    Returns columns [Date, Mkt-RF, SMB, HML, RF, Mom], Date as 'YYYY-MM-DD'
    strings, values as decimals (not percent).
    """
    if cache_path.exists():
        return pd.read_csv(cache_path)

    factors_df = _parse_ff_csv(
        _fetch_zip_csv(_FACTORS_URL), ["Mkt-RF", "SMB", "HML", "RF"]
    )
    momentum_df = _parse_ff_csv(_fetch_zip_csv(_MOMENTUM_URL), ["Mom"])
    merged = factors_df.merge(momentum_df, on="Date", how="inner")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(cache_path, index=False)
    log.info(
        "Fetched %d days of Fama-French factor returns (%s to %s), cached to %s",
        len(merged), merged["Date"].min(), merged["Date"].max(), cache_path,
    )
    return merged
