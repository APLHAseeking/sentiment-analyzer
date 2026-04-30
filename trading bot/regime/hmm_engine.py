"""HMM-based regime detection engine.

Design principles:
1. NO LOOK-AHEAD BIAS: The model is fitted only on training data. For
   out-of-sample inference we use forward-only prediction (viterbi /
   posterior on the sequence seen so far).
2. MODEL SELECTION: We fit candidate HMMs with n_components in
   cfg.candidate_counts and choose the best by BIC (or AIC).
3. LABELING: Regimes are ordered by mean return so the labels are
   economically interpretable (lowest return = crash, highest = euphoria).
4. PERSISTENCE: The fitted model + scaler are saved to disk so they can
   be reloaded without refitting on restart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import joblib
import numpy as np
import pandas as pd
from scipy.special import logsumexp
from sklearn.preprocessing import StandardScaler

from regime.gaussian_hmm import GaussianHMM

from features.feature_pipeline import (
    FeatureConfig,
    build_feature_matrix,
    build_feature_matrix_with_scaler,
    compute_features,
)

log = logging.getLogger(__name__)


@dataclass
class FitResult:
    model: GaussianHMM
    scaler: StandardScaler
    n_regimes: int
    bic: float
    aic: float
    score: float                     # log-likelihood on training data
    label_map: list[str]             # model_state_index → regime name
    label_options: list[str]         # rank → human label (ascending return order)
    state_to_rank: dict[int, int]    # model_state → rank (0=worst)
    train_index: pd.DatetimeIndex
    train_regimes: np.ndarray        # ranked regime index per training bar


@dataclass
class RegimeState:
    date: str
    regime_index: int
    regime_label: str
    confidence: float                 # posterior probability of the predicted regime
    is_stable: bool
    n_regimes: int
    raw_posteriors: list[float]       # full posterior distribution over regimes
    transition_rate: float = 0.0    # fraction of last N bar-pairs where regime changed
    instability_score: float = 0.0  # same value, alias for callers reading it as a score


class HMMRegimeEngine:
    """Fit and query a Gaussian HMM for market regime classification."""

    def __init__(self, cfg: Any) -> None:
        """
        Parameters
        ----------
        cfg : system.config.RegimeConfig (or any object with matching attrs)
        """
        self._cfg = cfg
        self._result: FitResult | None = None
        self._recent_labels: list[int] = []   # sliding window for stability filter

        # Incremental inference state (populated by initialize_incremental)
        self._last_log_alpha: np.ndarray | None = None
        self._feature_cfg_cache: FeatureConfig | None = None
        self._data_tail: pd.DataFrame | None = None

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, data: pd.DataFrame, feature_cfg: FeatureConfig | None = None) -> FitResult:
        """Fit all candidate HMMs and select the best by BIC.

        Parameters
        ----------
        data : raw market data (from market_data.market_feed)
        feature_cfg : optional feature configuration

        This method MUST be called only on training data. Passing the full
        dataset here would leak future information into regime labels.
        """
        if feature_cfg is None:
            feature_cfg = FeatureConfig()

        X, index, scaler = build_feature_matrix(data, feature_cfg)

        if len(X) < feature_cfg.min_history_bars:
            raise ValueError(
                f"Insufficient training data: {len(X)} bars, need {feature_cfg.min_history_bars}"
            )

        best: FitResult | None = None
        for n in self._cfg.candidate_counts:
            try:
                result = self._fit_single(n, X, index, scaler)
                log.info(
                    "HMM n=%d | log-lik=%.1f | BIC=%.1f | AIC=%.1f",
                    n, result.score, result.bic, result.aic,
                )
                if best is None or self._is_better(result, best):
                    best = result
            except Exception as exc:
                log.warning("HMM n=%d fit failed: %s", n, exc)

        if best is None:
            raise RuntimeError("All HMM candidate fits failed")

        log.info(
            "Selected n=%d regimes by %s (BIC=%.1f). Labels: %s",
            best.n_regimes, self._cfg.selection_criterion, best.bic, best.label_map,
        )
        self._result = best
        self._recent_labels = []
        return best

    def _fit_single(
        self, n: int, X: np.ndarray, index: pd.DatetimeIndex, scaler: StandardScaler
    ) -> FitResult:
        model = GaussianHMM(
            n_components=n,
            covariance_type=self._cfg.covariance_type,
            n_iter=self._cfg.n_iter,
            random_state=self._cfg.random_state,
        )
        model.fit(X)

        # Viterbi path for labeling (training data only)
        train_regimes = model.predict(X)

        # Label regimes by ascending mean return so regime 0 = worst
        label_map = self._assign_labels(model, X, train_regimes, n)

        # Relabel the Viterbi path using the sorted order
        # The label_map gives us: sorted_index[k] = model's internal state
        # for the k-th regime in return order.
        # We need a mapping from model state → sorted position.
        mean_rets = np.array([X[train_regimes == s, 0].mean() if (train_regimes == s).any()
                               else 0.0 for s in range(n)])
        sorted_states = np.argsort(mean_rets)  # sorted_states[k] = model's state for rank k
        state_to_rank = {state: rank for rank, state in enumerate(sorted_states)}
        train_regimes_ranked = np.array([state_to_rank[s] for s in train_regimes])

        log_lik = model.score(X)
        n_params = n ** 2 + n * X.shape[1] + n * X.shape[1]  # approximate
        bic = -2 * log_lik + n_params * np.log(len(X))
        aic = -2 * log_lik + 2 * n_params

        label_options = self._cfg.label_maps.get(n)
        if label_options is None:
            label_options = [f"regime_{i}" for i in range(n)]

        return FitResult(
            model=model,
            scaler=scaler,
            n_regimes=n,
            bic=bic,
            aic=aic,
            score=log_lik,
            label_map=label_map,
            label_options=list(label_options),
            state_to_rank=state_to_rank,
            train_index=index,
            train_regimes=train_regimes_ranked,
        )

    def _assign_labels(
        self, model: GaussianHMM, X: np.ndarray, regimes: np.ndarray, n: int
    ) -> list[str]:
        """Assign human-readable labels ordered by mean return (ascending).

        Returns label_map where label_map[model_state] = human label string.
        """
        mean_rets = np.array([
            X[regimes == s, 0].mean() if (regimes == s).any() else 0.0
            for s in range(n)
        ])
        sorted_states = np.argsort(mean_rets)  # ascending: worst to best

        label_options = self._cfg.label_maps.get(n)
        if label_options is None:
            label_options = [f"regime_{i}" for i in range(n)]

        # label_map[model_state] = human label
        label_map: list[str] = [""] * n
        for rank, state in enumerate(sorted_states):
            label_map[state] = label_options[rank]
        return label_map

    def _is_better(self, candidate: FitResult, current_best: FitResult) -> bool:
        if self._cfg.selection_criterion == "aic":
            return candidate.aic < current_best.aic
        return candidate.bic < current_best.bic

    # ------------------------------------------------------------------
    # Inference (forward-only, no look-ahead)
    # ------------------------------------------------------------------

    def classify(
        self, data: pd.DataFrame, feature_cfg: FeatureConfig | None = None
    ) -> list[RegimeState]:
        """Classify regimes for the given data using the fitted model.

        IMPORTANT: data must end at or before the current bar. We use
        model.predict_proba() on the full sequence up to each bar,
        which is causal (forward-only) for the HMM — no future leakage.
        """
        if self._result is None:
            raise RuntimeError("Call fit() before classify()")

        if feature_cfg is None:
            feature_cfg = FeatureConfig()

        X, index = build_feature_matrix_with_scaler(
            data, self._result.scaler, feature_cfg
        )

        # Posterior probabilities — causal because HMM uses past sequence only
        posteriors = self._result.model.predict_proba(X)  # shape (T, n_regimes)
        viterbi = self._result.model.predict(X)

        n = self._result.n_regimes
        # Use the state→rank mapping computed and stored at fit time
        state_to_rank = self._result.state_to_rank
        label_options = self._result.label_options

        states: list[RegimeState] = []
        for i, (ts, raw_state) in enumerate(zip(index, viterbi)):
            rank = state_to_rank.get(int(raw_state), int(raw_state))
            label = label_options[rank] if rank < len(label_options) else str(rank)

            # Remap posteriors to rank order
            ranked_posteriors = [0.0] * n
            for s in range(n):
                r = state_to_rank.get(s, s)
                if r < n:
                    ranked_posteriors[r] += float(posteriors[i, s])

            confidence = ranked_posteriors[rank]
            is_stable, transition_rate, instability_score = self._compute_stability_metrics(rank)
            states.append(RegimeState(
                date=str(ts.date()),
                regime_index=rank,
                regime_label=label,
                confidence=confidence,
                is_stable=is_stable,
                n_regimes=n,
                raw_posteriors=ranked_posteriors,
                transition_rate=transition_rate,
                instability_score=instability_score,
            ))
        return states

    def current_regime(
        self, data: pd.DataFrame, feature_cfg: FeatureConfig | None = None
    ) -> RegimeState:
        """Return the regime for the latest bar in data."""
        states = self.classify(data, feature_cfg)
        if not states:
            raise RuntimeError("No regime states computed — insufficient data")
        state = states[-1]
        self._recent_labels.append(state.regime_index)
        # Keep a sliding window of min_stable_bars * 3
        window = self._cfg.min_stable_bars * 3
        if len(self._recent_labels) > window:
            self._recent_labels = self._recent_labels[-window:]
        return state

    def initialize_incremental(
        self,
        data: pd.DataFrame,
        feature_cfg: FeatureConfig | None = None,
    ) -> RegimeState:
        """Run full forward pass on historical data, caching the final alpha vector.

        Must be called once after fit() before using update_single(). Subsequent
        calls to update_single() are O(K²) per bar and fully causal.

        Parameters
        ----------
        data : historical market data (columns: close, volume, vix)
        feature_cfg : feature configuration; uses FeatureConfig defaults if None

        Returns the current regime state at the end of the data.
        """
        if self._result is None:
            raise RuntimeError("Call fit() before initialize_incremental()")
        if feature_cfg is None:
            feature_cfg = FeatureConfig()
        self._feature_cfg_cache = feature_cfg

        X, _ = build_feature_matrix_with_scaler(data, self._result.scaler, feature_cfg)
        if len(X) == 0:
            raise RuntimeError("No features computed — check data length and feature config")

        log_emis = self._result.model._log_emission(X)
        log_alpha = self._result.model._forward(log_emis)
        self._last_log_alpha = log_alpha[-1].copy()

        # Cache a rolling tail large enough for rolling-window features
        tail_bars = feature_cfg.trend_window * 2 + feature_cfg.vol_window
        self._data_tail = data.iloc[-tail_bars:].copy()

        return self.current_regime(data, feature_cfg)

    def update_single(
        self,
        new_bar: pd.DataFrame,
        date_str: str | None = None,
    ) -> RegimeState:
        """Classify one new bar using the cached forward state.

        Causal (filter-only): uses only the forward algorithm, no backward pass.
        O(K²) per bar instead of O(T·K²) for full re-classification.

        Requires initialize_incremental() to have been called first.

        Parameters
        ----------
        new_bar : single-row DataFrame with close, volume, vix columns
        date_str : date label for the returned RegimeState (uses today if None)
        """
        if self._result is None:
            raise RuntimeError("Call fit() before update_single()")
        if self._last_log_alpha is None or self._data_tail is None:
            raise RuntimeError(
                "Call initialize_incremental() before update_single()"
            )

        feature_cfg = self._feature_cfg_cache or FeatureConfig()

        # Extend the cached tail with the new bar for feature computation
        extended = pd.concat([self._data_tail, new_bar]).sort_index()
        extended = extended[~extended.index.duplicated(keep="last")]

        feat_df = compute_features(extended, feature_cfg)
        if feat_df.empty:
            raise RuntimeError(
                "Could not compute features for the new bar — insufficient tail data"
            )

        # Select the same feature columns as training
        cols = ["ret_1d", "vol_20d", "trend_z", "vol_z"]
        if feature_cfg.use_vix and "vix_level" in feat_df.columns:
            if feat_df["vix_level"].notna().all():
                cols += ["vix_level", "vix_change"]
        if feature_cfg.use_momentum and "momentum" in feat_df.columns:
            cols.append("momentum")
        if feature_cfg.use_drawdown and "drawdown" in feat_df.columns:
            cols.append("drawdown")

        available = [c for c in cols if c in feat_df.columns]
        last_row = feat_df[available].dropna().iloc[-1:].values  # (1, D)

        if last_row.shape[0] == 0:
            raise RuntimeError("New bar produced all-NaN features — tail too short")

        # Align to scaler's expected feature count
        n_expected = self._result.scaler.n_features_in_
        if last_row.shape[1] < n_expected:
            pad = np.zeros((1, n_expected - last_row.shape[1]))
            last_row = np.hstack([last_row, pad])
        else:
            last_row = last_row[:, :n_expected]
        obs_scaled = self._result.scaler.transform(last_row)[0]  # (D,)

        # One causal forward step
        new_log_alpha = self._result.model.forward_step(
            self._last_log_alpha, obs_scaled
        )
        self._last_log_alpha = new_log_alpha

        # Compute filtered state probabilities
        log_z = logsumexp(new_log_alpha)
        posteriors_raw = np.exp(new_log_alpha - log_z)  # (K,)

        # Map to ranked regime
        n = self._result.n_regimes
        state_to_rank = self._result.state_to_rank
        label_options = self._result.label_options
        ranked_posteriors = [0.0] * n
        for s in range(n):
            r = state_to_rank.get(s, s)
            if r < n:
                ranked_posteriors[r] += float(posteriors_raw[s])

        rank = int(np.argmax(ranked_posteriors))
        label = label_options[rank] if rank < len(label_options) else str(rank)
        confidence = ranked_posteriors[rank]

        # Update recent labels and data tail
        self._recent_labels.append(rank)
        window = self._cfg.min_stable_bars * 3
        if len(self._recent_labels) > window:
            self._recent_labels = self._recent_labels[-window:]
        tail_bars = feature_cfg.trend_window * 2 + feature_cfg.vol_window
        self._data_tail = extended.iloc[-tail_bars:].copy()

        is_stable, transition_rate, instability_score = self._compute_stability_metrics(rank)

        return RegimeState(
            date=date_str or date.today().isoformat(),
            regime_index=rank,
            regime_label=label,
            confidence=confidence,
            is_stable=is_stable,
            n_regimes=n,
            raw_posteriors=ranked_posteriors,
            transition_rate=transition_rate,
            instability_score=instability_score,
        )

    def rolling_refit(
        self,
        data: pd.DataFrame,
        train_window_bars: int = 1000,
        feature_cfg: FeatureConfig | None = None,
    ) -> FitResult:
        """Refit the HMM on the most recent train_window_bars of data.

        Use for scheduled model refreshes (e.g., monthly). After refitting,
        call initialize_incremental() again before the next update_single().

        Parameters
        ----------
        data : full market history; the tail of length train_window_bars is used
        train_window_bars : number of most-recent bars to use as the training window
        feature_cfg : feature configuration; uses FeatureConfig defaults if None
        """
        train_data = (
            data.iloc[-train_window_bars:] if len(data) > train_window_bars else data
        )
        result = self.fit(train_data, feature_cfg)
        # Invalidate incremental state — model has changed, cached alpha is stale
        self._last_log_alpha = None
        self._data_tail = None
        return result

    def _compute_stability_metrics(self, current_rank: int) -> tuple[bool, float, float]:
        """Compute stability state for the current regime rank.

        Returns
        -------
        is_stable : True if the regime has been current_rank for the last min_stable_bars
        transition_rate : fraction of consecutive bar-pairs in the window where regime changed
        instability_score : same as transition_rate (0.0 = stable, 1.0 = every bar switches)
        """
        window = self._cfg.min_stable_bars
        if len(self._recent_labels) < window:
            return False, 1.0, 1.0

        recent = self._recent_labels[-window:]
        is_stable = all(r == current_rank for r in recent)

        if len(recent) < 2:
            transition_rate = 0.0
        else:
            n_transitions = sum(
                1 for i in range(1, len(recent)) if recent[i] != recent[i - 1]
            )
            transition_rate = n_transitions / (len(recent) - 1)

        return is_stable, transition_rate, transition_rate

    def _check_stability(self, current_rank: int) -> bool:
        """Return True if the regime has been stable for min_stable_bars."""
        is_stable, _, _ = self._compute_stability_metrics(current_rank)
        return is_stable

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        if self._result is None:
            raise RuntimeError("Nothing to save — model not fitted")
        joblib.dump({"result": self._result, "recent_labels": self._recent_labels}, path)
        log.info("Regime model saved to %s", path)

    def load(self, path: str) -> None:
        payload = joblib.load(path)
        self._result = payload["result"]
        self._recent_labels = payload.get("recent_labels", [])
        log.info(
            "Regime model loaded from %s (n=%d, labels=%s)",
            path, self._result.n_regimes, self._result.label_map,
        )

    @property
    def is_fitted(self) -> bool:
        return self._result is not None

    @property
    def n_regimes(self) -> int:
        return self._result.n_regimes if self._result else 0

    @property
    def label_map(self) -> list[str]:
        return self._result.label_map if self._result else []
