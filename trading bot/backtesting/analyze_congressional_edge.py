"""Empirical analysis of the congressional signal layer's marginal value.

This module answers: does adding congressional disclosure signals improve
risk-adjusted performance relative to the factor-only baseline, and at
which ``max_lag_days`` threshold does any edge concentrate?

Usage (offline demo with synthetic data)
-----------------------------------------
    python -m backtesting.analyze_congressional_edge

Or from a script / notebook::

    from backtesting.pit_data import CSVPITProvider
    from backtesting.analyze_congressional_edge import (
        run_congressional_edge_analysis,
        print_results,
    )

    provider = CSVPITProvider(...)
    results = run_congressional_edge_analysis(
        provider=provider,
        rebalance_dates=["2020-06-30", "2020-12-31"],
        congressional_signals=my_signals,   # list[dict] with lag_days key
        spy_prices=spy_series,
    )
    print_results(results)
"""
from __future__ import annotations

import logging
import textwrap
from datetime import date, timedelta

import numpy as np
import pandas as pd

from backtesting.pit_data import PITDataProvider, CSVPITProvider
from backtesting.run_strategy_backtest import run_pit_backtest
from backtesting.metrics import compute_all
from backtesting.simulation import equity_series, simulate_portfolio, trade_returns

log = logging.getLogger(__name__)

# Lag thresholds to evaluate (in calendar days from disclosure date to rebalance)
_LAG_VARIANTS: list[int] = [7, 14, 30, 45]

# Cost assumptions (must match run_pit_backtest defaults for a fair comparison)
_SLIPPAGE_BPS: float = 10.0
_COMMISSION_PCT: float = 0.05


# ── Core analysis function ────────────────────────────────────────────────────

def run_congressional_edge_analysis(
    provider: PITDataProvider,
    rebalance_dates: list[str],
    congressional_signals: list[dict] | None = None,
    spy_prices: pd.Series | None = None,
    top_n: int = 20,
    position_pct: float = 5.0,
    initial_cash: float = 100_000.0,
) -> dict[str, dict]:
    """Run factor-only baseline and four congressional lag variants, return metrics.

    Parameters
    ----------
    provider              : PITDataProvider (CSVPITProvider or any PIT-safe implementation)
    rebalance_dates       : ISO date strings at which to score and rebalance
    congressional_signals : list of dicts, each with keys:
                              ``date``         — ISO string when signal was *generated*
                              ``ticker``       — uppercase ticker
                              ``conviction``   — integer 0–100
                              ``position_pct`` — desired position size as % NAV
                              ``lag_days``     — calendar days between disclosure and
                                                 the date this signal was generated
                            If None / empty, all congressional variants will match the
                            factor-only baseline (expected result: no difference).
    spy_prices            : optional daily SPY price series for alpha/IR computation
    top_n                 : number of top factor-scored names per rebalance window
    position_pct          : fixed position size (% NAV) per factor signal
    initial_cash          : starting capital

    Returns
    -------
    dict[variant_name -> metrics_dict]
    Variant names: ``"factor_only"``, ``"congress_lag7"``, ``"congress_lag14"``,
    ``"congress_lag30"``, ``"congress_lag45"``.

    Each metrics dict contains: ``sharpe``, ``alpha_pct``, ``ir``,
    ``total_return_pct``, ``annualized_return_pct``, ``max_drawdown_pct``,
    ``n_signals``.
    """
    congressional_signals = congressional_signals or []

    # ── 1. Factor-only baseline ───────────────────────────────────────────────
    log.info("Running factor-only baseline …")
    baseline_result = run_pit_backtest(
        provider=provider,
        rebalance_dates=rebalance_dates,
        top_n=top_n,
        position_pct=position_pct,
        initial_cash=initial_cash,
        slippage_bps=_SLIPPAGE_BPS,
        commission_pct=_COMMISSION_PCT,
        spy_prices=spy_prices,
    )

    results: dict[str, dict] = {
        "factor_only": _extract_metrics(baseline_result, spy_prices),
    }

    # ── 2. Congressional variants ─────────────────────────────────────────────
    for max_lag in _LAG_VARIANTS:
        variant_name = f"congress_lag{max_lag}"
        log.info("Running variant %s (max_lag_days=%d) …", variant_name, max_lag)

        # Filter congressional signals to those within the lag threshold
        eligible = [
            s for s in congressional_signals
            if isinstance(s.get("lag_days"), (int, float)) and s["lag_days"] <= max_lag
        ]

        # Merge factor signals with eligible congressional signals
        merged_signals = _merge_signals(
            factor_signals=baseline_result["signals"],
            congressional_signals=eligible,
        )

        if not merged_signals:
            # Degenerate: no signals at all — re-use baseline metrics
            results[variant_name] = results["factor_only"].copy()
            results[variant_name]["n_signals"] = 0
            continue

        # Re-simulate with merged signals (shares the same price data pipeline)
        # We need price data for congressional tickers too
        all_prices = _collect_prices(
            provider=provider,
            signals=merged_signals,
            rebalance_dates=rebalance_dates,
        )

        sim = simulate_portfolio(
            signals=merged_signals,
            price_data=all_prices,
            initial_cash=initial_cash,
            slippage_bps=_SLIPPAGE_BPS,
            commission_pct=_COMMISSION_PCT,
        )
        eq = equity_series(sim)
        tr = trade_returns(sim)

        spy_rets: pd.Series | None = None
        if spy_prices is not None and not spy_prices.empty:
            spy_rets = spy_prices.pct_change().dropna()

        metrics = compute_all(
            eq, tr,
            trades=sim.trades,
            total_volume_traded=sim.total_volume_traded,
            benchmark_returns=spy_rets,
        )
        results[variant_name] = _normalise_metrics(metrics, len(merged_signals))

    return results


