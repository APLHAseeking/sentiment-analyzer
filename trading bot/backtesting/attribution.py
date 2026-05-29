"""Factor attribution via OLS regression (numpy only — no statsmodels).

Expected CSV format for load_factor_returns()
----------------------------------------------
Date,MKT_RF,SMB,HML,MOM[,extra_factor,...]

- Date: YYYY-MM-DD (ISO format)
- All return columns: daily decimal returns (e.g. 0.0023, not 2.3)
- Source: Ken French Data Library (daily Fama-French + Momentum factors)
  URL: https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
  Download "Fama/French 3 Factors (Daily)" and "Momentum Factor (Daily)", merge on Date.
- AQR factors (QMJ, BAB) follow the same format.

attribute_returns() aligns all series on their common dates, adds a constant
column for the intercept (alpha), and runs OLS via numpy.linalg.lstsq.
t-statistics are computed from residual variance / (X'X)^{-1} diagonal.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def load_factor_returns(path: str) -> pd.DataFrame:
    """Read a local CSV of daily factor returns in Ken French / AQR format.

    The first column must be named 'Date' (YYYY-MM-DD). All other columns are
    treated as factor return series (decimal, not percent). Returns a DataFrame
    indexed by date with one column per factor.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Factor file not found: {path}")
    df = pd.read_csv(p, parse_dates=["Date"], index_col="Date")
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.dropna(how="all")
    log.info("Loaded %d factor-return rows from %s (columns: %s)",
             len(df), path, list(df.columns))
    return df


def attribute_returns(
    strategy_daily_returns: pd.Series,
    factor_returns: pd.DataFrame,
    risk_free_daily: pd.Series | float = 0.0,
) -> dict:
    """OLS attribution: excess strategy returns ~ factor_returns columns + constant.

    Parameters
    ----------
    strategy_daily_returns : daily return series (decimal).
    factor_returns         : DataFrame of daily factor returns (decimal), one column
                             per factor (e.g. MKT_RF, SMB, HML, MOM).
    risk_free_daily        : scalar or Series of daily risk-free rates (decimal).
                             When a scalar is given it is used for all dates.
                             Ken French factors already express MKT_RF net of the
                             risk-free rate, so pass 0.0 when using those factors.

    Returns
    -------
    dict with keys:
        alpha          : annualised intercept (decimal, not percent)
        alpha_pct      : annualised intercept in percent
        alpha_tstat    : t-statistic for the intercept
        r_squared      : OLS R²
        factors        : {factor_name: {"beta": float, "tstat": float}} per factor
        n_obs          : number of observations used
    """
    # Align all series on common dates (inner join)
    combined = pd.concat([strategy_daily_returns.rename("_strat"), factor_returns], axis=1).dropna()
    if len(combined) < len(factor_returns.columns) + 2:
        log.warning("Too few aligned observations (%d) for attribution — returning zeros", len(combined))
        return _empty_result(factor_returns.columns.tolist())

    excess_strat = combined["_strat"]
    if not isinstance(risk_free_daily, (int, float)):
        rf = risk_free_daily.reindex(combined.index).fillna(0.0)
    else:
        rf = float(risk_free_daily)
    excess_strat = excess_strat - rf

    X_factors = combined[factor_returns.columns]
    n, k = len(X_factors), len(factor_returns.columns)

    # Build design matrix: [1, factor_1, ..., factor_k]
    X = np.column_stack([np.ones(n), X_factors.values])
    y = excess_strat.values

    # OLS via least squares
    coeffs, residuals_ss, rank, _ = np.linalg.lstsq(X, y, rcond=None)

    # Residuals and variance
    y_hat = X @ coeffs
    resid = y - y_hat
    dof = n - k - 1  # degrees of freedom (subtract intercept + k factors)
    if dof <= 0:
        log.warning("Insufficient degrees of freedom (%d) for t-statistics", dof)
        return _empty_result(factor_returns.columns.tolist())

    sigma2 = float(np.dot(resid, resid) / dof)

    # (X'X)^{-1} for standard errors
    XtX = X.T @ X
    try:
        XtX_inv = np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        log.warning("Singular X'X matrix — cannot compute t-statistics")
        return _empty_result(factor_returns.columns.tolist())

    se = np.sqrt(np.maximum(np.diag(XtX_inv) * sigma2, 0.0))
    tstats = coeffs / np.where(se > 0, se, np.inf)

    # R²
    ss_tot = float(np.var(y, ddof=1)) * (n - 1)
    r2 = 1.0 - float(np.dot(resid, resid)) / ss_tot if ss_tot > 0 else 0.0

    # Intercept is coeffs[0] (daily); annualise
    alpha_daily = float(coeffs[0])
    alpha_ann = alpha_daily * 252
    alpha_tstat = float(tstats[0])

    factor_results = {}
    for i, col in enumerate(factor_returns.columns):
        factor_results[col] = {
            "beta": round(float(coeffs[i + 1]), 4),
            "tstat": round(float(tstats[i + 1]), 3),
        }

    return {
        "alpha": round(alpha_ann, 6),
        "alpha_pct": round(alpha_ann * 100, 4),
        "alpha_tstat": round(alpha_tstat, 3),
        "r_squared": round(r2, 4),
        "factors": factor_results,
        "n_obs": n,
    }


def _empty_result(factor_names: list[str]) -> dict:
    return {
        "alpha": 0.0,
        "alpha_pct": 0.0,
        "alpha_tstat": 0.0,
        "r_squared": 0.0,
        "factors": {f: {"beta": 0.0, "tstat": 0.0} for f in factor_names},
        "n_obs": 0,
    }
