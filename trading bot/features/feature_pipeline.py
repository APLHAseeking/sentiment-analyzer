"""Feature engineering pipeline for regime detection.

All features are computed in a strictly causal (non-look-ahead) manner:
each row at time t uses only data from t and earlier.

Train/test split safety: the pipeline operates on whatever slice of data
you pass in. The caller is responsible for passing only training-window
data when fitting the regime model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)


@dataclass
class FeatureConfig:
    vol_window: int = 20
    trend_window: int = 200
    momentum_window: int = 63
    use_vix: bool = True
    use_momentum: bool = True
    use_drawdown: bool = True
    min_history_bars: int = 220


def compute_features(
    data: pd.DataFrame,
    cfg: FeatureConfig | None = None,
) -> pd.DataFrame:
    """Compute all regime-detection features from raw market data.

    Parameters
    ----------
    data : DataFrame with columns [close, volume] and optionally [vix].
           Index must be DatetimeIndex sorted ascending.
    cfg  : Feature configuration; uses defaults if None.

    Returns
    -------
    DataFrame with one row per date, all NaN rows dropped.
    Each feature is computed with shift(1) where needed so that the value
    at time t reflects information known BEFORE the close at t.
    """
    if cfg is None:
        cfg = FeatureConfig()

    df = data.copy()

    # --- Returns ---------------------------------------------------------
    df["ret_1d"] = df["close"].pct_change()

    # --- Rolling volatility (annualised) ---------------------------------
    df["vol_20d"] = (
        df["ret_1d"].rolling(cfg.vol_window, min_periods=cfg.vol_window // 2).std()
        * np.sqrt(252)
    )

    # --- Trend: distance from long-term MA (in std devs) -----------------
    df["ma_long"] = df["close"].rolling(cfg.trend_window, min_periods=cfg.trend_window // 2).mean()
    df["trend_z"] = (df["close"] - df["ma_long"]) / (
        df["close"].rolling(cfg.trend_window, min_periods=cfg.trend_window // 2).std() + 1e-8
    )

    # --- Volume z-score (relative volume) --------------------------------
    df["vol_z"] = (
        df["volume"] - df["volume"].rolling(20, min_periods=10).mean()
    ) / (df["volume"].rolling(20, min_periods=10).std() + 1e-8)

    # --- VIX level -------------------------------------------------------
    if cfg.use_vix and "vix" in df.columns:
        df["vix_level"] = df["vix"]
        df["vix_change"] = df["vix"].pct_change()
    else:
        df["vix_level"] = float("nan")
        df["vix_change"] = float("nan")

    # --- Momentum --------------------------------------------------------
    if cfg.use_momentum:
        df["momentum"] = df["close"] / df["close"].shift(cfg.momentum_window) - 1

    # --- Drawdown from rolling 1-year peak (consistent window regardless of data length) ---
    if cfg.use_drawdown:
        rolling_peak = df["close"].rolling(252, min_periods=1).max()
        df["drawdown"] = df["close"] / rolling_peak - 1

    # Drop rows where core features are NaN (initial warm-up period)
    core_cols = ["ret_1d", "vol_20d", "trend_z"]
    df = df.dropna(subset=core_cols)

    return df


def build_feature_matrix(
    data: pd.DataFrame,
    cfg: FeatureConfig | None = None,
) -> tuple[np.ndarray, pd.DatetimeIndex, "StandardScaler"]:
    """Return (X, index) where X is the scaled feature matrix for HMM fitting.

    Only uses features that are fully available (no NaN in selected columns).
    """
    if cfg is None:
        cfg = FeatureConfig()

    feat_df = compute_features(data, cfg)

    # Select feature columns for HMM — exclude raw price / volume
    cols = ["ret_1d", "vol_20d", "trend_z", "vol_z"]
    if cfg.use_vix and "vix_level" in feat_df.columns:
        vix_ok = feat_df["vix_level"].notna().all()
        if vix_ok:
            cols += ["vix_level", "vix_change"]
    if cfg.use_momentum and "momentum" in feat_df.columns:
        cols.append("momentum")
    if cfg.use_drawdown and "drawdown" in feat_df.columns:
        cols.append("drawdown")

    # Drop any remaining NaN rows in selected columns
    X_df = feat_df[cols].dropna()
    index = X_df.index

    if len(X_df) < cfg.min_history_bars:
        log.warning(
            "Only %d bars available after feature computation; need %d for reliable HMM fit",
            len(X_df), cfg.min_history_bars,
        )
        raise ValueError(
            f"Insufficient training data: {len(X_df)} bars after feature computation, "
            f"need at least {cfg.min_history_bars}"
        )

    # Fit on the named DataFrame (not .values) so the scaler records
    # feature_names_in_ — this lets align_features_to_scaler() pad/select
    # missing columns by NAME later instead of by raw position.
    scaler = StandardScaler()
    X = scaler.fit_transform(X_df)
    return X, index, scaler


def align_features_to_scaler(
    X_df: pd.DataFrame,
    scaler: StandardScaler,
) -> np.ndarray:
    """Align a feature DataFrame to the scaler's expected columns, by NAME.

    For each column the scaler was trained on (``scaler.feature_names_in_``),
    use the input's value if that named column is present in ``X_df``,
    otherwise fall back to that column's training mean (so it standardises
    to ~0 / neutral instead of a spurious z-score). This is the single
    shared alignment path for both training-time inference
    (``build_feature_matrix_with_scaler``) and live single-bar updates
    (``regime/hmm_engine.py::_pad_features_to_scaler``) — it fixes the bug
    where a MIDDLE column missing (e.g. vix_level) shifted LATER columns
    (e.g. momentum, drawdown) into the wrong slots under purely positional
    padding.

    Falls back to positional alignment if the scaler has no
    ``feature_names_in_`` (e.g. fit directly on a bare ndarray) — this
    preserves behavior for scalers that were never fit on a named
    DataFrame.

    Returns a plain ndarray shaped (len(X_df), scaler.n_features_in_).
    """
    n_expected = scaler.n_features_in_
    feature_names = getattr(scaler, "feature_names_in_", None)

    if feature_names is None:
        # No column names recorded on the scaler — fall back to positional
        # padding/truncation (legacy behavior for bare-ndarray-fit scalers).
        n_have = X_df.shape[1]
        values = X_df.values if isinstance(X_df, pd.DataFrame) else np.asarray(X_df)
        if n_have < n_expected:
            means = np.tile(np.asarray(scaler.mean_)[n_have:n_expected], (len(X_df), 1))
            return np.hstack([values, means])
        return values[:, :n_expected]

    n_rows = len(X_df)
    out = np.empty((n_rows, n_expected), dtype=float)
    for j, name in enumerate(feature_names):
        if name in X_df.columns:
            out[:, j] = X_df[name].values
        else:
            out[:, j] = scaler.mean_[j]
    return out


def build_feature_matrix_with_scaler(
    data: pd.DataFrame,
    scaler: StandardScaler,
    cfg: FeatureConfig | None = None,
) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """Apply a pre-fitted scaler — used for out-of-sample inference.

    CRITICAL: Use the scaler fitted on training data only. Never refit
    the scaler on test/live data — that would introduce look-ahead bias.
    """
    if cfg is None:
        cfg = FeatureConfig()

    feat_df = compute_features(data, cfg)

    cols = ["ret_1d", "vol_20d", "trend_z", "vol_z"]
    if cfg.use_vix and "vix_level" in feat_df.columns and feat_df["vix_level"].notna().all():
        cols += ["vix_level", "vix_change"]
    if cfg.use_momentum and "momentum" in feat_df.columns:
        cols.append("momentum")
    if cfg.use_drawdown and "drawdown" in feat_df.columns:
        cols.append("drawdown")

    # Align to the columns the scaler was fitted on
    available = [c for c in cols if c in feat_df.columns]
    X_df = feat_df[available].dropna()
    index = X_df.index

    # Align by column NAME to the scaler's expected features (see
    # align_features_to_scaler doc for why this must not be positional).
    X_raw = align_features_to_scaler(X_df, scaler)

    feature_names = getattr(scaler, "feature_names_in_", None)
    if feature_names is not None:
        # Pass named columns through to transform() to avoid sklearn's
        # "X does not have valid feature names" warning (cosmetic only —
        # alignment already happened above).
        X_raw = pd.DataFrame(X_raw, columns=feature_names, index=index)
    X = scaler.transform(X_raw)
    return X, index
