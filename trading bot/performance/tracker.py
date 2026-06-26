"""Live performance tracker — same metrics as backtesting, from live trading.db."""
from __future__ import annotations

import pandas as pd
import bot.db as db
from backtesting.metrics import compute_all


class PerformanceTracker:
    """Compute P&L metrics from trading.db for the live portfolio.

    Returns dicts with the same keys as backtesting.metrics.compute_all so
    live and backtest results can be compared directly.
    """

    def equity_series(self) -> pd.Series:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT date, total_nav FROM portfolio_log ORDER BY date ASC"
            ).fetchall()
        if not rows:
            return pd.Series(dtype=float)
        dates = [r["date"] for r in rows]
        navs = [float(r["total_nav"]) for r in rows]
        return pd.Series(navs, index=pd.to_datetime(dates), name="equity")

    def trade_returns(self) -> pd.Series:
        closed = db.get_closed_positions()
        if not closed:
            return pd.Series(dtype=float)
        # Use realized_pnl (net of both-side commissions) divided by entry cost so
        # the return is commission-inclusive, matching backtesting/simulation.py.
        # realized_pnl is stored as: (exit_price * shares - exit_commission)
        #                           - (entry_price * shares + entry_commission)
        rets = [
            float(r["realized_pnl"]) / (float(r["entry_price"]) * float(r["shares"]))
            for r in closed
            if float(r["entry_price"]) > 0 and float(r["shares"]) > 0
        ]
        return pd.Series(rets, name="trade_return")

    def summary(self) -> dict:
        """Full metrics dict. Returns {"error": ...} if no portfolio history."""
        eq = self.equity_series()
        tr = self.trade_returns()
        if eq.empty:
            return {"error": "No portfolio history yet — run the bot for at least one day"}
        return compute_all(eq, tr)

    def by_regime(self) -> dict[str, dict]:
        """Per-regime trade attribution.

        Returns {regime_label: {n_trades, avg_return_pct, win_rate}}.
        Joins closed_positions with the most-recent regime_log entry for each date
        (regime_log can have multiple entries per date; DISTINCT prevents duplicates).
        """
        with db.get_conn() as conn:
            rows = conn.execute(
                """SELECT cp.entry_price, cp.exit_price, cp.shares,
                          COALESCE(rl.regime_label, 'unknown') AS regime_label
                   FROM closed_positions cp
                   LEFT JOIN (
                       SELECT date, regime_label
                       FROM regime_log
                       GROUP BY date
                       HAVING id = MAX(id)
                   ) rl ON cp.entry_date = rl.date"""
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for r in rows:
            entry = float(r["entry_price"])
            if entry <= 0:
                continue
            ret = (float(r["exit_price"]) - entry) / entry
            grouped.setdefault(r["regime_label"], []).append(ret)
        return {
            label: {
                "n_trades": len(rets),
                "avg_return_pct": round(sum(rets) / len(rets) * 100, 2),
                "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 3),
            }
            for label, rets in grouped.items()
        }
