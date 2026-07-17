from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import bot.db as db
from performance.tracker import PerformanceTracker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerformanceReport:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_realized_pnl: float
    avg_hold_days: float
    best_trade_pnl: float
    worst_trade_pnl: float
    report_date: str


def compute_performance_report() -> PerformanceReport:
    rows = db.get_closed_positions()
    if not rows:
        return PerformanceReport(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            total_realized_pnl=0.0, avg_hold_days=0.0,
            best_trade_pnl=0.0, worst_trade_pnl=0.0,
            report_date=date.today().isoformat(),
        )

    pnls = [float(r["realized_pnl"]) for r in rows]
    hold_days = []
    for r in rows:
        try:
            entry = date.fromisoformat(r["entry_date"])
            exit_ = date.fromisoformat(r["exit_date"])
            hold_days.append((exit_ - entry).days)
        except (ValueError, TypeError):
            pass

    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)  # break-even counted as loss
    return PerformanceReport(
        total_trades=len(pnls),
        wins=wins,
        losses=losses,
        win_rate=wins / len(pnls),
        total_realized_pnl=sum(pnls),
        avg_hold_days=sum(hold_days) / len(hold_days) if hold_days else 0.0,
        best_trade_pnl=max(pnls),
        worst_trade_pnl=min(pnls),
        report_date=date.today().isoformat(),
    )


def log_weekly_report() -> None:
    report = compute_performance_report()
    log.info(
        "=== WEEKLY PERFORMANCE REPORT (%s) ===\n"
        "Closed trades: %d | Wins: %d | Losses: %d | Win rate: %.1f%%\n"
        "Total realized P&L: $%.2f\n"
        "Avg hold period: %.1f days\n"
        "Best trade: $%.2f | Worst: $%.2f",
        report.report_date,
        report.total_trades, report.wins, report.losses, report.win_rate * 100,
        report.total_realized_pnl,
        report.avg_hold_days,
        report.best_trade_pnl, report.worst_trade_pnl,
    )
    by_source = db.get_performance_by_source()
    for source, pnls in sorted(by_source.items()):
        wins = sum(1 for p in pnls if p > 0)
        log.info(
            "  [%s] %d trades | %d wins | $%.2f P&L",
            source, len(pnls), wins, sum(pnls),
        )

    tracker = PerformanceTracker()
    summary = tracker.summary()
    if "error" in summary:
        log.info(
            "=== LIVE VS BACKTEST COMPARISON ===\n"
            "not enough portfolio history yet for live-vs-backtest comparison (%s)",
            summary["error"],
        )
    else:
        log.info(
            "=== LIVE VS BACKTEST COMPARISON ===\n"
            "Total return: %.2f%% | Annualized: %.2f%% | Vol: %.2f%%\n"
            "Sharpe: %.3f | Sortino: %.3f | Calmar: %.3f\n"
            "Max drawdown: %.2f%% | Win rate: %.1f%% | Profit factor: %.3f\n"
            "Trades: %d | Avg trade return: %.3f%%",
            summary["total_return_pct"], summary["annualized_return_pct"], summary["annualized_vol_pct"],
            summary["sharpe"], summary["sortino"], summary["calmar"],
            summary["max_drawdown_pct"], summary["win_rate"] * 100, summary["profit_factor"],
            summary["n_trades"], summary["avg_trade_return_pct"],
        )

    by_regime = tracker.by_regime()
    for regime_label, stats in sorted(by_regime.items()):
        log.info(
            "  [regime: %s] %d trades | avg return: %.2f%% | win rate: %.1f%%",
            regime_label, stats["n_trades"], stats["avg_return_pct"], stats["win_rate"] * 100,
        )