# ── Output formatter ──────────────────────────────────────────────────────────

def print_results(results: dict[str, dict]) -> None:
    """Print a formatted comparison table to stdout.

    Example output::

        Variant              Sharpe   Alpha%   IR     Return%   MaxDD%  Signals
        Factor only          0.82     n/a      n/a    12.30     8.10    60
        +Congress lag<=7     0.85     0.82     0.21   13.10     7.90    63
        +Congress lag<=14    0.84     0.71     0.19   12.80     8.20    65
        +Congress lag<=30    0.80    -0.14     0.07   11.90     8.50    71
        +Congress lag<=45    0.76    -0.31     0.03   11.20     9.10    78
    """
    display_map = {
        "factor_only":    "Factor only         ",
        "congress_lag7":  "+Congress lag<=7    ",
        "congress_lag14": "+Congress lag<=14   ",
        "congress_lag30": "+Congress lag<=30   ",
        "congress_lag45": "+Congress lag<=45   ",
    }
    header = f"{'Variant':<24} {'Sharpe':>7}  {'Alpha%':>7}  {'IR':>6}  {'Return%':>8}  {'MaxDD%':>7}  {'Signals':>8}"
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    order = ["factor_only", "congress_lag7", "congress_lag14", "congress_lag30", "congress_lag45"]
    for key in order:
        if key not in results:
            continue
        m = results[key]
        label = display_map.get(key, key)
        sharpe = f"{m.get('sharpe', 0.0):>7.3f}"
        alpha = (
            f"{m.get('alpha_pct', 0.0):>7.2f}"
            if m.get("alpha_pct") is not None
            else "    n/a"
        )
        ir = (
            f"{m.get('ir', 0.0):>6.3f}"
            if m.get("ir") is not None
            else "   n/a"
        )
        ret = f"{m.get('total_return_pct', 0.0):>8.2f}"
        mdd = f"{m.get('max_drawdown_pct', 0.0):>7.2f}"
        sigs = f"{m.get('n_signals', 0):>8d}"
        print(f"{label:<24} {sharpe}  {alpha}  {ir}  {ret}  {mdd}  {sigs}")

    print(sep)
    _print_recommendation(results)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_metrics(run_result: dict, spy_prices: pd.Series | None) -> dict:
    """Pull the fields we care about from a run_pit_backtest result dict."""
    m = run_result.get("metrics", {})
    return _normalise_metrics(m, run_result.get("n_signals", 0))


def _normalise_metrics(metrics: dict, n_signals: int) -> dict:
    """Flatten into a canonical dict with consistent key names."""
    return {
        "sharpe":              metrics.get("sharpe", 0.0),
        "alpha_pct":           metrics.get("alpha_annualized_pct"),       # None if no SPY
        "ir":                  metrics.get("information_ratio"),           # None if no SPY
        "total_return_pct":    metrics.get("total_return_pct", 0.0),
        "annualized_return_pct": metrics.get("annualized_return_pct", 0.0),
        "max_drawdown_pct":    metrics.get("max_drawdown_pct", 0.0),
        "n_signals":           n_signals,
    }


