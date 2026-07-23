"""Phase 0 follow-up, step 4d: PIT backtest of the insider (Form 4) signal
against the real historical transactions fetched in step 4c
(`pit_cache/insider_transactions_full.parquet`, built by
`screener/insider_pit_history.py::run_full_fetch`). Mirrors
`backtest_sue_pit.py`'s event-study discipline — PIT tradable-date lag,
PIT-universe restriction (`restrict_to_pit_universe`, reused unmodified),
a naive-vs-PIT honesty check — but benchmarks against SPY via the same
HAC gate as `backtest_factor_pit.py`/`backtest_factor_pit_followup.py`,
for consistency across this whole follow-up report, rather than SUE's own
"top-quintile vs all quarterly-earnings-events" contemporaneous benchmark
(which has no natural analog here: every qualifying insider buy already
passes the same live filter — `bot/insider_signal.py::is_qualified_insider`
— there is no natural low-conviction bucket to split against).

Pre-committed rule, identical to every other hypothesis in this follow-up
session: HAC t-stat > 2 AND IR > 0.5 on daily excess return over SPY,
independently on the research window (rebalance/event dates through
2024-06-30) and the held-out validation window (2024-09-30 through
2025-06-30), no sign flip between them. Lag-bucket decomposition
(mirrors docs/CONGRESSIONAL_EDGE.md's method: <=7/<=14/<=30/<=45 days) is
reported as diagnostic context only, not a second gate — the primary,
pre-committed test is the full qualified population (bot/insider_signal.py's
exact live filters: $50k floor, <=45-day lag) at 20d and 60d horizons.

screener/factor_scorer.py and bot/insider_signal.py are untouched by this
work — recommendation-only, same commitment every prior backtest in this
repo has kept.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from backtesting.backtest_factor_pit import fetch_spy_returns, run_gate as spy_run_gate
from backtesting.backtest_factor_pit_followup import RESEARCH_CUTOFF, split_windowed_gate
from backtesting.backtest_sue_pit import restrict_to_pit_universe
from market_data.pit_prices import fetch_pit_prices

log = logging.getLogger(__name__)

_TRANSACTIONS_PATH = Path("pit_cache/insider_transactions_full.parquet")
_PRICES_CACHE_DIR = Path("pit_cache/prices")

SAMPLE_START = date(2021, 9, 1)
SAMPLE_END = date(2025, 6, 30)

# Live qualification filters, mirrored exactly from
# bot/insider_signal.py::is_qualified_insider — the backtest must test the
# same population the live signal actually uses.
_MIN_TRADE_USD = 50_000.0
_MAX_LAG_DAYS = 45

HORIZONS = (20, 60)
_LAG_BUCKETS = (7, 14, 30, 45)  # diagnostic only, mirrors CONGRESSIONAL_EDGE.md


def load_qualified_events(transactions_path: Path | None = None) -> pd.DataFrame:
    """Load the fetched raw transactions and apply the exact live
    qualification filters (dollar floor, lag window) — everything else
    (universe restriction, PIT lag) is applied downstream, same separation
    as build_fundamentals_csv keeping raw ratios vs. scoring being a later
    step. transactions_path resolves _TRANSACTIONS_PATH at CALL time, not
    as a bound default — a bound default captures the module constant's
    value at function-definition time, which silently ignores a test's (or
    caller's) later `patch("...backtest_insider_pit._TRANSACTIONS_PATH",
    ...)` (caught live by this module's own test suite)."""
    if transactions_path is None:
        transactions_path = _TRANSACTIONS_PATH
    raw = pd.read_parquet(transactions_path)
    if raw.empty:
        return raw
    lag_days = (
        pd.to_datetime(raw["disclosure_date"]) - pd.to_datetime(raw["transaction_date"])
    ).dt.days
    qualified = raw[
        (raw["amount_usd"] >= _MIN_TRADE_USD) & (lag_days >= 0) & (lag_days <= _MAX_LAG_DAYS)
    ].copy()
    qualified["lag_days"] = lag_days[qualified.index]
    return qualified.reset_index(drop=True)


def add_tradable_date(events: pd.DataFrame) -> pd.DataFrame:
    """Tradable date = the first NYSE session strictly after
    `disclosure_date` (the SEC filing date) — same PIT-lag convention
    backtest_sue_pit.py::add_tradable_date uses for filed_date, just
    against this module's own disclosure_date column name."""
    nyse = xcals.get_calendar("XNYS")
    sessions = nyse.sessions_in_range(str(SAMPLE_START), str(SAMPLE_END + timedelta(days=14)))
    sessions = pd.DatetimeIndex(sessions).normalize()

    def _next_session(d):
        after = sessions[sessions > pd.Timestamp(d)]
        return after[0].date() if len(after) else None

    events = events.copy()
    events["tradable_date"] = pd.to_datetime(events["disclosure_date"]).dt.date.apply(_next_session)
    return events.dropna(subset=["tradable_date"])


def build_pit_events() -> pd.DataFrame:
    """Qualified insider buys -> PIT tradable_date -> restricted to the S&P
    500 PIT universe as of tradable_date (restrict_to_pit_universe, reused
    unmodified from backtest_sue_pit.py — it only touches `ticker`/
    `tradable_date`, no SUE-specific coupling)."""
    events = load_qualified_events()
    if events.empty:
        return events
    events = add_tradable_date(events)
    return restrict_to_pit_universe(events)


def daily_holding_returns(events: pd.DataFrame, prices: pd.DataFrame, horizon: int) -> pd.Series:
    """Equal-weighted daily return of whatever qualifying insider-buy
    positions are currently within their [tradable_date+1, tradable_date+
    horizon] holding window — the calendar-time construction
    backtest_sue_pit.py::daily_calendar_excess_returns uses for its
    top-quintile leg, generalized here to the WHOLE qualified population
    (no top/bottom split — every row already passed the same live filter,
    see this module's docstring for why)."""
    daily_returns = prices.pct_change(fill_method=None)
    rows: list[pd.Series] = []
    for _, row in events.iterrows():
        ticker = row["ticker"]
        if ticker not in daily_returns.columns:
            continue
        series = daily_returns[ticker]
        entry = series.index[series.index >= pd.Timestamp(row["tradable_date"])]
        if entry.empty:
            continue
        entry_idx = series.index.get_loc(entry[0])
        exit_idx = entry_idx + horizon
        if exit_idx >= len(series):
            continue
        rows.append(series.iloc[entry_idx + 1: exit_idx + 1])
    if not rows:
        return pd.Series(dtype=float)
    return pd.concat(rows).groupby(level=0).mean().sort_index()


def run_gate_for_horizon(events: pd.DataFrame, prices: pd.DataFrame,
                          spy_prices: pd.Series, horizon: int) -> dict:
    """Same split_windowed_gate (research/validation, pre-registered cutoff)
    every other hypothesis in this follow-up uses, applied to this
    horizon's daily holding-return series converted to a synthetic equity
    curve so backtest_factor_pit.py::run_gate can be reused unchanged."""
    daily = daily_holding_returns(events, prices, horizon)
    if daily.empty:
        empty_gate = spy_run_gate(pd.Series(dtype=float), pd.Series(dtype=float))
        return {"research": empty_gate, "validation": empty_gate}
    equity = (1 + daily).cumprod() * 100_000.0
    spy_rets = spy_prices.pct_change().dropna()
    return split_windowed_gate(equity, spy_rets, RESEARCH_CUTOFF)


def lag_bucket_diagnostic(events: pd.DataFrame, prices: pd.DataFrame,
                           spy_prices: pd.Series, horizon: int) -> dict:
    """Diagnostic-only lag-bucket decomposition (<=7/<=14/<=30/<=45 days),
    mirroring docs/CONGRESSIONAL_EDGE.md's method — reported as context,
    never a second gate (this follow-up's pre-committed test is the full
    population above)."""
    out = {}
    for max_lag in _LAG_BUCKETS:
        bucket = events[events["lag_days"] <= max_lag]
        daily = daily_holding_returns(bucket, prices, horizon)
        if daily.empty:
            out[max_lag] = {"n_events": len(bucket), "gate": None}
            continue
        equity = (1 + daily).cumprod() * 100_000.0
        spy_rets = spy_prices.pct_change().dropna()
        out[max_lag] = {"n_events": len(bucket), "gate": spy_run_gate(equity, spy_rets)}
    return out


def naive_comparison(events: pd.DataFrame, prices: pd.DataFrame,
                      spy_prices: pd.Series, horizon: int) -> dict:
    """Re-anchor tradable_date = disclosure_date (T+0, the naive non-PIT
    anchor) on the same universe-restricted event set, then recompute the
    full-sample gate — the same honesty check backtest_sue_pit.py's
    naive_frames_comparison performs. PIT (d+1) should read weaker than
    this; if PIT reads stronger, that's a red flag for residual
    look-ahead, not a result to trust."""
    naive = events.copy()
    naive["tradable_date"] = pd.to_datetime(naive["disclosure_date"]).dt.date
    daily = daily_holding_returns(naive, prices, horizon)
    if daily.empty:
        return spy_run_gate(pd.Series(dtype=float), pd.Series(dtype=float))
    equity = (1 + daily).cumprod() * 100_000.0
    spy_rets = spy_prices.pct_change().dropna()
    return spy_run_gate(equity, spy_rets)


def run() -> dict:
    events = build_pit_events()
    if events.empty:
        log.warning("No qualified PIT insider events — check the step 4c fetch completed")
        return {"n_events": 0}

    tickers = sorted(events["ticker"].unique())
    prices, missing = fetch_pit_prices(
        tickers, SAMPLE_START.isoformat(), SAMPLE_END.isoformat(), _PRICES_CACHE_DIR,
    )
    spy_prices = fetch_spy_returns()

    results: dict = {"n_events": len(events), "missing_price_tickers": missing}
    for h in HORIZONS:
        results[f"gate_{h}d"] = run_gate_for_horizon(events, prices, spy_prices, h)
        results[f"lag_diagnostic_{h}d"] = lag_bucket_diagnostic(events, prices, spy_prices, h)
        results[f"naive_comparison_{h}d"] = naive_comparison(events, prices, spy_prices, h)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    output = run()
    print("n_events:", output.get("n_events"))
    for h in HORIZONS:
        g = output.get(f"gate_{h}d")
        if g:
            print(f"{h}d research:", g["research"], " validation:", g["validation"])
        print(f"{h}d naive comparison:", output.get(f"naive_comparison_{h}d"))
