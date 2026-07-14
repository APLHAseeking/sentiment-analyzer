"""SUE PIT backtest driver — see docs/superpowers/plans/2026-07-14-sue-pit-backtest.md
and docs/EDGE_BACKLOG.md for the confirmed spec (PIT semantics, pre-committed
gate). Recommendation-only: never writes to screener/factor_scorer.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import yfinance as yf

from backtesting.attribution import _hac_standard_errors
from backtesting.pit_constituents import fetch_sp500_pit_constituents
from market_data.yf_session import make_shared_yf_session
from screener.xbrl_fundamentals import _fetch_ticker_cik_map
from screener.xbrl_pit_sue import fetch_companyfacts_eps, original_quarterly_eps, pit_sue_asof


def hac_mean_tstat(returns: pd.Series, bandwidth: int) -> tuple[float, float]:
    """Mean and Newey-West HAC t-stat of a return series, reusing the exact
    Bartlett-kernel estimator already in backtesting/attribution.py (mean-only
    regression: X = a column of ones). Returns (nan, nan) for an empty series
    and nan for the t-stat on a degenerate (zero-variance) series, so callers
    can distinguish "insufficient/degenerate data" from a genuine null result
    — this feeds a real pass/fail investment-weight gate downstream, and a
    silent 0.0 would read as confirmed-no-edge rather than can't-evaluate.
    """
    r = returns.dropna().to_numpy()
    n = len(r)
    if n == 0:
        return float("nan"), float("nan")
    X = np.ones((n, 1))
    mean = float(r.mean())
    resid = r - mean
    XtX_inv = np.array([[1.0 / n]])
    se = _hac_standard_errors(X, resid, XtX_inv, bandwidth)[0]
    # resid = r - mean is floating-point subtraction: for a genuinely constant
    # (zero-variance) series it leaves residuals on the order of the machine
    # epsilon rather than exact 0.0, so se lands near 1e-18 instead of ==0.
    # Comparing se to a strict >0 would then produce a huge, meaningless
    # finite t-stat for degenerate data instead of the nan callers need to
    # tell "degenerate" apart from "real null" — use a scale-relative
    # tolerance instead of exact zero.
    degenerate_tol = max(abs(mean), 1.0) * np.finfo(float).eps * n
    tstat = mean / se if se > degenerate_tol else float("nan")
    return mean, tstat


SAMPLE_START = date(2012, 1, 1)
SAMPLE_END = date(2026, 4, 15)
_CACHE_DIR = Path("pit_cache/companyfacts")
_CONSTITUENTS_CACHE = Path("pit_cache/sp500_constituents.parquet")


def universe_tickers() -> set[str]:
    """Every ticker that was ever an S&P 500 PIT member during the sample window."""
    constituents = fetch_sp500_pit_constituents(_CONSTITUENTS_CACHE)
    mask = (constituents["date"] >= SAMPLE_START) & (constituents["date"] <= SAMPLE_END)
    return set(constituents.loc[mask, "ticker"])


_MAX_PLAUSIBLE_SUE = 50.0  # sanity ceiling — a genuine SUE z-score is rarely beyond ~10-20


def build_pit_sue_events(tickers: set[str]) -> pd.DataFrame:
    """One row per (ticker, quarter) with a PIT SUE value: ticker, tradable_date
    (filed + 1 trading day — added in a later step), sue. `filed_date` is the
    actual signal date; every downstream drift/regime computation anchors here.

    quarterly["filed"] is a raw ISO date string (see pit_eps_asof's docstring
    in xbrl_pit_sue.py) — parse explicitly with date.fromisoformat, not
    .dt/.date() accessors.

    IMPORTANT: `pit_sue_asof` is called on the FULL (unfiltered) per-company
    quarterly history, not a SAMPLE_START-truncated slice — the SAMPLE_START/
    SAMPLE_END window controls which events are OUTPUT (in-sample for the
    study), not which prior quarters the seasonal-random-walk denominator
    is allowed to see. Using genuinely-filed pre-SAMPLE_START quarters as
    denominator history is not look-ahead (they were real, PIT-known
    history as of any as_of date after them) — it's exactly what a live
    trader standing on that date would have had. Filtering the INPUT history
    instead (as an earlier version of this function did) starves the
    seasonal-random-walk denominator down to as few as `_MIN_SUE_CHANGE_OBS`
    prior YoY changes, which occasionally coincides with a near-zero (but
    not exactly zero, so not caught by sue_from_quarterly_eps's `sd <= 0`
    guard) standard deviation and produces an enormous, meaningless SUE
    value — reproduced concretely on PTC's 2013-08-07 event (7.2e15 with a
    truncated 43-quarter history vs 1.3 with PTC's full 51-quarter history).
    `_MAX_PLAUSIBLE_SUE` is a second, independent safety net for any
    residual near-zero-variance case even with full history (e.g. a company
    with genuinely flat EPS for an unusually long stretch) — excluded, not
    clipped, consistent with this module's "unknown/degenerate is not
    neutral" convention; never silently reached in practice once the root
    cause above is fixed, but cheap insurance against trusting a garbage
    value in the drift/gate computation.
    """
    cik_map = _fetch_ticker_cik_map(cache=None)
    rows = []
    for ticker in sorted(tickers):
        cik = cik_map.get(ticker)
        if cik is None:
            continue
        facts = fetch_companyfacts_eps(cik, _CACHE_DIR)
        quarterly = original_quarterly_eps(facts)
        if quarterly.empty:
            continue
        in_window = quarterly[
            quarterly["filed"].apply(lambda f: SAMPLE_START <= date.fromisoformat(str(f)) <= SAMPLE_END)
        ]
        # One event per unique filed DATE, not per quarter-row: some filings
        # (typically an annual report's "selected quarterly data" footnote)
        # reveal several historical quarters on the same accession/filed
        # date. pit_sue_asof(quarterly, as_of) depends only on as_of, so
        # iterating per quarter-row produced identical-value duplicate
        # "events" on the same day for the same ticker — 1,652 such rows
        # (659 distinct ticker/date groups) found in the real run, always
        # with matching SUE values, confirming this is the same underlying
        # information becoming known once, not several independent signals.
        for as_of in sorted({date.fromisoformat(str(f)) for f in in_window["filed"]}):
            sue = pit_sue_asof(quarterly, as_of)  # full history — see docstring
            if sue is not None and abs(sue) <= _MAX_PLAUSIBLE_SUE:
                rows.append({"ticker": ticker, "filed_date": as_of, "sue": sue})
    return pd.DataFrame(rows, columns=["ticker", "filed_date", "sue"])


def add_tradable_date(events: pd.DataFrame) -> pd.DataFrame:
    """Tradable date = the first NYSE session strictly after `filed_date` —
    the confirmed PIT lag (filed date + 1 trading day), agreed with the user
    before this backtest was built. Events with no following NYSE session in
    the lookup range (filed right at the end of the sample) are dropped.
    """
    nyse = xcals.get_calendar("XNYS")
    sessions = nyse.sessions_in_range(str(SAMPLE_START), str(SAMPLE_END + timedelta(days=14)))
    sessions = pd.DatetimeIndex(sessions).normalize()

    def _next_session(d) -> object | None:
        after = sessions[sessions > pd.Timestamp(d)]
        return after[0].date() if len(after) else None

    events = events.copy()
    events["tradable_date"] = events["filed_date"].apply(_next_session)
    return events.dropna(subset=["tradable_date"])


def restrict_to_pit_universe(events: pd.DataFrame) -> pd.DataFrame:
    """Drop events where the ticker was not an S&P 500 PIT member as of
    `tradable_date` — otherwise the fundamentals side is PIT-correct but the
    universe side quietly reintroduces survivorship (the exact leak flagged
    before this backtest was built). Same "most recent snapshot on or before
    as_of" semantics as backtesting/pit_data.py's CSVPITProvider.constituents()
    — reimplemented here (not calling CSVPITProvider directly) because that
    class requires fundamentals/prices files this backtest doesn't use.
    """
    constituents = fetch_sp500_pit_constituents(_CONSTITUENTS_CACHE)
    snapshot_dates = pd.DataFrame(
        {"snapshot_date": pd.to_datetime(sorted(constituents["date"].unique()))}
    )

    events = events.sort_values("tradable_date").reset_index(drop=True)
    events["_tradable_dt"] = pd.to_datetime(events["tradable_date"])
    # merge_asof requires numeric/datetime64 "on" columns — object-dtype
    # python `date` columns (as produced by add_tradable_date and
    # fetch_sp500_pit_constituents) raise a MergeError, hence the explicit
    # to_datetime conversion above and on snapshot_dates.
    matched = pd.merge_asof(
        events, snapshot_dates,
        left_on="_tradable_dt", right_on="snapshot_date", direction="backward",
    )
    member_pairs = set(zip(pd.to_datetime(constituents["date"]), constituents["ticker"]))
    mask = matched.apply(lambda r: (r["snapshot_date"], r["ticker"]) in member_pairs, axis=1)
    return matched.loc[mask].drop(columns=["snapshot_date", "_tradable_dt"]).reset_index(drop=True)


HORIZONS = (20, 60)


def fetch_prices(tickers: list[str]) -> pd.DataFrame:
    """Split/dividend-adjusted daily closes for `tickers`, wide format (date
    index, one column per ticker). Uses the repo's shared, timeout-bound
    yfinance session (market_data/yf_session.py) rather than yfinance's
    per-call default, which sets no network timeout at all — the exact
    class of bug that caused a multi-day scheduler hang in the live bot
    this week (see trading bot/CLAUDE.md history, 2026-07-13).
    """
    session = make_shared_yf_session()
    try:
        raw = yf.download(
            tickers, start=str(SAMPLE_START), end=str(SAMPLE_END + timedelta(days=90)),
            auto_adjust=True, progress=False, session=session,
        )
    finally:
        if session is not None:
            session.close()
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        return raw["Close"]
    return raw[["Close"]].rename(columns={"Close": tickers[0]})


def compute_drift(events: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Per-event forward drift over each horizon in HORIZONS, anchored at
    `tradable_date` (d+1) — NOT filed_date or period-end — per the confirmed
    PIT semantics: drift = close(d+1+horizon) / close(d+1) - 1, excluding
    the announcement-day/pre-tradable-date jump by construction. Events for
    tickers missing from `prices`, or too close to the end of available
    price history to observe the full horizon, are dropped (not padded/
    guessed).
    """
    out = events.copy()
    for h in HORIZONS:
        col = []
        for _, row in events.iterrows():
            ticker = row["ticker"]
            if ticker not in prices.columns:
                col.append(None)
                continue
            series = prices[ticker].dropna()
            entry = series[series.index >= pd.Timestamp(row["tradable_date"])]
            if entry.empty:
                col.append(None)
                continue
            entry_idx = series.index.get_loc(entry.index[0])
            exit_idx = entry_idx + h
            if exit_idx >= len(series):
                col.append(None)
                continue
            col.append(float(series.iloc[exit_idx] / series.iloc[entry_idx] - 1.0))
        out[f"drift_{h}d"] = col
    return out.dropna(subset=[f"drift_{h}d" for h in HORIZONS], how="all")
