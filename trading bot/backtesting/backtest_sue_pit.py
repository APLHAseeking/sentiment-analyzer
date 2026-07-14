"""SUE PIT backtest driver — see docs/superpowers/plans/2026-07-14-sue-pit-backtest.md
and docs/EDGE_BACKLOG.md for the confirmed spec (PIT semantics, pre-committed
gate). Recommendation-only: never writes to screener/factor_scorer.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtesting.attribution import _hac_standard_errors


def hac_mean_tstat(returns: pd.Series, bandwidth: int) -> tuple[float, float]:
    """Mean and Newey-West HAC t-stat of a return series, reusing the exact
    Bartlett-kernel estimator already in backtesting/attribution.py (mean-only
    regression: X = a column of ones).
    """
    r = returns.dropna().to_numpy()
    n = len(r)
    X = np.ones((n, 1))
    mean = float(r.mean())
    resid = r - mean
    XtX_inv = np.array([[1.0 / n]])
    se = _hac_standard_errors(X, resid, XtX_inv, bandwidth)[0]
    tstat = mean / se if se > 0 else 0.0
    return mean, tstat
