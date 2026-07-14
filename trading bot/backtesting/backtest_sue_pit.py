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
