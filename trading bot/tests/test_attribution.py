"""Tests for backtesting.attribution — offline, no network."""
from __future__ import annotations

import io
import textwrap
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.attribution import attribute_returns, load_factor_returns


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2020-01-02", periods=n, freq="B")


def _series(values, name="strat") -> pd.Series:
    return pd.Series(values, index=_dates(len(values)), name=name)


def _factor_df(data: dict, n: int | None = None) -> pd.DataFrame:
    idx = _dates(n or len(next(iter(data.values()))))
    return pd.DataFrame(data, index=idx)


# ── load_factor_returns ───────────────────────────────────────────────────────

def test_load_factor_returns_basic(tmp_path):
    csv = tmp_path / "factors.csv"
    csv.write_text(
        "Date,MKT_RF,SMB,HML\n"
        "2020-01-02,0.001,0.0005,-0.0002\n"
        "2020-01-03,0.002,-0.001,0.0003\n"
    )
    df = load_factor_returns(str(csv))
    assert list(df.columns) == ["MKT_RF", "SMB", "HML"]
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp("2020-01-02")


def test_load_factor_returns_missing_file():
    with pytest.raises(FileNotFoundError):
        load_factor_returns("/nonexistent/factors.csv")


# ── attribute_returns — known-coefficient construction tests ──────────────────

def test_recovered_betas_match_construction():
    """Returns built as 0.0001 + 1.2*MKT - 0.3*HML + noise → betas ≈ (1.2, -0.3)."""
    np.random.seed(42)
    n = 500
    mkt = np.random.normal(0.0003, 0.01, n)
    hml = np.random.normal(0.0001, 0.008, n)
    noise = np.random.normal(0.0, 0.002, n)
    strat = 0.0001 + 1.2 * mkt - 0.3 * hml + noise

    factors = _factor_df({"MKT_RF": mkt, "HML": hml}, n)
    result = attribute_returns(_series(strat), factors)

    assert result["factors"]["MKT_RF"]["beta"] == pytest.approx(1.2, abs=0.05)
    assert result["factors"]["HML"]["beta"] == pytest.approx(-0.3, abs=0.05)


def test_recovered_alpha_matches_construction():
    """Daily drift of 0.0001 → annualised alpha ≈ 0.0001 * 252 = 0.0252 (2.52%)."""
    np.random.seed(7)
    n = 500
    mkt = np.random.normal(0.0003, 0.01, n)
    noise = np.random.normal(0.0, 0.002, n)
    drift = 0.0001
    strat = drift + 1.0 * mkt + noise

    factors = _factor_df({"MKT_RF": mkt}, n)
    result = attribute_returns(_series(strat), factors)

    assert result["alpha"] == pytest.approx(drift * 252, abs=0.01)
    assert result["alpha_pct"] == pytest.approx(drift * 252 * 100, abs=1.0)


def test_zero_alpha_when_pure_factor_exposure():
    """strategy = 1.5 * MKT (no alpha) → alpha ≈ 0."""
    np.random.seed(3)
    n = 500
    mkt = np.random.normal(0.0003, 0.01, n)
    strat = 1.5 * mkt  # pure market exposure, no alpha

    factors = _factor_df({"MKT_RF": mkt}, n)
    result = attribute_returns(_series(strat), factors)

    assert result["alpha"] == pytest.approx(0.0, abs=0.005)


def test_r_squared_near_one_for_deterministic_strategy():
    """strategy = exact linear combination → R² ≈ 1."""
    np.random.seed(5)
    n = 300
    mkt = np.random.normal(0.0003, 0.01, n)
    smb = np.random.normal(0.0001, 0.005, n)
    strat = 0.0002 + 1.1 * mkt + 0.4 * smb  # no noise at all

    factors = _factor_df({"MKT_RF": mkt, "SMB": smb}, n)
    result = attribute_returns(_series(strat), factors)

    assert result["r_squared"] == pytest.approx(1.0, abs=0.001)


