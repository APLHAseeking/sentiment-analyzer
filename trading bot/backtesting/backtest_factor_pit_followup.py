"""Phase 0 follow-up, steps 1-3: sleeve decomposition, an ex-low-vol
composite variant, and a financials-sector diagnostic cut — all against the
exact PIT inputs already built and cached for
docs/PHASE0_BACKTEST_2026-07-23.md (`pit_cache/backtest_inputs/*.csv`,
`pit_cache/ff_factors.csv`). No new data fetching; reuses
`backtest_factor_pit.py`'s cache paths, `REBALANCE_DATES`, `fetch_spy_returns`,
and `run_gate` rather than duplicating them — that file itself stays
untouched, it is Phase 0's frozen, already-reported record.

Pre-committed rule, identical to Phase 0/SUE/congressional: HAC t-stat > 2
AND IR > 0.5 on daily excess return over SPY, no sign flip. Applied
independently to a fixed research window (rebalance dates through
2024-06-30) and a held-out validation window (2024-09-30 through
2025-06-30) — the split (RESEARCH_CUTOFF below) was fixed before any of
these hypotheses were run. A hypothesis counts as a finding only if BOTH
windows clear the gate in the same direction; a research-only pass is
reported as "did not replicate," not softened. See
docs/PHASE0_FOLLOWUP_BACKTEST_<date>.md for the write-up.

The financials-sector cut (Step 3) is explicitly diagnostic-only — likely
well under 15 financials trades out of the composite's 58 total, too thin
to support a second formal gate — reported as descriptive context, never
gated, matching this repo's existing convention for attribution/regime
tables (docs/PHASE0_BACKTEST_2026-07-23.md's own beta diagnostic, and
backtest_sue_pit.py's regime_breakdown).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

import pandas as pd

from backtesting.attribution import attribute_returns, load_factor_returns
from backtesting.backtest_factor_pit import (
    _CONSTITUENTS_CSV, _FF_FACTORS_CACHE, _FUNDAMENTALS_CSV, _PRICES_CSV,
    REBALANCE_DATES, fetch_spy_returns, run_gate,
)
from backtesting.pit_data import CSVPITProvider
from backtesting.run_strategy_backtest import run_pit_backtest

log = logging.getLogger(__name__)

# Fixed before any of this session's new hypotheses were run — last 4 of the
# 16 quarterly rebalance dates (~9 months), operationalizing the task's
# "hold out the most recent 6-9 months" brief against the actual sample.
RESEARCH_CUTOFF = date(2024, 6, 30)  # last rebalance date IN the research window

SLEEVE_COLUMNS = [
    "value_score", "momentum_score", "quality_score", "low_vol_score", "reversal_score",
]

# Renormalizes the live _DEFAULT_WEIGHTS (screener/factor_scorer.py:384 —
# 0.25 value/momentum/quality, 0.10 low_vol, 0.15 reversal) over the 4
# sleeves excluding low_vol, proportional to their existing relative weights.
_EX_LOW_VOL_WEIGHTS = {
    "value_score": 0.25 / 0.90,
    "momentum_score": 0.25 / 0.90,
    "quality_score": 0.25 / 0.90,
    "reversal_score": 0.15 / 0.90,
}


def ex_low_vol_transform(scored_df: pd.DataFrame) -> pd.DataFrame:
    """score_transform for run_pit_backtest: a composite variant dropping the
    low_vol sleeve entirely, weights renormalized over the remaining 4.
    Tests whether the composite's low-beta tilt — this session's exploration
    confirmed both the low_vol sleeve's stock selection and vol-target
    position sizing contribute to the measured 0.24 realized beta — is
    suppressing the SPY-relative result. Leverage was considered and
    rejected as the operationalization: the harness has no margin-cost
    model, so a levered variant's IR would overstate what's live-achievable.
    """
    df = scored_df.copy()
    df["ex_low_vol_score"] = sum(
        df[col] * weight for col, weight in _EX_LOW_VOL_WEIGHTS.items()
    )
    return df


def split_windowed_gate(equity: pd.Series, benchmark_returns: pd.Series,
                         cutoff: date) -> dict:
    """Same HAC gate as backtest_factor_pit.py's run_gate, split at a fixed
    calendar cutoff (the pre-registered research/validation split) instead
    of stability_split's 50/50 row-count midpoint. Shares one boundary day
    between both windows — mirrors stability_split's own
    iloc[:mid+1]/iloc[mid:] overlap — so the validation window's first
    daily return isn't silently dropped by equity.pct_change()."""
    if equity.empty:
        empty = run_gate(equity, benchmark_returns)
        return {"research": empty, "validation": empty}
    cutoff_ts = pd.Timestamp(cutoff)
    idx = equity.index
    pos = idx.searchsorted(cutoff_ts, side="right") - 1
    pos = max(0, min(pos, len(equity) - 1))
    research = equity.iloc[:pos + 1]
    validation = equity.iloc[pos:]
    return {
        "research": run_gate(research, benchmark_returns),
        "validation": run_gate(validation, benchmark_returns),
    }


