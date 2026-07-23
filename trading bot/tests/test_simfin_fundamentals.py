# tests/test_simfin_fundamentals.py
from __future__ import annotations

import io
import zipfile
from unittest.mock import patch

import pandas as pd
import pytest

from screener.simfin_fundamentals import fetch_simfin_dataset, sector_map


def _make_zip_response(csv_text: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.csv", csv_text)
    mock_resp = type("R", (), {})()
    mock_resp.content = buf.getvalue()
    mock_resp.raise_for_status = lambda: None
    return mock_resp


_INCOME_CSV = (
    "Ticker;SimFinId;Currency;Fiscal Year;Fiscal Period;Report Date;Publish Date;"
    "Restated Date;Revenue;Net Income\n"
    "AAPL;111;USD;2024;Q1;2023-12-31;2024-02-01;2024-02-01;100;10\n"
)


def test_fetch_simfin_dataset_parses_semicolon_csv_and_caches(tmp_path):
    cache_path = tmp_path / "income.parquet"
    with patch("screener.simfin_fundamentals.requests.get",
               return_value=_make_zip_response(_INCOME_CSV)) as mock_get:
        df = fetch_simfin_dataset("income", cache_path, variant="quarterly")

    mock_get.assert_called_once()
    call_args = mock_get.call_args
    assert "dataset=income" in call_args[0][0]
    assert "variant=quarterly" in call_args[0][0]
    assert call_args[1]["headers"]["Authorization"].startswith("api-key ")
    assert list(df.columns) == [
        "Ticker", "SimFinId", "Currency", "Fiscal Year", "Fiscal Period",
        "Report Date", "Publish Date", "Restated Date", "Revenue", "Net Income",
    ]
    assert df.iloc[0]["Ticker"] == "AAPL"
    assert cache_path.exists()


def test_fetch_simfin_dataset_uses_cache_without_network_call(tmp_path):
    cache_path = tmp_path / "income.parquet"
    pd.DataFrame({"Ticker": ["AAPL"], "Revenue": [100]}).to_parquet(cache_path)

    with patch("screener.simfin_fundamentals.requests.get") as mock_get:
        df = fetch_simfin_dataset("income", cache_path)

    mock_get.assert_not_called()
    assert len(df) == 1


def test_fetch_simfin_dataset_raises_without_api_key(tmp_path):
    import dataclasses
    from system.config import settings as real_settings
    no_key_settings = dataclasses.replace(
        real_settings,
        credentials=dataclasses.replace(real_settings.credentials, simfin_api_key=""),
    )
    with patch("system.config.settings", no_key_settings):
        with pytest.raises(RuntimeError, match="SIMFIN_API_KEY"):
            fetch_simfin_dataset("income", tmp_path / "x.parquet")


def test_sector_map_builds_ticker_to_sector_dict():
    companies = pd.DataFrame({
        "Ticker": ["AAPL", "MSFT", None],
        "IndustryId": [100001, 100002, 100003],
    })
    industries = pd.DataFrame({
        "IndustryId": [100001, 100002],
        "Industry": ["Consumer Electronics", "Software"],
        "Sector": ["Technology", "Technology"],
    })
    result = sector_map(companies, industries)
    assert result == {"AAPL": "Technology", "MSFT": "Technology"}


def test_sector_map_excludes_unmatched_industry_ids():
    companies = pd.DataFrame({"Ticker": ["XYZ"], "IndustryId": [999999]})
    industries = pd.DataFrame({
        "IndustryId": [100001], "Industry": ["Consumer Electronics"], "Sector": ["Technology"],
    })
    result = sector_map(companies, industries)
    assert result == {}
