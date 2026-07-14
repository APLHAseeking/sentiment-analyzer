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


from screener.xbrl_pit_sue import original_quarterly_eps


def test_original_quarterly_eps_picks_earliest_non_amendment():
    facts = pd.DataFrame([
        # Original 10-Q: single quarter, filed first.
        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
         "form": "10-Q", "filed": "2022-05-01", "accn": "a1"},
        # Same period re-reported as comparative data in next year's 10-Q — later filed.
        {"start": "2022-01-01", "end": "2022-03-31", "val": 1.10,
         "form": "10-Q", "filed": "2023-05-01", "accn": "a2"},
        # 9-month cumulative fact for a DIFFERENT period — must be excluded (not ~1 quarter).
        {"start": "2022-01-01", "end": "2022-09-30", "val": 3.40,
         "form": "10-Q", "filed": "2022-11-01", "accn": "a3"},
    ])
    result = original_quarterly_eps(facts)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["val"] == 1.10
    assert str(row["filed"]) == "2022-05-01"
    assert row["cy_year"] == 2022
    assert row["cy_quarter"] == 1


def test_original_quarterly_eps_excludes_amendments_even_if_earlier():
    facts = pd.DataFrame([
        # A 10-K/A filed BEFORE the (hypothetically late-filed) original — still excluded.
        {"start": "2021-01-01", "end": "2021-03-31", "val": 2.00,
         "form": "10-Q/A", "filed": "2021-05-01", "accn": "b1"},
        {"start": "2021-01-01", "end": "2021-03-31", "val": 1.95,
         "form": "10-Q", "filed": "2021-05-10", "accn": "b2"},
    ])
    result = original_quarterly_eps(facts)
    assert len(result) == 1
    assert result.iloc[0]["val"] == 1.95
    assert str(result.iloc[0]["filed"]) == "2021-05-10"


def test_original_quarterly_eps_no_original_excludes_period():
    facts = pd.DataFrame([
        {"start": "2021-01-01", "end": "2021-03-31", "val": 2.00,
         "form": "10-Q/A", "filed": "2021-05-01", "accn": "c1"},
    ])
    result = original_quarterly_eps(facts)
    assert result.empty


def test_original_quarterly_eps_empty_input():
    result = original_quarterly_eps(pd.DataFrame(columns=["start", "end", "val", "form", "filed", "accn"]))
    assert result.empty


from datetime import date
from screener.xbrl_pit_sue import pit_eps_asof, pit_sue_asof


def _quarterly(rows):
    return pd.DataFrame(rows, columns=["cy_year", "cy_quarter", "val", "filed"])


def test_pit_eps_asof_excludes_not_yet_filed_quarters():
    quarterly = _quarterly([
        (2023, 4, 1.50, "2024-02-01"),
        (2023, 3, 1.40, "2023-11-01"),
        (2023, 2, 1.30, "2023-08-01"),
        (2023, 1, 1.20, "2023-05-01"),
        (2022, 4, 1.45, "2023-02-01"),
    ])
    # as_of is BEFORE the 2023Q4 filing date -> that quarter must not appear.
    series = pit_eps_asof(quarterly, as_of=date(2024, 1, 15), n_quarters=6)
    assert series[0] is None  # 2023Q4 (today's/newest calendar quarter) not yet filed
    # 2023Q3 should be the first populated slot.
    assert series[1] == 1.40


def test_pit_sue_asof_delegates_to_unmodified_formula():
    quarterly = _quarterly([
        (2023, 4, 1.50, "2024-02-01"),
        (2023, 3, 1.40, "2023-11-01"),
        (2023, 2, 1.30, "2023-08-01"),
        (2023, 1, 1.20, "2023-05-01"),
        (2022, 4, 1.30, "2023-02-01"),
        (2022, 3, 1.25, "2022-11-01"),
        (2022, 2, 1.15, "2022-08-01"),
        (2022, 1, 1.05, "2022-05-01"),
        (2021, 4, 1.20, "2022-02-01"),
    ])
    result = pit_sue_asof(quarterly, as_of=date(2024, 2, 5))
    # Anchor = 2023Q4 (1.50), t-4 = 2022Q4 (1.30) -> latest_change = 0.20
    from screener.xbrl_fundamentals import sue_from_quarterly_eps
    expected = sue_from_quarterly_eps(
        pit_eps_asof(quarterly, as_of=date(2024, 2, 5), n_quarters=14)
    )
    assert result == expected
    assert result is not None


def test_pit_eps_asof_includes_quarter_filed_exactly_on_as_of():
    # _completed_quarters(date(2024, 2, 1), 1) == [(2023, 4)]: Feb 1 2024 sits in
    # Q1 2024 (not yet completed), so the most recent completed quarter is 2023Q4.
    quarterly = _quarterly([
        (2023, 4, 1.10, "2024-02-01"),
    ])
    series = pit_eps_asof(quarterly, as_of=date(2024, 2, 1), n_quarters=1)
    assert series[0] == 1.10
