"""Point-in-time backtest for the two new price-based factor sleeves:
low-volatility / betting-against-beta and residual (idiosyncratic) momentum.

These factors depend only on historical prices, so — unlike the fundamentals-based
sleeves (which read *current* yfinance .info and are not historically
reconstructable) — they can be backtested honestly here. Each rebalance ranks the
universe using only data available before that date, forms an equal-weight long
portfolio of the strongest names, and holds to the next monthly rebalance.

Caveats (see docs/PHASE0_FINDINGS.md): the universe below is a fixed current list,
so results carry survivorship bias and are indicative, not a clean OOS test.

Run:  python backtesting/backtest_price_factors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtesting.metrics import compute_all, daily_returns  # noqa: E402

# Representative, sector-diversified large/mid-cap set (fixed -> survivorship caveat).
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMD", "INTC", "CSCO", "ORCL", "IBM", "ADBE", "CRM",
    "GOOGL", "META", "NFLX", "T", "VZ", "CMCSA", "DIS",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK", "SCHW",
    "JNJ", "PFE", "MRK", "ABBV", "LLY", "UNH", "CVS", "MDT", "TMO", "ABT",
    "PG", "KO", "PEP", "WMT", "COST", "MCD", "NKE", "SBUX", "TGT", "HD", "LOW",
    "XOM", "CVX", "COP", "SLB", "EOG",
    "BA", "CAT", "GE", "HON", "UPS", "RTX", "DE", "MMM", "LMT",
    "NEE", "DUK", "SO", "D", "AEP",
    "LIN", "SHW", "FCX", "NEM",
    "AMT", "PLD", "SPG",
]
BENCHMARK = "SPY"
START = "2018-01-01"
LOOKBACK = 252       # ~12 months
SKIP = 21            # skip last ~1 month for momentum (avoid short-term reversal)
TOP_QUANTILE = 0.30  # long the strongest 30% each rebalance


def _download() -> pd.DataFrame:
    tickers = UNIVERSE + [BENCHMARK]
    raw = yf.download(tickers, start=START, auto_adjust=True, progress=False)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    return close.dropna(how="all")


def _rebalance_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """First trading day of each month with enough history behind it."""
    months = pd.Series(index, index=index).groupby([index.year, index.month]).first()
    return [d for d in months if index.get_loc(d) >= LOOKBACK]


def _factor_scores(close: pd.DataFrame, spy: pd.Series, asof_loc: int) -> pd.DataFrame:
    """Compute vol / beta / resid_mom for each ticker using only data before asof_loc."""
    window = close.iloc[asof_loc - LOOKBACK:asof_loc]
    spy_win = spy.iloc[asof_loc - LOOKBACK:asof_loc]
    spy_ret = spy_win.pct_change().dropna()
    spy_var = spy_ret.var()
    rows = {}
    for t in close.columns:
        col = window[t].dropna()
        if len(col) < LOOKBACK - 10:
            continue
        ret = col.pct_change().dropna()
        vol = ret.std() * np.sqrt(252)
        aligned = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
        aligned.columns = ["s", "m"]
        if len(aligned) < 60 or spy_var <= 0:
            continue
        beta = aligned["s"].cov(aligned["m"]) / aligned["m"].var()
        resid = aligned["s"] - beta * aligned["m"]
        resid_win = resid.iloc[:max(0, len(resid) - SKIP)]
        resid_mom = (1 + resid_win).prod() - 1 if len(resid_win) else np.nan
        rows[t] = {"vol": vol, "beta": beta, "resid_mom": resid_mom}
    return pd.DataFrame(rows).T


def _run_strategy(close, spy, rebal, picker) -> pd.Series:
    """Build a daily equity curve for a long-only equal-weight monthly strategy."""
    index = close.index
    equity = pd.Series(1.0, index=index)
    nav = 1.0
    for i, d in enumerate(rebal):
        loc = index.get_loc(d)
        scores = _factor_scores(close, spy, loc)
        picks = picker(scores)
        end_loc = index.get_loc(rebal[i + 1]) if i + 1 < len(rebal) else len(index) - 1
        if not picks:
            equity.iloc[loc:end_loc + 1] = nav
            continue
        held = close[picks].iloc[loc:end_loc + 1]
        port = held.pct_change().fillna(0).mean(axis=1)  # equal-weight daily return
        curve = nav * (1 + port).cumprod()
        equity.iloc[loc:end_loc + 1] = curve.values
        nav = float(curve.iloc[-1])
    return equity.loc[rebal[0]:]


def _pick_low_vol(scores: pd.DataFrame) -> list[str]:
    s = scores.dropna(subset=["vol"])
    if s.empty:
        return []
    cut = s["vol"].quantile(TOP_QUANTILE)  # lowest-vol names
    return s[s["vol"] <= cut].index.tolist()


def _pick_resid_mom(scores: pd.DataFrame) -> list[str]:
    s = scores.dropna(subset=["resid_mom"])
    if s.empty:
        return []
    cut = s["resid_mom"].quantile(1 - TOP_QUANTILE)  # highest residual momentum
    return s[s["resid_mom"] >= cut].index.tolist()


def _pick_all(scores: pd.DataFrame) -> list[str]:
    return scores.index.tolist()


def main() -> None:
    print(f"Downloading {len(UNIVERSE)} tickers + {BENCHMARK} since {START} ...")
    close = _download()
    spy = close[BENCHMARK]
    universe_close = close[[c for c in close.columns if c != BENCHMARK]]
    rebal = _rebalance_dates(close.index)
    print(f"{len(close)} trading days, {len(rebal)} monthly rebalances "
          f"({rebal[0].date()} -> {rebal[-1].date()})\n")

    strategies = {
        "Low-Vol / BAB": _pick_low_vol,
        "Residual Momentum": _pick_resid_mom,
        "Equal-Weight (baseline)": _pick_all,
    }
    bench_eq = spy.loc[rebal[0]:]
    bench_rets = daily_returns(bench_eq)
    bench_metrics = compute_all(bench_eq / bench_eq.iloc[0])

    header = f"{'Strategy':<26}{'TotRet%':>9}{'AnnRet%':>9}{'Vol%':>7}{'Sharpe':>8}{'Sortino':>8}{'MaxDD%':>8}{'Alpha%':>8}{'Beta':>7}"
    print(header)
    print("-" * len(header))
    for name, picker in strategies.items():
        eq = _run_strategy(universe_close, spy, rebal, picker)
        eq = eq / eq.iloc[0]
        m = compute_all(eq, benchmark_returns=bench_rets)
        print(f"{name:<26}{m['total_return_pct']:>9}{m['annualized_return_pct']:>9}"
              f"{m['annualized_vol_pct']:>7}{m['sharpe']:>8}{m['sortino']:>8}"
              f"{m['max_drawdown_pct']:>8}{m.get('alpha_annualized_pct', 0):>8}{m.get('beta', 0):>7}")
    bm = bench_metrics
    print(f"{'SPY (buy & hold)':<26}{bm['total_return_pct']:>9}{bm['annualized_return_pct']:>9}"
          f"{bm['annualized_vol_pct']:>7}{bm['sharpe']:>8}{bm['sortino']:>8}{bm['max_drawdown_pct']:>8}"
          f"{'-':>8}{'-':>7}")
    print("\nAlpha/Beta are vs SPY daily returns. Survivorship-biased fixed universe — indicative only.")


if __name__ == "__main__":
    main()