def test_four_factor_carhart():
    """Carhart 4-factor: recover all four betas within tolerance."""
    np.random.seed(99)
    n = 600
    mkt = np.random.normal(0.0003, 0.012, n)
    smb = np.random.normal(0.0001, 0.006, n)
    hml = np.random.normal(0.0001, 0.006, n)
    mom = np.random.normal(0.0002, 0.007, n)
    noise = np.random.normal(0.0, 0.003, n)

    true_betas = {"MKT_RF": 1.1, "SMB": 0.3, "HML": -0.2, "MOM": 0.5}
    strat = (0.00005
             + true_betas["MKT_RF"] * mkt
             + true_betas["SMB"] * smb
             + true_betas["HML"] * hml
             + true_betas["MOM"] * mom
             + noise)

    factors = _factor_df({"MKT_RF": mkt, "SMB": smb, "HML": hml, "MOM": mom}, n)
    result = attribute_returns(_series(strat), factors)

    for fname, true_b in true_betas.items():
        recovered = result["factors"][fname]["beta"]
        assert recovered == pytest.approx(true_b, abs=0.08), (
            f"{fname}: expected {true_b}, got {recovered}"
        )


def test_t_statistics_large_for_high_conviction_factor():
    """High-coefficient factor with low noise → |t-stat| >> 2."""
    np.random.seed(11)
    n = 500
    mkt = np.random.normal(0.0003, 0.01, n)
    noise = np.random.normal(0.0, 0.001, n)  # very small noise
    strat = 1.5 * mkt + noise

    factors = _factor_df({"MKT_RF": mkt}, n)
    result = attribute_returns(_series(strat), factors)

    assert abs(result["factors"]["MKT_RF"]["tstat"]) > 10


def test_partial_date_overlap():
    """Factor series with only partial date overlap → uses intersection, no crash."""
    np.random.seed(13)
    n = 300
    all_dates = pd.date_range("2020-01-02", periods=n, freq="B")
    mkt = pd.Series(np.random.normal(0.0003, 0.01, n), index=all_dates)
    strat = pd.Series(np.random.normal(0.0005, 0.01, 200), index=all_dates[:200])
    factors = pd.DataFrame({"MKT_RF": mkt})

    result = attribute_returns(strat, factors)
    assert result["n_obs"] == 200  # only the overlap


def test_empty_result_on_insufficient_data():
    """Too few observations → returns zero-filled result without raising."""
    strat = _series([0.001])
    factors = _factor_df({"MKT_RF": [0.001]}, 1)
    result = attribute_returns(strat, factors)
    assert result["alpha"] == 0.0
    assert result["n_obs"] == 0


def test_attribute_returns_hac_se_exceeds_naive_ols_se_for_autocorrelated_residuals():
    """Strategy residuals with strong positive AR(1) autocorrelation must
    produce a larger (HAC/Newey-West) alpha_se than the naive (homoskedastic,
    serially-uncorrelated) OLS se — HAC corrects for the fact that
    autocorrelated daily returns understate the true sampling uncertainty."""
    rng = np.random.default_rng(42)
    n = 400
    dates = pd.date_range("2020-01-02", periods=n, freq="B")
    mkt = rng.normal(0.0003, 0.01, n)

    # AR(1) residuals with rho=0.7
    eps = rng.normal(0.0, 0.005, n)
    resid_ar1 = np.zeros(n)
    for t in range(1, n):
        resid_ar1[t] = 0.7 * resid_ar1[t - 1] + eps[t]

    strat = 0.0002 + 0.8 * mkt + resid_ar1
    factors = pd.DataFrame({"MKT_RF": mkt}, index=dates)
    strat_series = pd.Series(strat, index=dates)

    result = attribute_returns(strat_series, factors)
    assert "alpha_se" in result
    assert result["alpha_se"] > 0

    # Naive (non-HAC) OLS se for comparison — the old code's formula
    combined = pd.concat([strat_series.rename("_strat"), factors], axis=1).dropna()
    X = np.column_stack([np.ones(len(combined)), combined["MKT_RF"].values])
    y = combined["_strat"].values
    coeffs_naive, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid_naive = y - X @ coeffs_naive
    dof = len(y) - X.shape[1]
    sigma2 = np.dot(resid_naive, resid_naive) / dof
    naive_alpha_se_ann = np.sqrt(np.linalg.inv(X.T @ X)[0, 0] * sigma2) * 252

    assert result["alpha_se"] > naive_alpha_se_ann


def test_empty_result_includes_alpha_se():
    from backtesting.attribution import _empty_result
    result = _empty_result(["MKT_RF"])
    assert result["alpha_se"] == 0.0
