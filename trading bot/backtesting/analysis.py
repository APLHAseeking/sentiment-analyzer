"""Backtesting analysis utilities — regime, confidence, and exposure breakdowns."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def regime_performance(trades: list) -> dict[str, dict]:
    """Group SimTrade results by regime label at entry.

    Returns dict mapping regime_label → {n_trades, win_rate, avg_pnl_pct}.
    """
    if not trades:
        return {}
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[t.regime_at_entry].append(t)
    result = {}
    for label, group in groups.items():
        wins = sum(1 for t in group if t.pnl_pct > 0)
        result[label] = {
            "n_trades": len(group),
            "win_rate": wins / len(group),
            "avg_pnl_pct": sum(t.pnl_pct for t in group) / len(group),
        }
    return result


def confidence_bucket_performance(trades: list) -> dict[str, dict]:
    """Split trades into conviction buckets: low (1-3), mid (4-6), high (7-10).

    Returns dict mapping bucket name → {n_trades, win_rate, avg_pnl_pct}.
    """
    buckets: dict[str, list] = {"low": [], "mid": [], "high": []}
    for t in trades:
        c = t.conviction
        if c <= 3:
            buckets["low"].append(t)
        elif c <= 6:
            buckets["mid"].append(t)
        else:
            buckets["high"].append(t)
    result = {}
    for name, group in buckets.items():
        if not group:
            result[name] = {"n_trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0}
        else:
            wins = sum(1 for t in group if t.pnl_pct > 0)
            result[name] = {
                "n_trades": len(group),
                "win_rate": wins / len(group),
                "avg_pnl_pct": sum(t.pnl_pct for t in group) / len(group),
            }
    return result


def exposure_by_regime(states: list) -> dict[str, float]:
    """Compute fraction of bars spent in each regime label.

    Parameters
    ----------
    states : list of RegimeState objects (must have .regime_label attribute)

    Returns dict mapping label → fraction (sums to 1.0), or {} if empty.
    """
    if not states:
        return {}
    counts: dict[str, int] = defaultdict(int)
    for s in states:
        counts[s.regime_label] += 1
    total = len(states)
    return {label: count / total for label, count in counts.items()}
