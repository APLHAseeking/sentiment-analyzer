"""Earnings and FOMC event exclusion window.

Prevents the bot from opening positions when a known market-moving
event falls within a configurable window of calendar days.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import yfinance as yf

log = logging.getLogger(__name__)

# Official FOMC announcement dates (second day of each two-day meeting).
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
# Update this list in January each year when the Fed publishes the new schedule.
_FOMC_DATES_2026: list[date] = [
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 5, 7),
    date(2026, 6, 18),
    date(2026, 7, 29),
    date(2026, 9, 17),
    date(2026, 10, 29),
    date(2026, 12, 10),
]

_FOMC_DATES: list[date] = _FOMC_DATES_2026  # extend here for future years


def _get_next_earnings(ticker: str) -> date | None:
    """Return the next scheduled earnings date for ticker via yfinance, or None."""
    try:
        cal: Any = yf.Ticker(ticker).calendar
        if not cal:
            return None
        dates = cal.get("Earnings Date", [])
        if not dates:
            return None
        raw = dates[0]
        if hasattr(raw, "date"):
            return raw.date()
        if isinstance(raw, date):
            return raw
        return None
    except Exception as exc:
        log.debug("Could not fetch earnings date for %s: %s", ticker, exc)
        return None


def has_upcoming_event(
    ticker: str,
    window_days: int = 2,
    today: date | None = None,
) -> tuple[bool, str]:
    """Return (True, reason) if an earnings or FOMC event is within window_days.

    Parameters
    ----------
    ticker        : ticker to check for upcoming earnings
    window_days   : calendar days; event on today through today+window_days is a block
    today         : override today's date (for testing)

    Returns
    -------
    (True, "FOMC 2026-05-07")         — FOMC within window
    (True, "earnings 2026-05-09")     — earnings within window
    (False, "")                        — no upcoming event
    """
    _today = today or date.today()

    for fomc_date in _FOMC_DATES:
        days_until = (fomc_date - _today).days
        if 0 <= days_until <= window_days:
            return True, f"FOMC {fomc_date.isoformat()}"

    earnings_date = _get_next_earnings(ticker)
    if earnings_date is not None:
        days_until = (earnings_date - _today).days
        if 0 <= days_until <= window_days:
            return True, f"earnings {earnings_date.isoformat()}"

    return False, ""
