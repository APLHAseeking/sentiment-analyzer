from __future__ import annotations

# yfinance GICS sector string -> sector ETF ticker. Unknown/missing sectors should use
# .get() and treat the result as neutral (omit rs_vs_sector_3m_pct), not an error.
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}