def _merge_signals(
    factor_signals: list[dict],
    congressional_signals: list[dict],
) -> list[dict]:
    """Merge factor and congressional signals.

    For tickers that appear in both on the same date, use the factor signal
    (already has position_pct set; congressional adds no incremental size to
    avoid double-counting). This is conservative and consistent with the live
    engine's ``"both"`` signal type which grants full conviction but no extra
    size cap relaxation.

    Congressional signals for tickers *not* in the factor top-N are appended
    as additional positions.
    """
    factor_keys: set[tuple[str, str]] = {
        (s["date"], s["ticker"]) for s in factor_signals
    }
    merged = list(factor_signals)
    for cs in congressional_signals:
        key = (cs.get("date", ""), cs.get("ticker", ""))
        if key not in factor_keys:
            merged.append(cs)
    return merged


def _collect_prices(
    provider: PITDataProvider,
    signals: list[dict],
    rebalance_dates: list[str],
) -> dict[str, pd.Series]:
    """Fetch price series for every ticker that appears in the merged signals."""
    sorted_dates = sorted(date.fromisoformat(d) for d in rebalance_dates)
    last_hold_end = (
        sorted_dates[-1] + timedelta(days=180) if sorted_dates else date.today()
    )

    prices: dict[str, pd.Series] = {}
    for sig in signals:
        ticker = sig.get("ticker", "")
        if not ticker or ticker in prices:
            continue
        sig_date = date.fromisoformat(sig["date"]) if isinstance(sig.get("date"), str) else date.today()
        series = provider.prices(ticker, sig_date - timedelta(days=5), last_hold_end)
        if not series.empty:
            prices[ticker] = series
    return prices


def _print_recommendation(results: dict[str, dict]) -> None:
    """Print a data-driven recommendation based on the computed metrics."""
    base = results.get("factor_only", {})
    base_sharpe = base.get("sharpe", 0.0)

    lines = ["\nRecommendation"]
    best_variant = None
    best_incremental_sharpe = 0.0

    for lag in _LAG_VARIANTS:
        key = f"congress_lag{lag}"
        if key not in results:
            continue
        v = results[key]
        inc_sharpe = v.get("sharpe", 0.0) - base_sharpe
        alpha = v.get("alpha_pct")
        ir = v.get("ir")

        if inc_sharpe > best_incremental_sharpe:
            best_incremental_sharpe = inc_sharpe
            best_variant = lag

        alpha_str = f"{alpha:.2f}%" if alpha is not None else "n/a"
        ir_str = f"{ir:.3f}" if ir is not None else "n/a"
        inc_str = f"{inc_sharpe:+.3f}"
        lines.append(
            f"  lag<={lag:2d}: incremental Sharpe={inc_str}  alpha={alpha_str}  IR={ir_str}"
        )

    if best_incremental_sharpe > 0.05:
        lines.append(
            f"\n  => Congressional layer adds edge at lag<={best_variant} days "
            f"(+{best_incremental_sharpe:.3f} Sharpe). "
            "Recommend keeping layer with max_lag_days set to that threshold."
        )
    else:
        lines.append(
            "\n  => No meaningful incremental Sharpe from congressional layer "
            f"(best: {best_incremental_sharpe:+.3f})."
        )
        lines.append(
            "     Consider dropping congressional layer or reducing max_lag_days to 7."
        )
    lines.append(
        "\n  NOTE: Thresholds above use synthetic data. Replace with real PIT data "
        "before drawing production conclusions."
    )
    print("\n".join(lines))


# ── Synthetic-data main ───────────────────────────────────────────────────────

