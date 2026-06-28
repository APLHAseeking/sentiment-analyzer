"""Qualification + cluster logic for the insider (Form 4) signal source.

Mirrors `bot/signal_engine.py` (congressional) but for corporate insiders. Keeps
the scoring/sizing/risk pipeline untouched — this module only decides which raw
Form 4 buys are worth sending to the AI scorer."""
from __future__ import annotations

from datetime import date, timedelta

import bot.db as db
from bot.signal_engine import compute_lag_days
from bot.universe import is_in_universe
from system.config import settings

_C_SUITE_KEYWORDS = ("chief executive", "ceo", "chief financial", "cfo",
                     "president", "chair")


def is_c_suite(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in _C_SUITE_KEYWORDS)


def get_insider_cluster_count(ticker: str, since_date: str) -> int:
    """Count distinct insiders with open-market buys for ticker since since_date."""
    rows = db.get_recent_insider_disclosures_for_ticker(ticker, since_date)
    return len({r["insider_name"] for r in rows if r["transaction_type"] == "buy"})


def is_qualified_insider(disclosure: dict) -> bool:
    cfg = settings.insider
    if disclosure.get("transaction_type") != "buy":
        return False
    if float(disclosure.get("amount_usd", 0) or 0) < cfg.min_trade_usd:
        return False
    lag = compute_lag_days(disclosure["transaction_date"], disclosure["disclosure_date"])
    if lag < 0 or lag > cfg.max_lag_days:
        return False
    return is_in_universe(disclosure["ticker"])


def filter_insider_disclosures(disclosures: list[dict]) -> list[dict]:
    """Qualify raw Form 4 buys, then rank best-first so the per-day cap keeps the
    strongest signals (C-suite > cluster > dollar size)."""
    qualified = [d for d in disclosures if is_qualified_insider(d)]
    since = (date.today() - timedelta(days=settings.insider.cluster_window_days)).isoformat()

    def _strength(d: dict) -> tuple:
        cluster = get_insider_cluster_count(d["ticker"], since)
        return (is_c_suite(d.get("title", "")), cluster, float(d.get("amount_usd", 0) or 0))

    return sorted(qualified, key=_strength, reverse=True)
