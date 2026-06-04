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
from risk.position_sizing import vol_pct_from_close, vol_target_size_pct
from regime.hmm_engine import HMMRegimeEngine, RegimeState
from backtesting.analysis import (
    regime_performance, confidence_bucket_performance, exposure_by_regime,
)
from backtesting.benchmarks import buy_and_hold, trend_following, random_allocation
from backtesting.metrics import total_return as _total_return
from backtesting.stress_test import DEFAULT_STRESS_SCENARIOS, apply_stress_scenario

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
    benchmarks: dict = field(default_factory=dict)
    regime_breakdown: dict = field(default_factory=dict)
    confidence_breakdown: dict = field(default_factory=dict)
    regime_exposure: dict = field(default_factory=dict)
    stress_results: dict = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    aggregated_metrics: dict = field(default_factory=dict)


def run_walk_forward(
    market_data: pd.DataFrame,
    signal_data: list[dict],
    price_data: dict[str, pd.Series],
    regime_cfg: Any,
    backtest_cfg: Any,
    feature_cfg: FeatureConfig | None = None,
    alloc_cfg: Any = None,
    persist_to_db: bool = True,
    stress_scenarios: list | None = None,
) -> WalkForwardResult:
    """Run walk-forward validation over the full dataset.

    Parameters
    ----------
    market_data      : daily regime features data (SPY/VIX).
    signal_data      : historical signals — congressional AND fundamental screener
                       signals merged and sorted by date. Each dict must have at
                       minimum: {date, ticker, conviction, position_pct}.
    price_data       : individual stock price series for simulation.
    regime_cfg       : regime model configuration.
    backtest_cfg     : train/test window parameters.
    stress_scenarios : list of StressScenario to run per window. Defaults to []
                       (no stress tests). Pass DEFAULT_STRESS_SCENARIOS to enable
                       all five. Stress tests multiply run time by len(scenarios)+1.

    Note on overlapping windows
    ---------------------------
    With step_months < test_months, consecutive windows share bars. Aggregated
    metrics (avg_sharpe, etc.) therefore double-count overlapping periods. Treat
    averages as indicative, not statistically independent estimates.
    """
    if feature_cfg is None:
        feature_cfg = FeatureConfig()

    from system.config import settings as _settings
    _risk_budget = _settings.sizing.per_trade_risk_pct
    _pos_ceiling = _settings.risk.max_position_pct

    scenarios = stress_scenarios if stress_scenarios is not None else []
    if scenarios:
        log.info("Stress testing enabled: %d scenario(s) per window", len(scenarios))

    initial_cash = getattr(backtest_cfg, "initial_cash", 100_000.0)

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

            # Confidence gate — mirrors AllocationEngine.compute() live behaviour
            if alloc_cfg is not None:
                min_conf = getattr(alloc_cfg, "min_confidence_to_trade", 0.0)
                if regime.confidence < min_conf:
                    continue  # would be filtered in production

            # Apply regime size multiplier + optional confidence scaling
            # Vol-target base size from the trailing close window (matches live).
            series_for_vol = price_data.get(sig["ticker"])
            atr_pct = 1.0
            if series_for_vol is not None and not series_for_vol.empty:
                upto = series_for_vol.loc[series_for_vol.index <= pd.Timestamp(sig_date)]
                if len(upto) >= 15:
                    atr_pct = vol_pct_from_close(upto.values, window=14)
            base_pct = vol_target_size_pct(atr_pct, _risk_budget, _pos_ceiling)
            mult = (
                alloc_cfg.regime_size_multiplier.get(regime.regime_label, 1.0)
                if alloc_cfg is not None
                and hasattr(alloc_cfg, "regime_size_multiplier")
                else 1.0
            )
            if alloc_cfg is not None and getattr(alloc_cfg, "confidence_scale", False):
                lo = getattr(alloc_cfg, "min_confidence_to_trade", 0.0)
                conf_mult = 0.5 + 0.5 * (regime.confidence - lo) / max(1.0 - lo, 1e-6)
                mult *= min(conf_mult, 1.0)

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
            initial_cash=initial_cash,
            slippage_bps=backtest_cfg.slippage_bps,
            commission_pct=backtest_cfg.commission_pct,
        )

        eq = equity_series(sim)
        tr = trade_returns(sim)

        # --- Benchmarks (compute before metrics so SPY returns go into compute_all) ---
        benchmarks: dict = {}
        spy_benchmark_returns: pd.Series | None = None
        try:
            spy_close = market_data["close"].loc[
                (market_data.index >= pd.Timestamp(test_start)) &
                (market_data.index <= pd.Timestamp(test_end))
            ]
            if not spy_close.empty:
                spy_benchmark_returns = spy_close.pct_change().dropna()
                slip = backtest_cfg.slippage_bps
                comm = backtest_cfg.commission_pct
                bah_eq = buy_and_hold(spy_close, initial_cash=initial_cash,
                                      slippage_bps=slip, commission_pct=comm)
                tf_eq  = trend_following(spy_close, initial_cash=initial_cash,
                                         slippage_bps=slip, commission_pct=comm)
                rand_eq = random_allocation(
                    spy_close,
                    n_signals=max(len(enriched_signals), 1),
                    initial_cash=initial_cash,
                    slippage_bps=slip,
                    commission_pct=comm,
                )
                benchmarks = {
                    "buy_and_hold": round(_total_return(bah_eq) * 100, 2),
                    "random":       round(_total_return(rand_eq) * 100, 2),
                }
                if not tf_eq.empty:
                    benchmarks["trend_following"] = round(_total_return(tf_eq) * 100, 2)
        except Exception as exc:
            log.warning("Benchmark computation failed for window %d: %s", i + 1, exc)

        metrics = compute_all(
            eq, tr,
            trades=sim.trades,
            total_volume_traded=sim.total_volume_traded,
            benchmark_returns=spy_benchmark_returns,
        )
        metrics["n_regimes"] = engine.n_regimes

        # --- Analysis ---
        regime_breakdown     = regime_performance(sim.trades)
        confidence_breakdown = confidence_bucket_performance(sim.trades)
        regime_exposure      = exposure_by_regime(test_states)

        # --- Stress tests ---
        stress_results: dict = {}
        for scenario in scenarios:
            try:
                s_prices, s_slip, s_delay = apply_stress_scenario(
                    test_price_data, backtest_cfg.slippage_bps, scenario
                )
                s_sim = simulate_portfolio(
                    signals=enriched_signals,
                    price_data=s_prices,
                    initial_cash=initial_cash,
                    slippage_bps=s_slip,
                    commission_pct=backtest_cfg.commission_pct,
                    fill_delay_bars=s_delay,
                )
                s_eq = equity_series(s_sim)
                s_tr = trade_returns(s_sim)
                stress_results[scenario.name] = compute_all(
                    s_eq, s_tr,
                    trades=s_sim.trades,
                    total_volume_traded=s_sim.total_volume_traded,
                )
            except Exception as exc:
                log.warning("Stress scenario '%s' failed (window %d): %s",
                            scenario.name, i + 1, exc)
                stress_results[scenario.name] = {}

        window_result = WalkForwardWindow(
            train_start=str(train_start.date()),
            train_end=str(train_end.date()),
            test_start=test_start,
            test_end=test_end,
            n_regimes=engine.n_regimes,
            metrics=metrics,
            regime_states=test_states,
            benchmarks=benchmarks,
            regime_breakdown=regime_breakdown,
            confidence_breakdown=confidence_breakdown,
            regime_exposure=regime_exposure,
            stress_results=stress_results,
        )
        all_results.append(window_result)
        log.info(
            "Window %d complete: Sharpe=%.2f, MaxDD=%.1f%%, n_trades=%d, "
            "beta=%.2f, alpha=%.1f%%",
            i + 1, metrics.get("sharpe", 0), metrics.get("max_drawdown_pct", 0),
            metrics.get("n_trades", 0),
            metrics.get("beta", float("nan")),
            metrics.get("alpha_annualized_pct", float("nan")),
        )

        if persist_to_db:
            db.log_backtest_result(
                run_id=window_result.run_id,
                train_start=window_result.train_start,
                train_end=window_result.train_end,
                test_start=window_result.test_start,
                test_end=window_result.test_end,
                n_regimes=window_result.n_regimes,
                metrics={
                    **metrics,
                    "benchmarks": benchmarks,
                    "regime_breakdown": regime_breakdown,
                    "confidence_breakdown": confidence_breakdown,
                    "stress_results": stress_results,
                },
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
    keys = [
        "sharpe", "sortino", "max_drawdown_pct", "total_return_pct", "win_rate",
        "beta", "alpha_annualized_pct", "information_ratio", "downside_beta",
    ]
    result: dict = {}
    for k in keys:
        vals = [m.get(k, 0.0) for m in metrics_list if k in m]
        if vals:
            result[f"avg_{k}"]  = round(float(sum(vals) / len(vals)), 3)
            result[f"min_{k}"]  = round(min(vals), 3)
            result[f"max_{k}"]  = round(max(vals), 3)
    result["n_windows"] = len(windows)

    # Benchmark excess return vs strategy total_return per window
    for bench in ("buy_and_hold", "trend_following", "random"):
        pairs = [
            (m.get("total_return_pct", 0.0), w.benchmarks[bench])
            for m, w in zip(metrics_list, windows)
            if bench in w.benchmarks
        ]
        if pairs:
            excess = [s - b for s, b in pairs]
            result[f"avg_excess_return_vs_{bench}_pct"] = round(
                sum(excess) / len(excess), 3
            )

    # Stress scenario aggregation
    scenario_names: set[str] = set()
    for w in windows:
        scenario_names.update(w.stress_results.keys())
    for sname in sorted(scenario_names):
        sharpes = [w.stress_results[sname].get("sharpe", 0.0)
                   for w in windows if sname in w.stress_results]
        mdds = [w.stress_results[sname].get("max_drawdown_pct", 0.0)
                for w in windows if sname in w.stress_results]
        if sharpes:
            result[f"stress_{sname}_avg_sharpe"] = round(
                sum(sharpes) / len(sharpes), 3
            )
        if mdds:
            result[f"stress_{sname}_avg_max_dd_pct"] = round(
                sum(mdds) / len(mdds), 3
            )

    # Regime exposure averaging across windows
    regime_labels: set[str] = set()
    for w in windows:
        regime_labels.update(w.regime_exposure.keys())
    for label in sorted(regime_labels):
        exposures = [w.regime_exposure.get(label, 0.0) for w in windows]
        result[f"avg_exposure_{label}"] = round(
            sum(exposures) / len(exposures), 4
        )

    return result
