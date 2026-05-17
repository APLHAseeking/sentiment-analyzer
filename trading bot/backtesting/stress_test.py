"""Stress scenario generation for walk-forward backtesting.

Each scenario is a frozen dataclass. Factory functions create common presets.
apply_stress_scenario() is the single interface used by walk_forward.py —
it returns modified price data and simulation parameter overrides.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StressScenario:
    name: str
    # Price-level transforms
    crash_pct: float | None = None
    crash_duration_days: int = 5
    vol_multiplier: float | None = None
    vol_duration_days: int = 20
    drop_bar_fraction: float | None = None
    drop_bar_seed: int = 42
    # Simulation-level overrides
    slippage_multiplier: float = 1.0
    fill_delay_bars: int = 0


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def sudden_crash(drop_pct: float = 0.30, duration_days: int = 5) -> StressScenario:
    return StressScenario(name="sudden_crash", crash_pct=drop_pct,
                          crash_duration_days=duration_days)


def high_vol_cluster(vol_mult: float = 3.0, duration_days: int = 20) -> StressScenario:
    return StressScenario(name="high_vol_cluster", vol_multiplier=vol_mult,
                          vol_duration_days=duration_days)


def slippage_spike(multiplier: float = 5.0) -> StressScenario:
    return StressScenario(name="slippage_spike", slippage_multiplier=multiplier)


def delayed_fills(delay_bars: int = 2) -> StressScenario:
    return StressScenario(name="delayed_fills", fill_delay_bars=delay_bars)


def missing_data(drop_fraction: float = 0.05, seed: int = 42) -> StressScenario:
    return StressScenario(name="missing_data", drop_bar_fraction=drop_fraction,
                          drop_bar_seed=seed)


DEFAULT_STRESS_SCENARIOS: list[StressScenario] = [
    sudden_crash(),
    high_vol_cluster(),
    slippage_spike(),
    delayed_fills(),
    missing_data(),
]


# ---------------------------------------------------------------------------
# Price transform functions
# ---------------------------------------------------------------------------

def _apply_crash(
    price_data: dict[str, pd.Series],
    drop_pct: float,
    start_idx: int,
    duration: int,
) -> dict[str, pd.Series]:
    """Log-linear decline anchored to the pre-crash price level.

    At bar start_idx the price begins declining; by bar start_idx+duration-1
    it has fallen exactly drop_pct from the price at start_idx-1 (the anchor).
    Post-crash bars follow the original log-returns from the crash end level —
    no artificial V-shaped recovery, but market direction is preserved.
    """
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty:
            result[ticker] = series.copy()
            continue
        prices_orig = series.to_numpy(dtype=float).copy()
        prices = prices_orig.copy()
        n = len(prices)
        end_idx = min(start_idx + duration, n)
        n_bars = end_idx - start_idx
        if n_bars > 0 and start_idx < n:
            anchor = float(prices_orig[start_idx - 1]) if start_idx > 0 else float(prices_orig[0])
            log_drop = np.log(max(1.0 - drop_pct, 1e-9))
            for k in range(n_bars):
                t = (k + 1) / n_bars
                prices[start_idx + k] = anchor * np.exp(log_drop * t)
            # Post-crash: replay original log-returns from the crash end level
            for j in range(end_idx, n):
                if prices_orig[j - 1] > 0:
                    log_ret = np.log(max(prices_orig[j] / prices_orig[j - 1], 1e-9))
                    prices[j] = prices[j - 1] * np.exp(log_ret)
        result[ticker] = pd.Series(prices, index=series.index, name=series.name)
    return result


def _apply_vol_cluster(
    price_data: dict[str, pd.Series],
    multiplier: float,
    start_idx: int,
    duration: int,
) -> dict[str, pd.Series]:
    """Amplify daily log-returns by multiplier within [start_idx, start_idx+duration).

    Log-returns are read from the ORIGINAL price series so the amplification
    is consistent (no compounding of modified prices into the return calculation).
    Each new bar is built from the running modified price to keep the chain causal.
    """
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty or len(series) < 2:
            result[ticker] = series.copy()
            continue
        prices_orig = series.to_numpy(dtype=float).copy()
        prices = prices_orig.copy()
        n = len(prices)
        end_idx = min(start_idx + duration, n)
        for i in range(max(1, start_idx), end_idx):
            if prices_orig[i - 1] <= 0 or prices_orig[i] <= 0:
                continue
            # Log-return from the ORIGINAL series (not the modified chain)
            log_ret = np.log(prices_orig[i] / prices_orig[i - 1])
            # Apply amplified return on top of the running modified price
            new_price = prices[i - 1] * np.exp(log_ret * multiplier)
            prices[i] = max(new_price, prices[i - 1] * 1e-6)
        result[ticker] = pd.Series(prices, index=series.index, name=series.name)
    return result


def _drop_random_bars(
    price_data: dict[str, pd.Series],
    fraction: float,
    seed: int,
) -> dict[str, pd.Series]:
    """Remove a random fraction of bars from every series (simulates missing data)."""
    rng = np.random.default_rng(seed)
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty:
            result[ticker] = series.copy()
            continue
        n = len(series)
        n_drop = max(0, int(n * fraction))
        if n_drop == 0:
            result[ticker] = series.copy()
            continue
        drop_idx = rng.choice(n, size=n_drop, replace=False)
        mask = np.ones(n, dtype=bool)
        mask[drop_idx] = False
        result[ticker] = series.iloc[mask].copy()
    return result


# ---------------------------------------------------------------------------
# Main interface
# ---------------------------------------------------------------------------

def apply_stress_scenario(
    price_data: dict[str, pd.Series],
    base_slippage_bps: float,
    scenario: StressScenario,
) -> tuple[dict[str, pd.Series], float, int]:
    """Apply a scenario to price data and return modified simulation inputs.

    Returns
    -------
    (stressed_price_data, stressed_slippage_bps, fill_delay_bars)
    """
    stressed: dict[str, pd.Series] = {t: s.copy() for t, s in price_data.items()}

    # Crash and vol cluster start a quarter of the way into the test window
    n_bars = max((len(s) for s in stressed.values()), default=0)
    start_idx = max(1, n_bars // 4)

    if scenario.crash_pct is not None:
        stressed = _apply_crash(stressed, scenario.crash_pct,
                                start_idx, scenario.crash_duration_days)
    if scenario.vol_multiplier is not None:
        stressed = _apply_vol_cluster(stressed, scenario.vol_multiplier,
                                      start_idx, scenario.vol_duration_days)
    if scenario.drop_bar_fraction is not None:
        stressed = _drop_random_bars(stressed, scenario.drop_bar_fraction,
                                     scenario.drop_bar_seed)

    return stressed, base_slippage_bps * scenario.slippage_multiplier, scenario.fill_delay_bars
