"""Phase 0 PIT backtest driver for the bot's primary signal — the full
fundamental factor composite (value/momentum/quality/low-vol/reversal),
scored via the same `_build_factor_df`/`_compute_composite` core the live
screener uses. See docs/PHASE0_FINDINGS.md for the gate this answers and
docs/BOT_REVIEW_2026-07-20.md for why this is the highest-leverage open item.

Mirrors backtesting/backtest_sue_pit.py's structure and discipline: the gate
(t-stat > 2 AND IR > 0.5, stable across periods) is pre-committed here,
before running, exactly as docs/PHASE0_FINDINGS.md already states it.
Recommendation-only — never writes to screener/factor_scorer.py.

Sample window: 2021-09-01 through 2025-06-30. Bounded on both ends by
SimFin's free-tier fundamentals history (2020-08-31 to 2025-06-30, confirmed
this session) plus a ~1-year run-in required before the first rebalance can
have a full trailing-4-quarter window (screener/simfin_fundamentals.py's
compute_fundamentals_snapshots returns None for trailing-figure ratios
before that). This is a real, disclosed limitation — fewer non-overlapping
walk-forward windows than an ideal multi-decade sample would give, not
hidden in the writeup.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from backtesting.backtest_sue_pit import hac_mean_tstat
from backtesting.pit_constituents import fetch_sp500_pit_constituents
from backtesting.pit_data import CSVPITProvider
from backtesting.run_strategy_backtest import run_pit_backtest
from market_data.pit_prices import TiingoRateLimited, fetch_pit_prices, fetch_ticker_prices
from market_data.yf_session import make_shared_yf_session
from screener.ff_factors import fetch_ff_factors
from screener.simfin_fundamentals import (
    compute_fundamentals_snapshots, fetch_simfin_dataset, sector_map,
)

log = logging.getLogger(__name__)

SAMPLE_START = date(2021, 9, 1)
SAMPLE_END = date(2025, 6, 30)
REBALANCE_DATES = [
    # Timestamp.isoformat() includes a time component ('2021-09-30T00:00:00'),
    # which date.fromisoformat() (run_pit_backtest's own parser) rejects —
    # .date().isoformat() gives the plain 'YYYY-MM-DD' it actually expects.
    d.date().isoformat() for d in pd.date_range(SAMPLE_START, SAMPLE_END, freq="QE")
]

_CACHE_DIR = Path("pit_cache")
_CONSTITUENTS_CACHE = _CACHE_DIR / "sp500_constituents.parquet"
_SIMFIN_CACHE_DIR = _CACHE_DIR / "simfin"
_PRICES_CACHE_DIR = _CACHE_DIR / "prices"
_FF_FACTORS_CACHE = _CACHE_DIR / "ff_factors.csv"

_BUILT_INPUTS_DIR = _CACHE_DIR / "backtest_inputs"
_CONSTITUENTS_CSV = _BUILT_INPUTS_DIR / "constituents.csv"
_FUNDAMENTALS_CSV = _BUILT_INPUTS_DIR / "fundamentals.csv"
_PRICES_CSV = _BUILT_INPUTS_DIR / "prices.csv"

_HAC_BANDWIDTH = 21  # ~1 trading month — daily strategy returns from a
                     # quarterly-rebalanced book are autocorrelated at
                     # roughly this scale; a defensible default, not tuned
                     # to the result (chosen before running the gate).


def universe_tickers() -> set[str]:
    """Every ticker that was ever an S&P 500 PIT member during the sample window."""
    constituents = fetch_sp500_pit_constituents(_CONSTITUENTS_CACHE)
    mask = (
        (constituents["date"] >= SAMPLE_START) & (constituents["date"] <= SAMPLE_END)
    )
    return set(constituents.loc[mask, "ticker"])


def build_constituents_csv() -> None:
    df = fetch_sp500_pit_constituents(_CONSTITUENTS_CACHE)
    mask = (df["date"] >= SAMPLE_START) & (df["date"] <= SAMPLE_END)
    _BUILT_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    df.loc[mask].to_csv(_CONSTITUENTS_CSV, index=False)


def build_prices_csv(tickers: list[str]) -> list[str]:
    """Fetch (or load from cache) daily prices for every ticker and write
    the wide-format prices.csv CSVPITProvider expects. Returns the list of
    tickers found in neither yfinance nor Tiingo (an honest gap list, not
    silently dropped from the report)."""
    wide, missing = fetch_pit_prices(
        tickers, SAMPLE_START.isoformat(), SAMPLE_END.isoformat(), _PRICES_CACHE_DIR,
    )
    _BUILT_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    wide.to_csv(_PRICES_CSV)
    return missing


def build_fundamentals_csv(tickers: list[str] | None = None) -> None:
    """`tickers`, if given, restricts which tickers get ratio-computed (and
    therefore which get a price_lookup() call) — MUST match whatever ticker
    set build_prices_csv() was already called with, or price_lookup() will
    trigger fresh, uncached, unbudgeted fetches for every ticker outside
    that set (a real bug caught live this session: a scope mismatch here
    silently walked SimFin's full ~3,781-ticker universe and burned real
    Tiingo rate-limit budget on tickers nobody asked to fetch). None means
    "every ticker in the SimFin datasets" — only correct for the real full
    production run where build_prices_csv() was also called with the full
    universe.
    """
    # SimFin reports banks and insurers on separate datasets with materially
    # different statement structures (no "Short Term Debt"/"Total Equity" in
    # a bank's income statement, no "Revenue" on a bank's balance sheet,
    # etc.) — confirmed empirically that all 3 variants share the same core
    # columns compute_fundamentals_snapshots actually reads (Ticker/Report
    # Date/Publish Date/Revenue/Net Income/Shares (Diluted) for income;
    # Total Equity/Short Term Debt/Long Term Debt for balance; Net Cash from
    # Operating Activities/Change in Fixed Assets & Intangibles for
    # cashflow), so concatenating is safe. Omitting the -banks/-insurance
    # variants was a real gap caught while preparing this backtest's report:
    # 146 of 576 universe tickers (25%, concentrated in financials — AIG,
    # AXP, BAC, C, BK, CB and more) had zero fundamentals at all under the
    # generic-only fetch, silently excluding an entire sector rather than
    # scoring it and letting it lose on the merits.
    def _fetch_all_variants(dataset: str) -> pd.DataFrame:
        variants = [dataset, f"{dataset}-banks", f"{dataset}-insurance"]
        frames = [
            fetch_simfin_dataset(v, _SIMFIN_CACHE_DIR / f"{v}.parquet", variant="quarterly")
            for v in variants
        ]
        return pd.concat(frames, ignore_index=True)

    income = _fetch_all_variants("income")
    balance = _fetch_all_variants("balance")
    cashflow = _fetch_all_variants("cashflow")
    companies = fetch_simfin_dataset("companies", _SIMFIN_CACHE_DIR / "companies.parquet")
    industries = fetch_simfin_dataset("industries", _SIMFIN_CACHE_DIR / "industries.parquet")
    sectors = sector_map(companies, industries)

    if tickers is not None:
        ticker_set = set(tickers)
        income = income[income["Ticker"].isin(ticker_set)]
        balance = balance[balance["Ticker"].isin(ticker_set)]
        cashflow = cashflow[cashflow["Ticker"].isin(ticker_set)]

    def price_lookup(ticker: str, as_of: date) -> float | None:
        try:
            series = fetch_ticker_prices(ticker, SAMPLE_START.isoformat(), SAMPLE_END.isoformat(),
                                          _PRICES_CACHE_DIR)
        except TiingoRateLimited as exc:
            # Not cached (see fetch_ticker_prices) — a later run can still
            # find real data. Here it just means this one snapshot's
            # price-dependent ratios (trailingPE/priceToBook/marketCap) are
            # None for now, same as any other price gap.
            log.warning("%s — no price for this fundamentals snapshot", exc)
            return None
        if series is None or series.empty:
            return None
        before = series[series.index.date <= as_of] if hasattr(series.index, "date") else series
        if before.empty:
            return None
        return float(before.iloc[-1])

    snapshots = compute_fundamentals_snapshots(income, balance, cashflow, sectors, price_lookup)
    _BUILT_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    snapshots.to_csv(_FUNDAMENTALS_CSV, index=False)


def run_gate(equity: pd.Series, benchmark_returns: pd.Series) -> dict:
    """HAC mean/t-stat/IR of the strategy's daily excess return over SPY —
    same formula shape as backtest_sue_pit.py's run_gate, generalized from
    an event-study return to a continuous equity-curve return series."""
    strat_returns = equity.pct_change().dropna()
    aligned = pd.concat([strat_returns, benchmark_returns], axis=1, join="inner").dropna()
    aligned.columns = ["strategy", "benchmark"]
    active = aligned["strategy"] - aligned["benchmark"]
    mean, tstat = hac_mean_tstat(active, bandwidth=_HAC_BANDWIDTH)
    std = active.std(ddof=1) if len(active) > 1 else float("nan")
    annualization = 252 ** 0.5
    ir = (mean / std) * annualization if std and std > 0 else float("nan")
    return {"mean_daily_excess": mean, "tstat": tstat, "ir": ir, "n_days": len(active)}


def stability_split(equity: pd.Series, benchmark_returns: pd.Series) -> dict:
    """First-half vs second-half of the equity curve, split by date count."""
    mid = len(equity) // 2
    return {
        "first_half": run_gate(equity.iloc[:mid + 1], benchmark_returns),
        "second_half": run_gate(equity.iloc[mid:], benchmark_returns),
    }


def fetch_spy_returns() -> pd.Series:
    session = make_shared_yf_session()
    try:
        import yfinance as yf
        hist = yf.Ticker("SPY", session=session).history(
            start=SAMPLE_START.isoformat(), end=SAMPLE_END.isoformat(), auto_adjust=True,
        )
    finally:
        if session is not None:
            session.close()
    prices = hist["Close"]
    prices.index = pd.to_datetime(prices.index).tz_localize(None)
    return prices


def run() -> dict:
    """Build all three PIT input files, run the backtest, compute the
    pre-committed gate. Returns a dict with the backtest result, gate,
    stability split, and the list of tickers with no price data found
    anywhere (an honest gap, reported not hidden)."""
    tickers = sorted(universe_tickers())
    log.info("Universe: %d unique historical tickers", len(tickers))

    build_constituents_csv()
    missing_price_tickers = build_prices_csv(tickers)
    build_fundamentals_csv(tickers)

    fetch_ff_factors(_FF_FACTORS_CACHE)

    spy_prices = fetch_spy_returns()

    provider = CSVPITProvider(
        constituents_path=str(_CONSTITUENTS_CSV),
        fundamentals_path=str(_FUNDAMENTALS_CSV),
        prices_path=str(_PRICES_CSV),
    )

    result = run_pit_backtest(
        provider=provider,
        rebalance_dates=REBALANCE_DATES,
        top_n=20,
        spy_prices=spy_prices,
        factor_csv_path=str(_FF_FACTORS_CACHE),
    )

    spy_rets = spy_prices.pct_change().dropna()
    gate = run_gate(result["equity_series"], spy_rets)
    stability = stability_split(result["equity_series"], spy_rets)

    return {
        "backtest": result,
        "gate": gate,
        "stability": stability,
        "missing_price_tickers": missing_price_tickers,
        "n_universe_tickers": len(tickers),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    output = run()
    print("Gate:", output["gate"])
    print("Stability:", output["stability"])
    print("Missing-price tickers:", len(output["missing_price_tickers"]))
    print("Metrics:", output["backtest"]["metrics"])
