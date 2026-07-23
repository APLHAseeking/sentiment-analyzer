# tests/test_ff_factors.py
from __future__ import annotations

import io
import zipfile
from unittest.mock import patch

import pandas as pd
import pytest

from screener.ff_factors import _parse_ff_csv, fetch_ff_factors


def _make_zip_response(inner_filename: str, text: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(inner_filename, text)
    mock_resp = type("R", (), {})()
    mock_resp.content = buf.getvalue()
    mock_resp.raise_for_status = lambda: None
    return mock_resp


_FACTORS_TEXT = (
    "This file was created by using the 202605 CRSP database.\n"
    "Some description line.\n"
    "Another description line.\n"
    "\n"
    ",Mkt-RF,SMB,HML,RF\n"
    "20260101,  1.00,  0.50, -0.25,  0.01\n"
    "20260102,  0.20, -0.10,  0.05,  0.01\n"
    "\n"
    "  Annual Factors: January-December \n"
    "20260000,  5.00,  1.00, -1.00,  0.10\n"
)

_MOMENTUM_TEXT = (
    "This file was created by using the 202605 CRSP database.  It,,\n"
    "contains a momentum factor description spanning,,\n"
    "several lines before the real header,,\n"
    "\n"
    ",Mom,\n"
    "20260101,0.30,\n"
    "20260102,-0.15,\n"
)


def test_parse_ff_csv_skips_description_header_lines():
    df = _parse_ff_csv(_FACTORS_TEXT, ["Mkt-RF", "SMB", "HML", "RF"])
    assert list(df.columns) == ["Date", "Mkt-RF", "SMB", "HML", "RF"]
    assert len(df) == 2  # the "Annual Factors" row after the blank line must not be included
    assert df.iloc[0]["Date"] == "2026-01-01"
    assert df.iloc[0]["Mkt-RF"] == pytest.approx(0.01)  # 1.00% -> 0.01 decimal


def test_parse_ff_csv_handles_different_header_offset_and_trailing_comma():
    """The momentum file's header appears at a different line offset than the
    factors file's, and has a trailing empty column (',Mom,') — both must
    still parse correctly, proving the header search isn't a fixed offset."""
    df = _parse_ff_csv(_MOMENTUM_TEXT, ["Mom"])
    assert list(df.columns) == ["Date", "Mom"]
    assert len(df) == 2
    assert df.iloc[1]["Mom"] == pytest.approx(-0.0015)  # -0.15% -> decimal


def test_fetch_ff_factors_merges_factors_and_momentum_by_date(tmp_path):
    cache_path = tmp_path / "ff_factors.csv"
    factors_resp = _make_zip_response("factors.csv", _FACTORS_TEXT)
    momentum_resp = _make_zip_response("momentum.csv", _MOMENTUM_TEXT)

    with patch("screener.ff_factors.requests.get", side_effect=[factors_resp, momentum_resp]):
        df = fetch_ff_factors(cache_path)

    assert list(df.columns) == ["Date", "Mkt-RF", "SMB", "HML", "RF", "Mom"]
    assert len(df) == 2
    assert df.iloc[0]["Mom"] == pytest.approx(0.003)  # 0.30% -> decimal
    assert cache_path.exists()


def test_fetch_ff_factors_uses_cache_without_network_call(tmp_path):
    cache_path = tmp_path / "ff_factors.csv"
    pd.DataFrame(
        {"Date": ["2026-01-01"], "Mkt-RF": [0.01], "SMB": [0.0], "HML": [0.0],
         "RF": [0.0], "Mom": [0.0]}
    ).to_csv(cache_path, index=False)

    with patch("screener.ff_factors.requests.get") as mock_get:
        df = fetch_ff_factors(cache_path)

    mock_get.assert_not_called()
    assert len(df) == 1
