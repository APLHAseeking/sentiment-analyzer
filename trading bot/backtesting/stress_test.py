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
    """Log-linear decline of drop_pct over duration bars starting at start_idx.

    Prices after the crash window stay at the new (lower) level — no recovery.
    """
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty:
            result[ticker] = series.copy()
            continue
        prices = series.to_numpy(dtype=float).copy()
        n = len(prices)
        end_idx = min(start_idx + duration, n)
        n_bars = end_idx - start_idx
        if n_bars > 0:
            for k in range(n_bars):
                prices[start_idx + k] *= (1 - drop_pct) ** ((k + 1) / n_bars)
            prices[end_idx:] *= (1 - drop_pct)
        result[ticker] = pd.Series(prices, index=series.index, name=series.name)
    return result


def _apply_vol_cluster(
    price_data: dict[str, pd.Series],
    multiplier: float,
    start_idx: int,
    duration: int,
) -> dict[str, pd.Series]:
    """Amplify daily log-returns by multiplier within [start_idx, start_idx+duration)."""
    result: dict[str, pd.Series] = {}
    for ticker, series in price_data.items():
        if series.empty or len(series) < 2:
            result[ticker] = series.copy()
            continue
        prices = series.to_numpy(dtype=float).copy()
        n = len(prices)
        end_idx = min(start_idx + duration, n)
        for i in range(max(1, start_idx), end_idx):
            prev = prices[i - 1]
            if prev <= 0:
                continue
            raw = prices[i]
            if raw <= 0:
                continue
            log_ret = np.log(raw / prev)
            new_price = prev * np.exp(log_ret * multiplier)
            # Clamp to a small positive floor to prevent collapse to zero/inf
            prices[i] = max(new_price, prev * 1e-6)
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
