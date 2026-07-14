# tests/test_backtest_sue_pit_stats.py
from __future__ import annotations

import numpy as np
import pandas as pd

from backtesting.backtest_sue_pit import hac_mean_tstat


def test_hac_mean_tstat_matches_sign_and_rough_magnitude():
    rng = np.random.default_rng(42)
    returns = pd.Series(rng.normal(loc=0.001, scale=0.01, size=500))
    mean, tstat = hac_mean_tstat(returns, bandwidth=20)
    assert mean == pytest_approx_close(returns.mean())
    assert tstat > 0  # positive mean -> positive t


def pytest_approx_close(x, tol=1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - x) < tol
    return _Approx()


def test_hac_tstat_smaller_than_naive_iid_under_induced_autocorrelation():
    """Overlapping-window returns are serially correlated by construction —
    HAC SE must be larger (t-stat smaller in magnitude) than a naive i.i.d.
    SE computed on the same series, or the reused Newey-West wiring is broken.
    """
    rng = np.random.default_rng(7)
    shocks = rng.normal(0, 0.01, size=520)
    # 20-day rolling sum induces strong positive serial correlation, like an
    # overlapping-holding-period calendar-time portfolio.
    overlapping = pd.Series(shocks).rolling(20).sum().dropna().reset_index(drop=True)

    _, hac_t = hac_mean_tstat(overlapping, bandwidth=20)
    naive_se = overlapping.std(ddof=1) / np.sqrt(len(overlapping))
    naive_t = overlapping.mean() / naive_se

    assert abs(hac_t) < abs(naive_t)
