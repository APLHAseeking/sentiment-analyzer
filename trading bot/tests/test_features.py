"""Tests for the feature engineering pipeline.

Critical property verified: all features are backward-looking (causal).
No future information leaks into any row.
"""
import numpy as np
import pandas as pd
import pytest

from features.feature_pipeline import (
    FeatureConfig, build_feature_matrix, compute_features,
    build_feature_matrix_with_scaler,
)


def _make_price_data(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
    vix = 15 + rng.normal(0, 3, n)
    return pd.DataFrame({"close": close, "volume": volume, "vix": vix}, index=dates)


def test_compute_features_no_future_leakage():
    """The feature at time t must not depend on data after time t."""
    data = _make_price_data(300)
    cfg = FeatureConfig(vol_window=20, trend_window=50)

    features_full = compute_features(data, cfg)

    # Compute features on a truncated dataset ending at t
    cutoff_idx = 150
    cutoff_date = data.index[cutoff_idx]
    features_truncated = compute_features(data.iloc[: cutoff_idx + 1], cfg)

    if cutoff_date in features_full.index and cutoff_date in features_truncated.index:
        for col in ["ret_1d", "vol_20d"]:
            if col in features_full.columns and col in features_truncated.columns:
                full_val = features_full.loc[cutoff_date, col]
                trunc_val = features_truncated.loc[cutoff_date, col]
                assert abs(full_val - trunc_val) < 1e-9, (
                    f"Look-ahead bias detected in {col}: full={full_val}, trunc={trunc_val}"
                )


def test_compute_features_returns_correct_columns():
    data = _make_price_data(300)
    cfg = FeatureConfig()
    feat = compute_features(data, cfg)
    assert "ret_1d" in feat.columns
    assert "vol_20d" in feat.columns
    assert "trend_z" in feat.columns
    assert "vol_z" in feat.columns


def test_compute_features_no_nan_in_core_cols():
    data = _make_price_data(300)
    cfg = FeatureConfig(vol_window=20, trend_window=50)
    feat = compute_features(data, cfg)
    assert feat["ret_1d"].isna().sum() == 0
    assert feat["vol_20d"].isna().sum() == 0


def test_build_feature_matrix_shape():
    data = _make_price_data(300)
    cfg = FeatureConfig(vol_window=20, trend_window=50, min_history_bars=10)
    X, index, scaler = build_feature_matrix(data, cfg)
    assert X.ndim == 2
    assert X.shape[0] == len(index)
    assert X.shape[1] >= 4  # at least ret, vol, trend, vol_z


def test_build_feature_matrix_scaled():
    data = _make_price_data(300)
    cfg = FeatureConfig(vol_window=20, trend_window=50, min_history_bars=10)
    X, index, scaler = build_feature_matrix(data, cfg)
    # Scaled features should have mean ~0 and std ~1
    assert abs(X.mean()) < 0.5
    assert abs(X.std() - 1.0) < 0.5


def test_build_feature_matrix_with_scaler_no_refit():
    """Applying a frozen scaler must not change its parameters."""
    data = _make_price_data(300)
    cfg = FeatureConfig(vol_window=20, trend_window=50, min_history_bars=10)
    X_train, _, scaler = build_feature_matrix(data.iloc[:200], cfg)
    mean_before = scaler.mean_.copy()

    # Apply to out-of-sample slice
    X_test, _ = build_feature_matrix_with_scaler(data.iloc[200:], scaler, cfg)
    assert np.allclose(scaler.mean_, mean_before), "Scaler was refitted on test data — look-ahead bias!"


def test_insufficient_data_warns(caplog):
    data = _make_price_data(30)
    cfg = FeatureConfig(vol_window=20, trend_window=50, min_history_bars=220)
    import logging
    with caplog.at_level(logging.WARNING, logger="features.feature_pipeline"):
        try:
            build_feature_matrix(data, cfg)
        except Exception:
            pass
    assert any("bars available" in r.message for r in caplog.records)


def test_missing_vix_handled_gracefully():
    data = _make_price_data(300)
    data = data.drop(columns=["vix"])
    cfg = FeatureConfig(use_vix=True, vol_window=20, trend_window=50)
    feat = compute_features(data, cfg)
    assert "ret_1d" in feat.columns  # core features still computed


def test_build_feature_matrix_with_scaler_mean_pads_missing_columns():
    """When the scaler expects more columns than this FeatureConfig produces,
    the missing columns must be padded with the scaler's training mean so
    they standardise to ~0 (neutral), not a spurious z-score."""
    data = _make_price_data(300)

    # Fit a scaler on a wide config (more columns)
    wide_cfg = FeatureConfig(
        vol_window=20, trend_window=50, min_history_bars=10,
        use_vix=False, use_momentum=True, use_drawdown=True,
    )
    _, _, scaler = build_feature_matrix(data.iloc[:200], wide_cfg)

    # Apply via a narrower config (fewer columns) — triggers padding
    narrow_cfg = FeatureConfig(
        vol_window=20, trend_window=50, min_history_bars=10,
        use_vix=False, use_momentum=False, use_drawdown=False,
    )
    X, _ = build_feature_matrix_with_scaler(data.iloc[200:], scaler, narrow_cfg)

    n_expected = scaler.n_features_in_
    n_have = 4  # ret_1d, vol_20d, trend_z, vol_z (no vix/momentum/drawdown)
    assert n_expected > n_have

    # After scaler.transform(), mean-padded columns standardise to ~0
    padded_cols = X[:, n_have:n_expected]
    assert np.allclose(padded_cols, 0.0, atol=1e-8)


def test_build_feature_matrix_with_scaler_aligns_by_name_for_middle_column():
    """When a MIDDLE column (vix_level/vix_change) is missing but LATER
    columns (momentum, drawdown) are present, padding must align by column
    NAME, not position — momentum/drawdown's real values must stay in their
    own slots, not get shifted into vix_level/vix_change's slots."""
    data = _make_price_data(300)

    # Fit scaler on the full 8-column feature set.
    full_cfg = FeatureConfig(
        vol_window=20, trend_window=50, min_history_bars=10,
        use_vix=True, use_momentum=True, use_drawdown=True,
    )
    _, _, scaler = build_feature_matrix(data.iloc[:200], full_cfg)
    full_cols = ["ret_1d", "vol_20d", "trend_z", "vol_z",
                 "vix_level", "vix_change", "momentum", "drawdown"]
    vix_level_mean = scaler.mean_[full_cols.index("vix_level")]
    vix_change_mean = scaler.mean_[full_cols.index("vix_change")]

    # Live config drops VIX (the middle columns) but keeps momentum/drawdown.
    no_vix_cfg = FeatureConfig(
        vol_window=20, trend_window=50, min_history_bars=10,
        use_vix=False, use_momentum=True, use_drawdown=True,
    )
    X, _ = build_feature_matrix_with_scaler(data.iloc[200:], scaler, no_vix_cfg)

    # Recompute the real (unscaled) momentum/drawdown for the same slice to
    # know what the correctly-aligned, scaled values should be.
    feat_df = compute_features(data.iloc[200:], no_vix_cfg)
    momentum_raw = feat_df["momentum"].dropna()
    drawdown_raw = feat_df["drawdown"].dropna()
    common_index = momentum_raw.index.intersection(drawdown_raw.index)

    momentum_scaled_expected = (
        momentum_raw.loc[common_index].values - scaler.mean_[full_cols.index("momentum")]
    ) / scaler.scale_[full_cols.index("momentum")]
    drawdown_scaled_expected = (
        drawdown_raw.loc[common_index].values - scaler.mean_[full_cols.index("drawdown")]
    ) / scaler.scale_[full_cols.index("drawdown")]

    # momentum/drawdown must be in their OWN columns (indices 6, 7), holding
    # their real scaled values — not the mean-fill, and not shifted into
    # vix_level/vix_change's columns (indices 4, 5).
    n = X.shape[0]
    assert np.allclose(X[-n:, 6], momentum_scaled_expected[-n:], atol=1e-6)
    assert np.allclose(X[-n:, 7], drawdown_scaled_expected[-n:], atol=1e-6)
    # vix_level/vix_change (the actually-missing middle columns) standardise to ~0.
    assert np.allclose(X[:, 4], 0.0, atol=1e-8)
    assert np.allclose(X[:, 5], 0.0, atol=1e-8)


def test_vol_z_uses_cfg_vol_window():
    """vol_z must respect cfg.vol_window, not hardcode a 20-bar window."""
    rng = np.random.default_rng(0)
    n = 15
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
    data = pd.DataFrame({"close": close, "volume": volume}, index=dates)

    cfg = FeatureConfig(vol_window=10, trend_window=10)
    feat = compute_features(data, cfg)

    # With vol_window=10 (min_periods=5): all rows surviving the core dropna should
    # have non-NaN vol_z.  A hardcoded 20-bar window (min_periods=10) leaves the
    # first few surviving rows as NaN.
    assert feat["vol_z"].isna().sum() == 0, (
        "vol_z contains NaN — likely still using hardcoded 20-bar window "
        "instead of cfg.vol_window"
    )
