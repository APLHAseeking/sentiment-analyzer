"""Point-in-time strategy backtest runner.

Uses CSVPITProvider (or any PITDataProvider) to fetch survivorship-free
constituent lists, PIT fundamentals, and historical prices, then runs the
full scoring → simulation → attribution pipeline.

Usage
-----
    from backtesting.pit_data import CSVPITProvider
    from backtesting.run_strategy_backtest import run_pit_backtest

    provider = CSVPITProvider(
        constituents_path="data/constituents.csv",
        fundamentals_path="data/fundamentals.csv",
        prices_path="data/prices.csv",
    )
    result = run_pit_backtest(
        provider=provider,
        rebalance_dates=["2020-06-30", "2020-12-31", "2021-06-30"],
        top_n=20,
        position_pct=5.0,
        initial_cash=100_000.0,
        factor_csv_path="data/ff_factors.csv",  # optional, for attribution
    )
    print(result["metrics"])
    if result["attribution"]:
        print(result["attribution"])
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from backtesting.pit_data import PITDataProvider
from backtesting.metrics import compute_all
from backtesting.simulation import equity_series, simulate_portfolio, trade_returns
from screener.factor_scorer import _build_factor_df, _compute_composite
from risk.position_sizing import vol_pct_from_close, vol_target_size_pct
from system.config import settings as _settings

log = logging.getLogger(__name__)

_MIN_MOMENTUM_BARS = 60   # looser than live screener (PIT data may be thinner)


def _compute_pit_momentum(
    provider: PITDataProvider,
    tickers: list[str],
    as_of: date,
    lookback_days: int = 365,
) -> dict[str, tuple[float | None, float | None, float | None, float | None]]:
    """Compute (mom_1m, mom_12m, mom_6m, high52_ratio) from PIT prices for each ticker."""
    start = as_of - timedelta(days=lookback_days + 40)
    result: dict[str, tuple[float | None, float | None, float | None, float | None]] = {}
    for ticker in tickers:
        prices = provider.prices(ticker, start, as_of)
        if len(prices) < _MIN_MOMENTUM_BARS:
            result[ticker] = (None, None, None, None)
            continue
        current = float(prices.iloc[-1])
        p1m = float(prices.iloc[max(0, len(prices) - 21)])
        # 12-month momentum requires exactly 252 trading bars back.
        # Using iloc[0] of whatever window is available gives a return over
        # ~405 days (lookback_days + 40) rather than 252 days and is wrong
        # when the history is thinner than that.
        p12m = float(prices.iloc[-252]) if len(prices) >= 252 else None
        # 6-1 momentum: return from ~6 months ago to ~1 month ago (skip last month)
        p6m_start = float(prices.iloc[-126]) if len(prices) >= 126 else None
        p6m_end = float(prices.iloc[max(0, len(prices) - 21)])
        mom_6m = (
            (p6m_end / p6m_start - 1) * 100
            if p6m_start is not None and p6m_start > 0
            else None
        )
        # 52-week high ratio: proximity to 52-week high
        high52 = float(prices.max())
        high52_ratio = current / high52 if high52 > 0 else None
        result[ticker] = (
            (current / p1m - 1) * 100 if p1m > 0 else None,
            (current / p12m - 1) * 100 if p12m is not None and p12m > 0 else None,
            mom_6m,
            high52_ratio,
        )
    return result


def _compute_pit_price_factors(
    provider: PITDataProvider,
    tickers: list[str],
    as_of: date,
    spy_prices: pd.Series | None = None,
    lookback_days: int = 365,
) -> dict[str, tuple[float | None, float | None, float | None]]:
    """Compute (realized_vol, beta, resid_mom) from PIT prices.

    Mirrors _fetch_price_factors_batch from the live screener but reads prices
    from the PITDataProvider instead of yfinance. SPY prices come from the
    spy_prices Series already fetched by the caller (shared, no extra download).
    """
    start = as_of - timedelta(days=lookback_days + 40)
    spy_ret: pd.Series | None = None
    if spy_prices is not None and not spy_prices.empty:
        spy_window = spy_prices[
            (spy_prices.index.date >= start) & (spy_prices.index.date <= as_of)
        ]
        if not spy_window.empty:
            spy_ret = spy_window.pct_change().dropna()

    result: dict[str, tuple[float | None, float | None, float | None]] = {}
    for ticker in tickers:
        prices = provider.prices(ticker, start, as_of)
        if len(prices) < _MIN_MOMENTUM_BARS:
            result[ticker] = (None, None, None)
            continue
        ret = prices.pct_change().dropna()
        vol = float(ret.std() * (252 ** 0.5) * 100) if ret.std() > 0 else None
        beta: float | None = None
        resid_mom: float | None = None
        if spy_ret is not None:
            aligned = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
            aligned.columns = ["stock", "spy"]
            spy_var = float(aligned["spy"].var())
            if len(aligned) >= _MIN_MOMENTUM_BARS and spy_var > 0:
                beta = float(aligned["stock"].cov(aligned["spy"]) / spy_var)
                resid = aligned["stock"] - beta * aligned["spy"]
                resid_window = resid.iloc[:max(0, len(resid) - 21)]
                if len(resid_window) > 0:
                    resid_mom = float(((1 + resid_window).prod() - 1) * 100)
        result[ticker] = (vol, beta, resid_mom)
    return result


def run_pit_backtest(
    provider: PITDataProvider,
    rebalance_dates: list[str],
    top_n: int = 20,
    position_pct: float = 5.0,
    initial_cash: float = 100_000.0,
    regime_label: str | None = None,
    slippage_bps: float = 10.0,
    commission_pct: float = 0.05,
    factor_csv_path: str | None = None,
    spy_prices: pd.Series | None = None,
    per_trade_risk_pct: float | None = None,
    max_position_pct: float | None = None,
) -> dict:
    """Run a full PIT backtest across the given rebalance dates.

    Parameters
    ----------
    provider          : PITDataProvider implementation (e.g. CSVPITProvider)
    rebalance_dates   : list of ISO date strings at which to score + rebalance
    top_n             : number of top-scoring tickers to generate signals for
    position_pct      : fixed position size (% of NAV) per signal
    initial_cash      : starting capital
    regime_label      : optional regime label for regime-aware factor weights
    slippage_bps      : one-way slippage in basis points
    commission_pct    : round-trip commission as % of trade value
    factor_csv_path   : path to Ken French factor CSV for attribution (optional)
    spy_prices        : SPY daily close series for beta/alpha/IR (optional)

    Returns
    -------
    dict with keys:
        metrics     : compute_all() output dict
        attribution : attribute_returns() output dict (None if no factor_csv_path)
        signals     : list of signal dicts generated
        n_signals   : total signals
        windows     : per-rebalance-date details
    """
    risk_budget = per_trade_risk_pct if per_trade_risk_pct is not None else _settings.sizing.per_trade_risk_pct
    pos_ceiling = max_position_pct if max_position_pct is not None else _settings.risk.max_position_pct
    all_signals: list[dict] = []
    all_prices: dict[str, pd.Series] = {}
    windows: list[dict] = []
    forced_closes: dict[str, list[str]] = {}
    prev_window_tickers: list[str] = []

    sorted_dates = sorted(date.fromisoformat(d) for d in rebalance_dates)

    for i, rebal_date in enumerate(sorted_dates):
        hold_end = sorted_dates[i + 1] if i + 1 < len(sorted_dates) else rebal_date + timedelta(days=180)

        log.info("Rebalance date %s → hold through %s", rebal_date, hold_end)

        # 1. PIT-safe universe
        universe = provider.constituents(rebal_date)
        if not universe:
            log.warning("No constituents found for %s — skipping", rebal_date)
            continue
        tickers = sorted(universe)

        # 2. PIT-safe fundamentals (yfinance .info compatible dict)
        infos: dict[str, dict | None] = {}
        for ticker in tickers:
            infos[ticker] = provider.fundamentals(ticker, rebal_date)

        # 3. PIT-safe momentum from price history
        momentum = _compute_pit_momentum(provider, tickers, rebal_date)

        # 3b. Price-based factors (vol, beta, resid_mom) — same window, shared SPY prices
        price_factors = _compute_pit_price_factors(provider, tickers, rebal_date, spy_prices)

        # 4. Score using the same core as the live screener (all 5 sleeves)
        factor_df = _build_factor_df(infos, momentum, price_factors)
        if factor_df.empty:
            log.warning("Empty factor_df for %s — no fundamentals available", rebal_date)
            continue
        scored_df = _compute_composite(factor_df, regime_label=regime_label)
        if scored_df.empty:
            log.warning("Empty scored_df for %s — all tickers filtered out", rebal_date)
            continue

        top = scored_df.nlargest(top_n, "composite_score")
        rebal_str = rebal_date.isoformat()
        hold_end_str = hold_end.isoformat()

        # Close previous window's positions at this rebalance date before
        # opening new ones — prevents capital lock-up across quarterly windows.
        if prev_window_tickers:
            forced_closes[rebal_str] = list(prev_window_tickers)

        window_tickers = top.index.tolist()
        prev_window_tickers = window_tickers
        for ticker in window_tickers:
            # Vol-target sizing from the trailing close window (matches live)
            vol_window = provider.prices(
                ticker, rebal_date - timedelta(days=40), rebal_date
            )
            atr_pct = vol_pct_from_close(vol_window.values, window=14) if not vol_window.empty else 1.0
            size_pct = vol_target_size_pct(atr_pct, risk_budget, pos_ceiling)
            all_signals.append({
                "date": rebal_str,
                "ticker": ticker,
                "conviction": int(top.loc[ticker, "composite_score"]),
                "position_pct": size_pct,
            })
            # Collect prices for the holding period
            if ticker not in all_prices:
                series = provider.prices(ticker,
                                         rebal_date - timedelta(days=5),
                                         hold_end)
                if not series.empty:
                    all_prices[ticker] = series

        windows.append({
            "rebalance_date": rebal_str,
            "hold_end": hold_end_str,
            "n_universe": len(universe),
            "n_top": len(top),
            "top_tickers": window_tickers,
        })
        log.info("  Universe=%d  scored=%d  top%d selected",
                 len(universe), len(scored_df), len(top))

    if not all_signals:
        log.warning("No signals generated — check PIT data coverage")
        empty_eq = pd.Series([initial_cash], index=pd.date_range("today", periods=1))
        return {
            "metrics": compute_all(empty_eq),
            "attribution": None,
            "signals": [],
            "n_signals": 0,
            "windows": windows,
        }

    # Simulate
    sim = simulate_portfolio(
        signals=all_signals,
        price_data=all_prices,
        initial_cash=initial_cash,
        slippage_bps=slippage_bps,
        commission_pct=commission_pct,
        fill_delay_bars=1,
        forced_closes=forced_closes,
    )
    eq = equity_series(sim)
    tr = trade_returns(sim)

    # Benchmark returns (SPY) for beta/alpha/IR
    spy_rets: pd.Series | None = None
    if spy_prices is not None and not spy_prices.empty:
        spy_rets = spy_prices.pct_change().dropna()

    metrics = compute_all(
        eq, tr,
        trades=sim.trades,
        total_volume_traded=sim.total_volume_traded,
        benchmark_returns=spy_rets,
    )

    # Factor attribution
    attribution = None
    if factor_csv_path is not None:
        try:
            from backtesting.attribution import attribute_returns, load_factor_returns
            factors = load_factor_returns(factor_csv_path)
            strat_rets = eq.pct_change().dropna()
            attribution = attribute_returns(strat_rets, factors)
        except Exception as exc:
            log.warning("Attribution failed: %s", exc)

    log.info(
        "PIT backtest complete: %d signals, %d trades, Sharpe=%.2f",
        len(all_signals), metrics.get("n_trades", 0), metrics.get("sharpe", 0),
    )
    return {
        "metrics": metrics,
        "attribution": attribution,
        "signals": all_signals,
        "n_signals": len(all_signals),
        "windows": windows,
    }