def _build_synthetic_fixtures(tmp_dir: str) -> tuple[str, str, str]:
    """Build minimal CSV fixtures in ``tmp_dir``; mirrors test_pit_data.py pattern."""
    import os
    import textwrap

    os.makedirs(tmp_dir, exist_ok=True)

    constituents_csv = textwrap.dedent("""
        date,ticker
        2020-01-01,AAPL
        2020-01-01,MSFT
        2020-01-01,GOOG
        2020-01-01,AMZN
        2020-07-01,AAPL
        2020-07-01,MSFT
        2020-07-01,GOOG
        2020-07-01,AMZN
    """).strip()

    fundamentals_csv = textwrap.dedent("""
        date,ticker,trailingPE,priceToBook,freeCashflow,marketCap,returnOnEquity,profitMargins,debtToEquity,sector
        2020-01-15,AAPL,22.0,11.0,5.0e10,1.3e12,0.60,0.21,1.7,Technology
        2020-01-15,MSFT,30.0,12.0,4.0e10,1.4e12,0.38,0.34,0.6,Technology
        2020-01-15,GOOG,28.0,5.0,3.0e10,9.0e11,0.17,0.22,0.1,Technology
        2020-01-15,AMZN,80.0,18.0,2.0e10,1.6e12,0.25,0.06,1.1,Consumer Cyclical
        2020-07-15,AAPL,24.0,12.0,6.0e10,1.5e12,0.65,0.23,1.6,Technology
        2020-07-15,MSFT,32.0,13.0,4.5e10,1.6e12,0.40,0.36,0.5,Technology
        2020-07-15,GOOG,30.0,6.0,3.5e10,1.0e12,0.18,0.24,0.1,Technology
        2020-07-15,AMZN,85.0,20.0,2.5e10,1.8e12,0.27,0.07,1.0,Consumer Cyclical
    """).strip()

    dates = pd.bdate_range("2019-01-02", "2020-12-31")
    rng = np.random.default_rng(42)

    def _price_series(start: float, vol: float = 0.015) -> np.ndarray:
        rets = rng.normal(0.0003, vol, len(dates))
        return start * np.exp(np.cumsum(rets))

    price_df = pd.DataFrame({
        "date": dates.date,
        "AAPL": _price_series(150.0),
        "MSFT": _price_series(100.0),
        "GOOG": _price_series(1200.0),
        "AMZN": _price_series(2000.0),
    })

    c_path = os.path.join(tmp_dir, "constituents.csv")
    f_path = os.path.join(tmp_dir, "fundamentals.csv")
    p_path = os.path.join(tmp_dir, "prices.csv")

    with open(c_path, "w") as fh:
        fh.write(constituents_csv)
    with open(f_path, "w") as fh:
        fh.write(fundamentals_csv)
    price_df.to_csv(p_path, index=False)

    return c_path, f_path, p_path


def _build_synthetic_congressional_signals(rebalance_dates: list[str]) -> list[dict]:
    """Generate synthetic congressional signals at various lag_days values.

    In a real run these would come from the scraper / cached JSON.
    Here we manufacture plausible-looking signals spread across lag buckets.
    """
    signals = []
    tickers_by_lag = {
        5:  "AAPL",   # very fresh (within 7-day window)
        10: "MSFT",   # within 14-day window
        25: "GOOG",   # within 30-day window
        40: "AMZN",   # within 45-day window
    }
    for rebal_str in rebalance_dates:
        rebal = date.fromisoformat(rebal_str)
        for lag, ticker in tickers_by_lag.items():
            signals.append({
                "date":         rebal_str,
                "ticker":       ticker,
                "conviction":   65,
                "position_pct": 5.0,
                "lag_days":     lag,
            })
    return signals


def main() -> None:
    """Run the analysis on synthetic data and print results.

    This proves the harness works offline.  Replace with a real
    CSVPITProvider (and real ``congressional_signals``) when PIT data
    is available.
    """
    import tempfile

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    with tempfile.TemporaryDirectory() as tmp_dir:
        print("Building synthetic fixtures …")
        c_path, f_path, p_path = _build_synthetic_fixtures(tmp_dir)
        provider = CSVPITProvider(c_path, f_path, p_path)

        rebalance_dates = ["2020-02-01", "2020-08-01"]

        # Synthetic SPY — slightly below portfolio to give alpha signal headroom
        rng = np.random.default_rng(7)
        spy_dates = pd.bdate_range("2020-01-02", "2020-12-31")
        spy_prices = pd.Series(
            100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(spy_dates)))),
            index=spy_dates,
        )

        congressional_signals = _build_synthetic_congressional_signals(rebalance_dates)

        print(f"Rebalance dates : {rebalance_dates}")
        print(f"Congressional signals: {len(congressional_signals)} "
              f"(synthetic, spread across lag buckets 5/10/25/40 days)\n")

        results = run_congressional_edge_analysis(
            provider=provider,
            rebalance_dates=rebalance_dates,
            congressional_signals=congressional_signals,
            spy_prices=spy_prices,
            top_n=3,
            position_pct=5.0,
            initial_cash=100_000.0,
        )

        print_results(results)

        print(textwrap.dedent("""
            ─────────────────────────────────────────────────────────────────
            NOTE: All numbers above are from SYNTHETIC data.
            They demonstrate the harness works but have no predictive meaning.
            Replace CSVPITProvider fixtures and congressional_signals with
            real PIT data to obtain actionable findings.
            ─────────────────────────────────────────────────────────────────
        """))


if __name__ == "__main__":
    main()
