"""Inverse ETF hedge drag analysis.

Quantifies the volatility-decay and whipsaw drag of daily-rebalanced inverse
ETFs (SH/PSQ/RWM/…) in bear-regime periods versus a simple gross-reduction
alternative.  Self-contained: no yfinance calls, no broker keys, no DB.

Run:
    python backtesting/analyze_hedge_drag.py
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Core computations
# ---------------------------------------------------------------------------

def simulate_inverse_etf_decay(
    daily_returns: pd.Series,
    leverage: float = -1.0,
    periods: int = 252,
) -> dict:
    """Simulate an inverse ETF's NAV vs the theoretical leveraged return.

    Parameters
    ----------
    daily_returns:
        Daily returns of the *underlying* index (e.g. SPY).  Length should be
        >= ``periods``; only the first ``periods`` rows are used.
    leverage:
        Leverage factor applied each day.  For a standard inverse ETF use -1.0.
    periods:
        Number of trading days to simulate.

    Returns
    -------
    dict with keys:
        - ``final_nav``: simulated ETF NAV at end (starting from 1.0)
        - ``theoretical_nav``: what a static ``leverage``× exposure would give
        - ``decay``: final_nav - theoretical_nav  (negative = decay)
        - ``annualised_decay_pct``: decay as an annualised % drag
        - ``daily_vol``: realised daily vol of the underlying series
        - ``vol_drag_estimate``: T * σ² / 2 approximation of expected decay
    """
    rets = daily_returns.iloc[:periods].values

    # Simulate daily compounding of leveraged return
    etf_navs = np.cumprod(1.0 + leverage * rets)
    final_nav = float(etf_navs[-1])

    # Theoretical: apply leverage to the total underlying move
    total_underlying = float(np.prod(1.0 + rets))
    # For leverage != -1 this is total_underlying ** leverage
    theoretical_nav = float(total_underlying ** leverage)

    decay = final_nav - theoretical_nav

    # Annualise: decay relative to theoretical, scaled by 252/T
    annualised_decay_pct = (
        (decay / abs(theoretical_nav)) * (252 / max(len(rets), 1)) * 100
        if theoretical_nav != 0
        else float("nan")
    )

    daily_vol = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    T = len(rets)
    # Expected volatility drag formula: T * sigma^2 / 2  (per-period units)
    vol_drag_estimate = T * daily_vol ** 2 / 2.0

    return {
        "final_nav": final_nav,
        "theoretical_nav": theoretical_nav,
        "decay": decay,
        "annualised_decay_pct": annualised_decay_pct,
        "daily_vol": daily_vol,
        "vol_drag_estimate": vol_drag_estimate,
    }


def _max_drawdown(equity: pd.Series) -> float:
    """Return max drawdown as a negative fraction (e.g. -0.15 = -15%)."""
    roll_max = equity.cummax()
    drawdown = (equity - roll_max) / roll_max
    return float(drawdown.min())


def _sharpe(equity: pd.Series, risk_free: float = 0.04) -> float:
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return 0.0
    excess = rets - risk_free / 252
    vol = float(rets.std())
    if vol == 0.0:
        return 0.0
    return float(excess.mean() / vol * math.sqrt(252))


def compare_hedge_strategies(
    bear_equity_curve: pd.Series,
    underlying: pd.Series,
) -> dict:
    """Compare inverse-ETF hedging vs gross-position reduction over a bear period.

    Both strategies start from the same ``bear_equity_curve`` (the portfolio's
    equity curve during bear-regime days) and adjust it to reflect the two
    different hedging approaches.

    Parameters
    ----------
    bear_equity_curve:
        Portfolio equity curve *during bear periods only* (len N, starts at
        some base NAV).  Already reflects long-only positions; we overlay
        the two hedge approaches on top.
    underlying:
        Underlying index level (e.g. SPY price series) aligned with
        ``bear_equity_curve``.

    Returns
    -------
    dict with sub-dicts ``inverse_etf_approach`` and
    ``gross_reduction_approach``, each containing:
        - ``total_return_pct``
        - ``sharpe``
        - ``max_drawdown_pct``
        - ``annualised_return_pct``
    """
    if len(bear_equity_curve) < 2 or len(underlying) < 2:
        empty = {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0,
                 "annualised_return_pct": 0.0}
        return {"inverse_etf_approach": empty, "gross_reduction_approach": empty}

    n = min(len(bear_equity_curve), len(underlying))
    portfolio_rets = bear_equity_curve.iloc[:n].pct_change().fillna(0.0)
    index_rets = underlying.iloc[:n].pct_change().fillna(0.0)

    # --- Inverse-ETF approach ---
    # Assume 25% NAV in SH (the broad inverse ETF) throughout the bear period.
    # SH daily return = -1 × SPY daily return (with daily compounding, no decay
    # modelled here beyond what naturally falls out of the SH simulation).
    sh_alloc = 0.25
    sh_rets = -1.0 * index_rets  # approximate SH daily return
    combined_rets_etf = (1.0 - sh_alloc) * portfolio_rets + sh_alloc * sh_rets
    equity_etf = (1.0 + combined_rets_etf).cumprod()

    # --- Gross-reduction approach ---
    # Reduce long exposure by 50%; the freed-up capital sits in cash (0%).
    gross_alloc = 0.50
    combined_rets_gross = gross_alloc * portfolio_rets  # 50% long, 50% cash
    equity_gross = (1.0 + combined_rets_gross).cumprod()

    def _metrics(equity: pd.Series) -> dict:
        tr = float(equity.iloc[-1] / equity.iloc[0] - 1) * 100
        mdd = _max_drawdown(equity) * 100
        sh = _sharpe(equity)
        T = len(equity)
        ann = float((equity.iloc[-1] / equity.iloc[0]) ** (252 / T) - 1) * 100
        return {
            "total_return_pct": round(tr, 3),
            "sharpe": round(sh, 3),
            "max_drawdown_pct": round(mdd, 3),
            "annualised_return_pct": round(ann, 3),
        }

    return {
        "inverse_etf_approach": _metrics(equity_etf),
        "gross_reduction_approach": _metrics(equity_gross),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_analysis(results: dict) -> None:
    """Print a formatted table from the results dict produced by ``main()``."""
    print()
    print("=" * 62)
    print("  INVERSE ETF DECAY ANALYSIS")
    print("=" * 62)

    decay_info = results.get("decay_simulation", {})
    print(f"\n  Simulation period (trading days) : {results.get('sim_periods', '?')}")
    print(f"  Underlying daily vol (annualised): "
          f"{decay_info.get('daily_vol', 0.0) * (252 ** 0.5) * 100:.1f}%")
    print(f"  ETF final NAV                    : {decay_info.get('final_nav', 0.0):.4f}")
    print(f"  Theoretical NAV (−1× static)     : {decay_info.get('theoretical_nav', 0.0):.4f}")
    print(f"  Decay (NAV gap)                  : {decay_info.get('decay', 0.0):+.4f}")
    print(f"  Annualised decay drag            : {decay_info.get('annualised_decay_pct', 0.0):.2f}%")
    print(f"  Vol-drag estimate (T·σ²/2)        : {decay_info.get('vol_drag_estimate', 0.0):.4f}")

    print()
    print("-" * 62)
    print("  BEAR-PERIOD HEDGE COMPARISON")
    print("-" * 62)
    cmp = results.get("strategy_comparison", {})
    etf = cmp.get("inverse_etf_approach", {})
    gross = cmp.get("gross_reduction_approach", {})

    header = f"  {'Metric':<32}  {'Inverse ETF':>10}  {'Gross -50%':>10}"
    print(header)
    print("  " + "-" * 58)
    metrics = [
        ("Total return (%)", "total_return_pct"),
        ("Annualised return (%)", "annualised_return_pct"),
        ("Sharpe ratio", "sharpe"),
        ("Max drawdown (%)", "max_drawdown_pct"),
    ]
    for label, key in metrics:
        ev = etf.get(key, float("nan"))
        gv = gross.get(key, float("nan"))
        print(f"  {label:<32}  {ev:>10.2f}  {gv:>10.2f}")

    print()
    diff_tr = gross.get("total_return_pct", 0.0) - etf.get("total_return_pct", 0.0)
    diff_ann = gross.get("annualised_return_pct", 0.0) - etf.get("annualised_return_pct", 0.0)
    print(f"  Gross-reduction advantage  : {diff_tr:+.2f}% total  |  "
          f"{diff_ann:+.2f}% ann.")
    print()
    if diff_ann > 1.0:
        verdict = "GROSS REDUCTION wins by >1% ann. — recommend evaluating as replacement."
    elif diff_ann < -1.0:
        verdict = "INVERSE ETF wins by >1% ann. — current approach justified."
    else:
        verdict = "Approaches within 1% ann. — further real-data testing required."
    print(f"  Verdict: {verdict}")
    print()
    print("=" * 62)
    print("  NOTE: Synthetic data only. Run with real SPY history to")
    print("  populate docs/HEDGE_ANALYSIS.md with actionable numbers.")
    print("=" * 62)
    print()


# ---------------------------------------------------------------------------
# Main: synthetic walk
# ---------------------------------------------------------------------------

def main() -> dict:
    """Run analysis on synthetic data and print results.

    Generates a random-walk SPY series (GBM), identifies 'bear' periods as
    drawdown > 15%, then runs both analysis functions.
    """
    rng = np.random.default_rng(seed=42)
    trading_days = 252 * 3  # 3 years of synthetic history

    # Geometric Brownian Motion: mu=7% ann, sigma=18% ann
    mu = 0.07 / 252
    sigma = 0.18 / math.sqrt(252)
    daily_rets = rng.normal(mu, sigma, size=trading_days)
    spy_prices = pd.Series(
        np.cumprod(1.0 + daily_rets) * 100.0,
        name="SPY",
    )
    spy_rets = pd.Series(daily_rets, name="SPY_ret")

    # 1. Decay simulation
    decay_info = simulate_inverse_etf_decay(spy_rets, leverage=-1.0, periods=252)

    # 2. Identify bear periods (rolling drawdown from peak > 15%)
    roll_max = spy_prices.cummax()
    drawdown = (spy_prices - roll_max) / roll_max
    bear_mask = drawdown < -0.15

    if bear_mask.sum() < 5:
        # If no deep bear in synthetic sample, use worst 20% of days
        threshold = np.percentile(daily_rets, 20)
        bear_mask = pd.Series(daily_rets < threshold, name="bear")

    bear_idx = bear_mask[bear_mask].index
    bear_prices = spy_prices.iloc[bear_idx]
    bear_rets_raw = spy_rets.iloc[bear_idx]

    # Build a synthetic portfolio equity curve during bear: slightly worse than SPY
    portfolio_rets_bear = bear_rets_raw * 1.1  # 10% more volatile long book
    portfolio_equity = (1.0 + portfolio_rets_bear).cumprod() * 100.0

    # 3. Compare strategies
    strategy_cmp = compare_hedge_strategies(portfolio_equity, bear_prices)

    results = {
        "sim_periods": 252,
        "bear_days": int(bear_mask.sum()),
        "decay_simulation": decay_info,
        "strategy_comparison": strategy_cmp,
    }
    print_analysis(results)
    return results


if __name__ == "__main__":
    main()