def run_variant(provider: CSVPITProvider, spy_prices: pd.Series, score_column: str,
                 score_transform=None) -> dict:
    """Run one composite/sleeve/variant across the full sample, then split
    the resulting equity curve at RESEARCH_CUTOFF for the gate — one
    simulation per variant, not two, mirroring how Phase 0's own
    stability_split works: each rebalance date only ever uses PIT data
    available as of that date, so slicing the post-hoc equity curve is
    methodologically equivalent to (and far cheaper than) re-running the
    simulation separately per window."""
    result = run_pit_backtest(
        provider=provider,
        rebalance_dates=REBALANCE_DATES,
        top_n=20,
        spy_prices=spy_prices,
        factor_csv_path=str(_FF_FACTORS_CACHE),
        score_column=score_column,
        score_transform=score_transform,
    )
    spy_rets = spy_prices.pct_change().dropna()
    gate = split_windowed_gate(result["equity_series"], spy_rets, RESEARCH_CUTOFF)
    return {"backtest": result, "gate": gate}


def financials_sector_cut(trades: list, signals: list[dict]) -> dict:
    """Diagnostic-only split of realized trade returns into Financial
    Services vs. everything else, using the sector each signal already
    carries (screener/simfin_fundamentals.py's SimFin-derived sector map).
    A ticker's sector is taken from its most recent signal (sector rarely
    changes within the sample; any drift is immaterial at this sample
    size). No gate — reported as descriptive context only, per this
    module's docstring."""
    sector_by_ticker: dict[str, str | None] = {}
    for sig in signals:
        sector_by_ticker[sig["ticker"]] = sig.get("sector")

    buckets: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        sector = sector_by_ticker.get(trade.ticker)
        bucket = "Financial Services" if sector == "Financial Services" else "Other"
        buckets[bucket].append(trade.pnl_pct)

    summary = {}
    for bucket, returns in buckets.items():
        series = pd.Series(returns)
        summary[bucket] = {
            "n_trades": len(series),
            "mean_pnl_pct": float(series.mean()) if len(series) else float("nan"),
            "median_pnl_pct": float(series.median()) if len(series) else float("nan"),
            "win_rate": float((series > 0).mean()) if len(series) else float("nan"),
        }
    return summary


def run() -> dict:
    """Run the composite (baseline, re-derived from the already-cached
    inputs for the financials cut) + all 5 sleeves + the ex-low-vol variant,
    each split at RESEARCH_CUTOFF. Returns a dict keyed by variant name."""
    provider = CSVPITProvider(
        constituents_path=str(_CONSTITUENTS_CSV),
        fundamentals_path=str(_FUNDAMENTALS_CSV),
        prices_path=str(_PRICES_CSV),
    )
    spy_prices = fetch_spy_returns()

    results: dict[str, dict] = {}

    composite = run_variant(provider, spy_prices, score_column="composite_score")
    results["composite"] = composite

    for sleeve in SLEEVE_COLUMNS:
        results[sleeve] = run_variant(provider, spy_prices, score_column=sleeve)

    results["ex_low_vol"] = run_variant(
        provider, spy_prices, score_column="ex_low_vol_score",
        score_transform=ex_low_vol_transform,
    )
    # Diagnostic-only: does the ex-low-vol variant's realized beta actually
    # move toward 1.0? Reused unchanged from Phase 0's own attribution call
    # — never substituted for the gate above.
    ff_factors = load_factor_returns(str(_FF_FACTORS_CACHE))
    ex_low_vol_rets = results["ex_low_vol"]["backtest"]["equity_series"].pct_change().dropna()
    if not ex_low_vol_rets.empty:
        results["ex_low_vol"]["attribution_diagnostic"] = attribute_returns(
            ex_low_vol_rets, ff_factors,
        )

    results["financials_cut_diagnostic"] = financials_sector_cut(
        composite["backtest"]["trades"], composite["backtest"]["signals"],
    )

    return results


def _fmt_gate(g: dict) -> str:
    return f"t={g['tstat']:.2f} IR={g['ir']:.2f} n={g['n_days']}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    output = run()
    for name in ["composite", *SLEEVE_COLUMNS, "ex_low_vol"]:
        gate = output[name]["gate"]
        print(f"{name:16s} research: {_fmt_gate(gate['research'])}"
              f"   validation: {_fmt_gate(gate['validation'])}")
    if "attribution_diagnostic" in output["ex_low_vol"]:
        attr = output["ex_low_vol"]["attribution_diagnostic"]
        print("ex_low_vol beta diagnostic:", attr["factors"].get("Mkt-RF"),
              "alpha_pct=", attr.get("alpha_pct"), "alpha_tstat=", attr.get("alpha_tstat"))
    print("Financials-sector cut (diagnostic):", output["financials_cut_diagnostic"])
