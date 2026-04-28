"""Walk-forward validation framework.

Rolling windows:
  [train_start ───── train_end] [test_start ─── test_end]
  step →
  [train_start ─────────── train_end] [test_start ─── test_end]
  ...

At each step:
1. Fit HMM regime model on training data ONLY.
2. Classify regimes forward-only on out-of-sample data.
3. Simulate the full portfolio through the test period.
4. Collect metrics.

Critical: the scaler fitted on training data is used unchanged for the
test period, preventing any forward information leakage.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

import bot.db as db
from backtesting.metrics import compute_all
from backtesting.simulation import (
    equity_series, simulate_portfolio, trade_returns
)
from features.feature_pipeline import FeatureConfig
from regime.hmm_engine import HMMRegimeEngine, RegimeState

log = logging.getLogger(__name__)


@dataclass
class WalkForwardWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_regimes: int
    metrics: dict = field(default_factory=dict)
    regime_states: list[RegimeState] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    aggregated_metrics: dict = field(default_factory=dict)


def run_walk_forward(
    market_data: pd.DataFrame,       # full history of market bars (for regime fitting)
    signal_data: list[dict],          # list of {date, ticker, conviction, position_pct}
    price_data: dict[str, pd.Series], # ticker → daily close prices (for simulation)
    regime_cfg: Any,                  # system.config.RegimeConfig
    backtest_cfg: Any,                # system.config.BacktestConfig
    feature_cfg: FeatureConfig | None = None,
    persist_to_db: bool = True,
) -> WalkForwardResult:
    """Run walk-forward validation over the full dataset.

    Parameters
    ----------
    market_data  : daily regime features data (SPY/VIX).
    signal_data  : historical congressional signals (or synthetic test signals).
    price_data   : individual stock price series for simulation.
    regime_cfg   : regime model configuration.
    backtest_cfg : train/test window parameters.
    """
    if feature_cfg is None:
        feature_cfg = FeatureConfig()

    windows = _build_windows(market_data.index, backtest_cfg)
    if not windows:
        log.warning("No walk-forward windows could be constructed")
        return WalkForwardResult(windows=[])

    log.info("Walk-forward: %d windows, train=%.1fy, test=%.1fmo, step=%.1fmo",
             len(windows), backtest_cfg.train_years,
             backtest_cfg.test_months, backtest_cfg.step_months)

    all_results: list[WalkForwardWindow] = []

    for i, (train_start, train_end, test_start, test_end) in enumerate(windows):
        log.info("Window %d/%d: train %s→%s | test %s→%s",
                 i + 1, len(windows), train_start, train_end, test_start, test_end)

        # --- Fit HMM on training data ONLY ---
        train_mask = (market_data.index >= train_start) & (market_data.index <= train_end)
        train_data = market_data.loc[train_mask]

        if len(train_data) < backtest_cfg.min_train_bars:
            log.warning("Window %d: insufficient training data (%d bars), skipping",
                        i + 1, len(train_data))
            continue

        engine = HMMRegimeEngine(regime_cfg)
        try:
            engine.fit(train_data, feature_cfg)
        except Exception as exc:
            log.warning("Window %d: HMM fit failed: %s — skipping", i + 1, exc)
            continue

        # --- Classify test period FORWARD-ONLY using training scaler ---
        # We pass train+test data so the HMM has context, but only act on test dates.
        # The scaler is FROZEN from training — no leakage.
        full_mask = (market_data.index >= train_start) & (market_data.index <= test_end)
        full_data = market_data.loc[full_mask]
        try:
            all_states = engine.classify(full_data, feature_cfg)
        except Exception as exc:
            log.warning("Window %d: classification failed: %s — skipping", i + 1, exc)
            continue

        # Keep only test-period states
        test_states = [s for s in all_states
                       if test_start <= s.date <= test_end]

        # Build a date → regime dict for signal enrichment
        regime_by_date = {s.date: s for s in test_states}

        # --- Enrich signals with regime info ---
        enriched_signals = []
        for sig in signal_data:
            sig_date = sig.get("date", "")
            if not (test_start <= sig_date <= test_end):
                continue
            regime = regime_by_date.get(sig_date)
            if regime is None:
                # Use nearest available regime
                available = [s for s in test_states if s.date <= sig_date]
                regime = available[-1] if available else None
            if regime is None:
                continue

            # Apply regime scaling to position_pct
            base_pct = float(sig.get("position_pct", 5.0))
            mult = regime_cfg.regime_size_multiplier.get(
                regime.regime_label,
                1.0,
            ) if hasattr(regime_cfg, "regime_size_multiplier") else 1.0
            enriched_signals.append({
                **sig,
                "position_pct": base_pct * mult,
                "regime_label": regime.regime_label,
                "regime_confidence": regime.confidence,
            })

        # --- Simulate portfolio ---
        test_price_data = {
            ticker: series.loc[
                (series.index >= pd.Timestamp(test_start)) &
                (series.index <= pd.Timestamp(test_end))
            ]
            for ticker, series in price_data.items()
        }
        sim = simulate_portfolio(
            signals=enriched_signals,
            price_data=test_price_data,
            initial_cash=100_000.0,
            slippage_bps=backtest_cfg.slippage_bps,
            commission_pct=backtest_cfg.commission_pct,
        )

        eq = equity_series(sim)
        tr = trade_returns(sim)
        metrics = compute_all(eq, tr)
        metrics["n_regimes"] = engine.n_regimes

        window_result = WalkForwardWindow(
            train_start=str(train_start.date()),
            train_end=str(train_end.date()),
            test_start=test_start,
            test_end=test_end,
            n_regimes=engine.n_regimes,
            metrics=metrics,
            regime_states=test_states,
        )
        all_results.append(window_result)
        log.info("Window %d complete: Sharpe=%.2f, MaxDD=%.1f%%, n_trades=%d",
                 i + 1, metrics.get("sharpe", 0), metrics.get("max_drawdown_pct", 0),
                 metrics.get("n_trades", 0))

        if persist_to_db:
            db.log_backtest_result(
                run_id=window_result.run_id,
                train_start=window_result.train_start,
                train_end=window_result.train_end,
                test_start=window_result.test_start,
                test_end=window_result.test_end,
                n_regimes=window_result.n_regimes,
                metrics=metrics,
            )

    aggregated = _aggregate(all_results)
    log.info("Walk-forward complete. Avg Sharpe: %.2f | Avg MaxDD: %.1f%%",
             aggregated.get("avg_sharpe", 0), aggregated.get("avg_max_drawdown_pct", 0))
    return WalkForwardResult(windows=all_results, aggregated_metrics=aggregated)


def _build_windows(
    index: pd.DatetimeIndex,
    cfg: Any,
) -> list[tuple[pd.Timestamp, pd.Timestamp, str, str]]:
    """Generate (train_start, train_end, test_start, test_end) tuples."""
    import math

    if len(index) == 0:
        return []

    start = index[0]
    end = index[-1]

    train_td = pd.DateOffset(years=cfg.train_years)
    test_td = pd.DateOffset(months=cfg.test_months)
    step_td = pd.DateOffset(months=cfg.step_months)

    windows = []
    cursor = start
    while True:
        train_start = cursor
        train_end = cursor + train_td
        test_start = train_end + pd.DateOffset(days=1)
        test_end = test_start + test_td

        if test_end > end:
            break

        # Snap to actual available dates
        avail_after_train = index[index > train_end]
        avail_in_test = index[(index >= test_start) & (index <= test_end)]
        if len(avail_after_train) == 0 or len(avail_in_test) < 20:
            break

        windows.append((
            train_start,
            train_end,
            str(avail_in_test[0].date()),
            str(avail_in_test[-1].date()),
        ))
        cursor += step_td

    return windows


def _aggregate(windows: list[WalkForwardWindow]) -> dict:
    if not windows:
        return {}
    metrics_list = [w.metrics for w in windows]
    keys = ["sharpe", "sortino", "max_drawdown_pct", "total_return_pct", "win_rate"]
    result = {}
    for k in keys:
        vals = [m.get(k, 0.0) for m in metrics_list if k in m]
        if vals:
            result[f"avg_{k}"] = round(float(sum(vals) / len(vals)), 3)
            result[f"min_{k}"] = round(min(vals), 3)
            result[f"max_{k}"] = round(max(vals), 3)
    result["n_windows"] = len(windows)
    return result
