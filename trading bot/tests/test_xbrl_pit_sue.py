# tests/test_xbrl_pit_sue.py
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import requests

from screener.xbrl_pit_sue import fetch_companyfacts_eps

_SAMPLE_PAYLOAD = {
    "facts": {
        "us-gaap": {
            "EarningsPerShareDiluted": {
                "units": {
                    "USD/shares": [
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
                         "accn": "0001-1", "fy": 2022, "fp": "Q1", "form": "10-Q",
                         "filed": "2022-05-01"},
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
                         "accn": "0001-2", "fy": 2023, "fp": "Q1", "form": "10-Q",
                         "filed": "2023-05-01", "frame": "CY2022Q1"},
                    ]
                }
            }
        }
    }
}


def test_fetch_companyfacts_eps_parses_facts_to_dataframe(tmp_path):
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = _SAMPLE_PAYLOAD
        mock_get.return_value.raise_for_status.return_value = None

        df = fetch_companyfacts_eps(cik=320193, cache_dir=tmp_path)

    assert len(df) == 2
    assert set(df.columns) == {"start", "end", "val", "form", "filed", "accn"}
    assert df.iloc[0]["val"] == 1.10
    mock_get.assert_called_once()
    url = mock_get.call_args[0][0]
    assert url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


def test_fetch_companyfacts_eps_uses_parquet_cache(tmp_path):
    cache_file = tmp_path / "0000320193.parquet"
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = _SAMPLE_PAYLOAD
        mock_get.return_value.raise_for_status.return_value = None
        fetch_companyfacts_eps(cik=320193, cache_dir=tmp_path)
        assert cache_file.exists()

        # Second call must hit the parquet cache, not the network.
        mock_get.reset_mock()
        df2 = fetch_companyfacts_eps(cik=320193, cache_dir=tmp_path)
        mock_get.assert_not_called()
        assert len(df2) == 2


def test_fetch_companyfacts_eps_missing_concept_returns_empty(tmp_path):
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"facts": {"us-gaap": {}}}
        mock_get.return_value.raise_for_status.return_value = None
        df = fetch_companyfacts_eps(cik=1, cache_dir=tmp_path)
    assert df.empty


def test_fetch_companyfacts_eps_404_caches_empty_result(tmp_path):
    """A 404 (no such CIK) is a confirmed absence — cache it so we don't refetch."""
    cache_file = tmp_path / "0000000002.parquet"
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        df = fetch_companyfacts_eps(cik=2, cache_dir=tmp_path)

    assert df.empty
    assert cache_file.exists()

    # A second call must hit the cache, not the network again.
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get2:
        df2 = fetch_companyfacts_eps(cik=2, cache_dir=tmp_path)
        mock_get2.assert_not_called()
    assert df2.empty


def test_fetch_companyfacts_eps_request_exception_does_not_cache(tmp_path):
    """A transient network failure is NOT a confirmed absence — must not poison the cache."""
    cache_file = tmp_path / "0000000003.parquet"
    with patch("screener.xbrl_pit_sue.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.RequestException("connection reset")
        df = fetch_companyfacts_eps(cik=3, cache_dir=tmp_path)

    assert df.empty
    assert not cache_file.exists()
