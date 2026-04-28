"""Performance metrics for backtesting.

All functions operate on a pandas Series of daily returns (or a DataFrame
of trade-level records). No yfinance calls — purely computational.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def total_return(equity: pd.Series) -> float:
    """Total return from start to end of equity curve."""
    if len(equity) < 2:
        return 0.0
    return float(equity.iloc[-1] / equity.iloc[0] - 1)


def annualized_return(equity: pd.Series, trading_days: int = 252) -> float:
    n = len(equity)
    if n < 2:
        return 0.0
    tr = total_return(equity)
    return float((1 + tr) ** (trading_days / n) - 1)


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def annualized_volatility(equity: pd.Series, trading_days: int = 252) -> float:
    rets = daily_returns(equity)
    if len(rets) < 2:
        return 0.0
    return float(rets.std() * np.sqrt(trading_days))


def sharpe_ratio(equity: pd.Series, risk_free: float = 0.04, trading_days: int = 252) -> float:
    rets = daily_returns(equity)
    if len(rets) < 2:
        return 0.0
    excess = rets - risk_free / trading_days
    vol = rets.std()
    if vol == 0:
        return 0.0
    return float(excess.mean() / vol * np.sqrt(trading_days))


def sortino_ratio(equity: pd.Series, risk_free: float = 0.04, trading_days: int = 252) -> float:
    rets = daily_returns(equity)
    if len(rets) < 2:
        return 0.0
    excess = rets - risk_free / trading_days
    downside = rets[rets < 0].std()
    if downside == 0:
        return 0.0
    return float(excess.mean() / downside * np.sqrt(trading_days))


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough drawdown as a positive fraction (0.20 = 20% drawdown)."""
    if len(equity) < 2:
        return 0.0
    rolling_max = equity.expanding().max()
    drawdowns = equity / rolling_max - 1
    return float(abs(drawdowns.min()))


def drawdown_series(equity: pd.Series) -> pd.Series:
    rolling_max = equity.expanding().max()
    return equity / rolling_max - 1


def calmar_ratio(equity: pd.Series, trading_days: int = 252) -> float:
    ann_ret = annualized_return(equity, trading_days)
    mdd = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return float(ann_ret / mdd)


def win_rate(trade_returns: pd.Series) -> float:
    if len(trade_returns) == 0:
        return 0.0
    return float((trade_returns > 0).mean())


def profit_factor(trade_returns: pd.Series) -> float:
    wins = trade_returns[trade_returns > 0].sum()
    losses = abs(trade_returns[trade_returns < 0].sum())
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def avg_trade_return(trade_returns: pd.Series) -> float:
    if len(trade_returns) == 0:
        return 0.0
    return float(trade_returns.mean())


def compute_all(equity: pd.Series, trade_returns: pd.Series | None = None) -> dict:
    """Compute the full metrics suite."""
    tr = trade_returns if trade_returns is not None else pd.Series(dtype=float)
    return {
        "total_return_pct": round(total_return(equity) * 100, 2),
        "annualized_return_pct": round(annualized_return(equity) * 100, 2),
        "annualized_vol_pct": round(annualized_volatility(equity) * 100, 2),
        "sharpe": round(sharpe_ratio(equity), 3),
        "sortino": round(sortino_ratio(equity), 3),
        "calmar": round(calmar_ratio(equity), 3),
        "max_drawdown_pct": round(max_drawdown(equity) * 100, 2),
        "win_rate": round(win_rate(tr), 3),
        "profit_factor": round(profit_factor(tr), 3),
        "avg_trade_return_pct": round(avg_trade_return(tr) * 100, 3),
        "n_trades": len(tr),
    }
