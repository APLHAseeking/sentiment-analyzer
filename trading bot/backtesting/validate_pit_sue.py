# backtesting/validate_pit_sue.py
"""One-off validation: compare PIT-reconstructed quarterly EPS (companyfacts,
earliest-original-filing) against the live frames-sourced value, for a small
sample of large-cap tickers and recent (unlikely-restated) quarters. Run
manually before the full backtest; not part of the pytest suite (hits the
network).
"""
from __future__ import annotations

from pathlib import Path

from screener.xbrl_fundamentals import _fetch_ticker_cik_map, _fetch_frame
from screener.xbrl_pit_sue import fetch_companyfacts_eps, original_quarterly_eps

_SAMPLE_TICKERS = ["AAPL", "MSFT", "JPM", "WMT", "JNJ"]
_CACHE_DIR = Path("pit_cache/companyfacts")


def main() -> None:
    cik_map = _fetch_ticker_cik_map(cache=None)
    frame = _fetch_frame("EarningsPerShareDiluted", "USD-per-shares", "CY2025Q1", cache=None)

    for ticker in _SAMPLE_TICKERS:
        cik = cik_map.get(ticker)
        if cik is None:
            print(f"{ticker}: no CIK found")
            continue
        facts = fetch_companyfacts_eps(cik, _CACHE_DIR)
        quarterly = original_quarterly_eps(facts)
        pit_row = quarterly[(quarterly["cy_year"] == 2025) & (quarterly["cy_quarter"] == 1)]
        pit_val = float(pit_row.iloc[0]["val"]) if not pit_row.empty else None
        frame_val = frame.get(cik)
        match = "OK" if pit_val is not None and frame_val is not None and abs(pit_val - frame_val) < 0.01 else "MISMATCH"
        print(f"{ticker}: PIT={pit_val}  frame={frame_val}  {match}")


if __name__ == "__main__":
    main()
