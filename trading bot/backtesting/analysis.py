"""Backtesting analysis utilities — regime, confidence, and exposure breakdowns."""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from backtesting.metrics import profit_factor as _profit_factor


def regime_performance(trades: list) -> dict[str, dict]:
    """Group SimTrade results by regime label at entry.

    Returns dict mapping regime_label →
    {n_trades, win_rate, avg_pnl_pct, profit_factor}.
    Labels with zero trades are omitted.
    """
    if not trades:
        return {}
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[t.regime_at_entry].append(t)
    result = {}
    for label, group in groups.items():
        wins = sum(1 for t in group if t.pnl_pct > 0)
        pnl_series = pd.Series([t.pnl_pct / 100 for t in group])
        result[label] = {
            "n_trades": len(group),
            "win_rate": wins / len(group),
            "avg_pnl_pct": sum(t.pnl_pct for t in group) / len(group),
            "profit_factor": round(_profit_factor(pnl_series), 3),
        }
    return result


def confidence_bucket_performance(trades: list) -> dict[str, dict]:
    """Split trades into conviction buckets: low [1-5], mid [6-7], high [8-10].

    Returns dict mapping bucket name →
    {n_trades, win_rate, avg_pnl_pct, profit_factor}.
    All three buckets are always present (n_trades=0 if empty).
    """
    buckets: dict[str, list] = {"low": [], "mid": [], "high": []}
    for t in trades:
        c = t.conviction
        if c <= 5:
            buckets["low"].append(t)
        elif c <= 7:
            buckets["mid"].append(t)
        else:
            buckets["high"].append(t)
    result = {}
    for name, group in buckets.items():
        if not group:
            result[name] = {
                "n_trades": 0, "win_rate": 0.0,
                "avg_pnl_pct": 0.0, "profit_factor": 0.0,
            }
        else:
            wins = sum(1 for t in group if t.pnl_pct > 0)
            pnl_series = pd.Series([t.pnl_pct / 100 for t in group])
            result[name] = {
                "n_trades": len(group),
                "win_rate": wins / len(group),
                "avg_pnl_pct": sum(t.pnl_pct for t in group) / len(group),
                "profit_factor": round(_profit_factor(pnl_series), 3),
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
