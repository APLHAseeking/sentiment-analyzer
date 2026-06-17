from technical.sector_map import SECTOR_ETF_MAP


def test_known_sectors_map_to_expected_etfs():
    assert SECTOR_ETF_MAP["Technology"] == "XLK"
    assert SECTOR_ETF_MAP["Financial Services"] == "XLF"
    assert SECTOR_ETF_MAP["Healthcare"] == "XLV"
    assert SECTOR_ETF_MAP["Energy"] == "XLE"
    assert SECTOR_ETF_MAP["Industrials"] == "XLI"
    assert SECTOR_ETF_MAP["Consumer Cyclical"] == "XLY"
    assert SECTOR_ETF_MAP["Consumer Defensive"] == "XLP"
    assert SECTOR_ETF_MAP["Utilities"] == "XLU"
    assert SECTOR_ETF_MAP["Real Estate"] == "XLRE"
    assert SECTOR_ETF_MAP["Basic Materials"] == "XLB"
    assert SECTOR_ETF_MAP["Communication Services"] == "XLC"


def test_unknown_sector_returns_none_via_get():
    assert SECTOR_ETF_MAP.get("Unknown Sector") is None
