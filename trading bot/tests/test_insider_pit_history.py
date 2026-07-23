"""Tests for screener/insider_pit_history.py — the historical, date-range
Form 4 daily-index walker used to PIT-backtest the insider signal.
Mirrors tests/test_insider.py's mocking conventions. All offline."""
from __future__ import annotations

from datetime import date

import pandas as pd
import requests

from screener.insider_pit_history import (
    _parse_form_idx_with_cik,
    fetch_form4_index_for_date,
    pilot_request_volume,
    walk_daily_indexes,
)

_FORM_IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    July 6, 2026

Form Type   Company Name                          CIK         Date Filed  File Name
---------------------------------------------------------------------------------------
3           Foo Corp                              123456      20260706    edgar/data/123456/0001-23-000009.txt
4           Bar Inc                               777888      20260706    edgar/data/777888/0001234567-26-000123.txt
4           Zap Inc                                888999      20260706    edgar/data/888999/0009876543-26-000456.txt
4/A         Baz LLC                               999         20260706    edgar/data/999/0001-23-000010.txt
10-K        Qux Co                                55555       20260706    edgar/data/55555/0001-23-000011.txt
"""


def test_parse_form_idx_with_cik_keeps_only_exact_form_4_and_exposes_cik():
    df = _parse_form_idx_with_cik(_FORM_IDX, "2026-07-06")
    assert len(df) == 2
    row = df[df["cik"] == 777888].iloc[0]
    assert row["accession"] == "0001234567-26-000123"
    assert row["href"] == "https://www.sec.gov/Archives/edgar/data/777888/000123456726000123"
    assert row["filing_date"] == "2026-07-06"


def test_fetch_form4_index_for_date_uses_cache_without_network(tmp_path, mocker):
    cache_dir = tmp_path / "insider_index"
    cache_dir.mkdir()
    cached = pd.DataFrame(
        [{"cik": 1, "accession": "a", "href": "h", "filing_date": "2022-01-03"}]
    )
    cached.to_parquet(cache_dir / "2022-01-03.parquet")

    get_mock = mocker.patch("screener.insider_pit_history.requests.get")
    result = fetch_form4_index_for_date(date(2022, 1, 3), cache_dir)

    get_mock.assert_not_called()
    assert len(result) == 1
    assert result.iloc[0]["cik"] == 1


def test_fetch_form4_index_for_date_caches_404_permanently(tmp_path, mocker):
    """A weekend/holiday (no index published) must cache an empty frame —
    permanent, since a holiday never later publishes an index."""
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(status_code=404)
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = fetch_form4_index_for_date(date(2022, 1, 1), cache_dir)  # a Saturday

    assert result.empty
    assert (cache_dir / "2022-01-01.parquet").exists()


def test_fetch_form4_index_for_date_caches_403_access_denied_permanently(tmp_path, mocker):
    """Live-verified this session: SEC's Archives are S3-backed with public
    ListBucket disabled, so a genuinely missing daily-index file (weekend/
    holiday) 403s with an S3 AccessDenied body, not a 404 — bot/insider.py's
    live walker assumes 404, this must not repeat that assumption or every
    weekend/holiday would be treated as a transient failure and re-fetched
    forever instead of permanently cached as not-published."""
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(
        status_code=403,
        text='<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>',
    )
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = fetch_form4_index_for_date(date(2023, 6, 3), cache_dir)  # a Saturday

    assert result.empty
    assert (cache_dir / "2023-06-03.parquet").exists()


def test_fetch_form4_index_for_date_does_not_cache_a_non_access_denied_403(tmp_path, mocker):
    """A 403 WITHOUT the S3 AccessDenied body is a real access-denial or
    rate-limit response, not "day not published" — must not be cached
    permanently, or a genuine rate-limit block would silently and
    permanently blank out real data for that day."""
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(status_code=403, text="Rate limit exceeded")
    resp.raise_for_status = mocker.MagicMock(side_effect=requests.exceptions.HTTPError("403"))
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = fetch_form4_index_for_date(date(2023, 6, 5), cache_dir)  # a Monday

    assert result.empty
    assert not (cache_dir / "2023-06-05.parquet").exists()


def test_fetch_form4_index_for_date_does_not_cache_transient_failure(tmp_path, mocker):
    """Mirrors the Tiingo-429-cached-as-permanent-miss bug found and fixed
    in Phase 0 — a network error must NOT be cached, so a later run can
    still find real data."""
    import requests

    cache_dir = tmp_path / "insider_index"
    mocker.patch(
        "screener.insider_pit_history.requests.get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    )
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = fetch_form4_index_for_date(date(2022, 1, 3), cache_dir)

    assert result.empty
    assert not (cache_dir / "2022-01-03.parquet").exists()


def test_fetch_form4_index_for_date_parses_and_caches_real_response(tmp_path, mocker):
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(status_code=200, text=_FORM_IDX)
    resp.raise_for_status = mocker.MagicMock()
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = fetch_form4_index_for_date(date(2026, 7, 6), cache_dir)

    assert len(result) == 2
    assert (cache_dir / "2026-07-06.parquet").exists()
    # Second call must hit the cache, not the network again.
    get_mock = mocker.patch("screener.insider_pit_history.requests.get")
    fetch_form4_index_for_date(date(2026, 7, 6), cache_dir)
    get_mock.assert_not_called()


def test_walk_daily_indexes_filters_by_cik(tmp_path, mocker):
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(status_code=200, text=_FORM_IDX)
    resp.raise_for_status = mocker.MagicMock()
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = walk_daily_indexes(
        date(2026, 7, 6), date(2026, 7, 6), cache_dir, cik_filter={777888},
    )

    assert len(result) == 1
    assert result.iloc[0]["cik"] == 777888


def test_walk_daily_indexes_no_filter_keeps_all_form4(tmp_path, mocker):
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(status_code=200, text=_FORM_IDX)
    resp.raise_for_status = mocker.MagicMock()
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = walk_daily_indexes(date(2026, 7, 6), date(2026, 7, 6), cache_dir)

    assert len(result) == 2


def test_pilot_request_volume_reports_window_and_candidate_counts(tmp_path, mocker):
    cache_dir = tmp_path / "insider_index"
    resp = mocker.MagicMock(status_code=200, text=_FORM_IDX)
    resp.raise_for_status = mocker.MagicMock()
    mocker.patch("screener.insider_pit_history.requests.get", return_value=resp)
    mocker.patch("screener.insider_pit_history.time.sleep")

    result = pilot_request_volume(
        date(2026, 7, 6), date(2026, 7, 7), cache_dir, cik_filter={777888},
    )

    assert result["window_days"] == 2
    assert result["index_requests"] == 2
    # Same _FORM_IDX served both days (mocked) -> 1 in-universe candidate/day
    assert result["candidate_filings_in_universe"] == 2
    assert len(result["candidates"]) == 2
