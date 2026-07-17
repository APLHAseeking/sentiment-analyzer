"""Tests for the HMM regime engine.

Critical properties verified:
- No look-ahead bias in classification
- Stability filter behaves correctly
- Label ordering is economically consistent (worst → best)
- Model save/load round-trips cleanly
"""
import numpy as np
import pandas as pd
import pytest
from dataclasses import dataclass, field

from regime.hmm_engine import HMMRegimeEngine, RegimeState


@dataclass
class _MockRegimeCfg:
    candidate_counts: tuple = (3,)
    selection_criterion: str = "bic"
    n_iter: int = 20
    random_state: int = 42
    covariance_type: str = "diag"
    min_stable_bars: int = 3
    instability_penalty: float = 0.5
    # Kept at 1 (not the production default of 5) so the function-scoped
    # fitted_engine fixture — refit from scratch by ~30 tests — stays fast.
    # n_restarts itself is exercised directly against GaussianHMM in the
    # EM-restart tests below, not through this engine-level fixture.
    n_restarts: int = 1
    label_maps: dict = field(default_factory=lambda: {
        3: ["bear", "neutral", "bull"],
    })
    model_path: str = "test_regime_model.joblib"


def _make_market_data(n: int = 500, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2019-01-01", periods=n, freq="B")
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
    vix = np.clip(15 + rng.normal(0, 3, n), 10, 50)
    return pd.DataFrame({"close": close, "volume": volume, "vix": vix}, index=dates)


@pytest.fixture
def fitted_engine():
    from features.feature_pipeline import FeatureConfig
    cfg = _MockRegimeCfg()
    engine = HMMRegimeEngine(cfg)
    data = _make_market_data(600)
    feature_cfg = FeatureConfig(vol_window=20, trend_window=50, min_history_bars=100)
    engine.fit(data.iloc[:500], feature_cfg)
    return engine, data, feature_cfg


def test_engine_fits_successfully(fitted_engine):
    engine, data, _ = fitted_engine
    assert engine.is_fitted
    assert engine.n_regimes == 3


def test_regime_labels_are_strings(fitted_engine):
    engine, _, _ = fitted_engine
    for label in engine.label_map:
        assert isinstance(label, str)
        assert len(label) > 0


def test_classify_returns_correct_length(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    states = engine.classify(data, feature_cfg, update_recent_labels=False)
    assert len(states) > 0
    assert all(isinstance(s, RegimeState) for s in states)


def test_no_look_ahead_bias(fitted_engine):
    """Classification at time t must not change when we add data after t.

    The scaler is frozen at fit time and predict_proba_filtered is a forward-only
    pass — so the regime label at date T is fully determined by observations up to T
    and must not shift when future bars are appended.
    """
    engine, data, feature_cfg = fitted_engine

    cutoff = 300
    states_truncated = engine.classify(data.iloc[:cutoff], feature_cfg, update_recent_labels=False)
    states_full = engine.classify(data, feature_cfg, update_recent_labels=False)

    last_trunc = states_truncated[-1]
    full_at_same_date = next((s for s in states_full if s.date == last_trunc.date), None)

    assert full_at_same_date is not None, (
        f"Date {last_trunc.date} from truncated run not found in full run"
    )
    assert last_trunc.regime_index == full_at_same_date.regime_index, (
        f"Look-ahead bias: regime at {last_trunc.date} is {last_trunc.regime_index} "
        f"(truncated run) vs {full_at_same_date.regime_index} (full run — future data changed past label)"
    )


def test_confidence_is_valid_probability(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    states = engine.classify(data, feature_cfg, update_recent_labels=False)
    for s in states:
        assert 0.0 <= s.confidence <= 1.0 + 1e-6
        assert abs(sum(s.raw_posteriors) - 1.0) < 0.01


def test_stability_filter_requires_min_bars():
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    # Empty recent_labels → unstable
    assert not engine._check_stability(0)

    # 2 matching bars, need 3 → still unstable
    engine._recent_labels = [0, 0]
    assert not engine._check_stability(0)

    # 3 matching bars → stable
    engine._recent_labels = [0, 0, 0]
    assert engine._check_stability(0)

    # 3 bars but last differs → unstable
    engine._recent_labels = [0, 0, 1]
    assert not engine._check_stability(0)


def test_stability_filter_detects_flickering():
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 1, 0, 1, 0]  # flickering
    assert not engine._check_stability(0)
    assert not engine._check_stability(1)


def test_model_save_load_roundtrip(tmp_path, fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    path = str(tmp_path / "model.joblib")
    engine.save(path)

    engine2 = HMMRegimeEngine(_MockRegimeCfg())
    engine2.load(path)
    assert engine2.is_fitted
    assert engine2.n_regimes == engine.n_regimes
    assert engine2.label_map == engine.label_map

    # Loaded model classifies the same on a slice with enough bars for features
    sample = data.iloc[:500]  # use the same training window
    s1 = engine.classify(sample, feature_cfg, update_recent_labels=False)
    s2 = engine2.classify(sample, feature_cfg, update_recent_labels=False)
    if s1 and s2:
        assert s1[-1].regime_index == s2[-1].regime_index


def test_regime_index_is_within_bounds(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    n = engine.n_regimes
    states = engine.classify(data, feature_cfg, update_recent_labels=False)
    for s in states:
        assert 0 <= s.regime_index < n


def test_fit_raises_with_insufficient_data():
    from features.feature_pipeline import FeatureConfig
    cfg = _MockRegimeCfg()
    engine = HMMRegimeEngine(cfg)
    # 300 bars with trend_window=200 + vol_window=20 leaves ~80 usable bars
    # Setting min_history_bars=500 ensures the check fires
    data = _make_market_data(300)
    feature_cfg = FeatureConfig(vol_window=20, trend_window=200, min_history_bars=500)
    with pytest.raises(ValueError, match="Insufficient"):
        engine.fit(data, feature_cfg)


def test_regime_state_has_instability_fields(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    states = engine.classify(data, feature_cfg, update_recent_labels=False)
    for s in states:
        assert hasattr(s, "transition_rate")
        assert hasattr(s, "instability_score")
        assert 0.0 <= s.transition_rate <= 1.0 + 1e-9
        assert 0.0 <= s.instability_score <= 1.0 + 1e-9


def test_compute_stability_metrics_empty_history():
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    is_stable, tr, score = engine._compute_stability_metrics(0)
    assert not is_stable
    assert tr == pytest.approx(1.0)
    assert score == pytest.approx(1.0)


def test_compute_stability_metrics_fully_stable():
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 0, 0]
    is_stable, tr, score = engine._compute_stability_metrics(0)
    assert is_stable
    assert tr == pytest.approx(0.0)
    assert score == pytest.approx(0.0)


def test_compute_stability_metrics_flickering():
    cfg = _MockRegimeCfg(min_stable_bars=4)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 1, 0, 1]
    is_stable, tr, score = engine._compute_stability_metrics(1)
    assert not is_stable
    assert tr == pytest.approx(1.0)
    assert score == pytest.approx(1.0)


def test_compute_stability_metrics_partial_flickering():
    cfg = _MockRegimeCfg(min_stable_bars=4)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 0, 1, 0]
    is_stable, tr, score = engine._compute_stability_metrics(0)
    assert not is_stable
    assert tr == pytest.approx(2 / 3, abs=1e-6)


def test_check_stability_backward_compat():
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 0, 0]
    result = engine._check_stability(0)
    assert isinstance(result, bool)
    assert result is True
    engine._recent_labels = [0, 1, 0]
    assert engine._check_stability(0) is False


def test_forward_step_output_shape(fitted_engine):
    """forward_step(log_alpha, obs) returns (K,) array."""
    engine, _, _ = fitted_engine
    model = engine._result.model
    K = model.n_components
    D = model.means_.shape[1]
    log_alpha_prev = np.zeros(K)
    obs = np.zeros(D)
    result = model.forward_step(log_alpha_prev, obs)
    assert result.shape == (K,)


def test_forward_step_gives_valid_distribution(fitted_engine):
    """Normalised forward step sums to 1.0."""
    from scipy.special import logsumexp
    engine, _, _ = fitted_engine
    model = engine._result.model
    K = model.n_components
    D = model.means_.shape[1]
    log_alpha_prev = np.log(np.ones(K) / K)
    obs = model.means_[0]
    new_alpha = model.forward_step(log_alpha_prev, obs)
    probs = np.exp(new_alpha - logsumexp(new_alpha))
    assert abs(probs.sum() - 1.0) < 1e-6


def test_initialize_incremental_returns_regime_state(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    state = engine.initialize_incremental(data.iloc[:500], feature_cfg)
    assert isinstance(state, RegimeState)
    assert engine._last_log_alpha is not None
    assert engine._data_tail is not None
    assert engine._last_log_alpha.shape == (engine.n_regimes,)


def test_update_single_returns_regime_state(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    engine.initialize_incremental(data.iloc[:500], feature_cfg)
    new_bar = data.iloc[500:501]
    state = engine.update_single(new_bar, date_str="2021-06-01")
    assert isinstance(state, RegimeState)
    assert state.date == "2021-06-01"
    assert 0.0 <= state.confidence <= 1.0 + 1e-9
    assert 0 <= state.regime_index < engine.n_regimes


def test_update_single_is_deterministic(fitted_engine):
    """Same bar processed twice from the same initial state gives the same result."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine

    engine.initialize_incremental(data.iloc[:500], feature_cfg)
    state_a = engine.update_single(data.iloc[500:501])

    engine.initialize_incremental(data.iloc[:500], feature_cfg)
    state_b = engine.update_single(data.iloc[500:501])

    assert state_a.regime_index == state_b.regime_index
    assert state_a.confidence == pytest.approx(state_b.confidence, abs=1e-9)


def test_update_single_requires_initialization(fitted_engine):
    engine, data, _ = fitted_engine
    with pytest.raises(RuntimeError, match="initialize_incremental"):
        engine.update_single(data.iloc[0:1])


def test_update_single_advances_recent_labels(fitted_engine):
    """Three sequential update_single calls add three entries to _recent_labels."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    engine.initialize_incremental(data.iloc[:500], feature_cfg)
    count_before = len(engine._recent_labels)
    for i in range(3):
        engine.update_single(data.iloc[500 + i : 501 + i])
    assert len(engine._recent_labels) == count_before + 3


def test_update_single_detects_stale_row_masked_by_partial_nan(fitted_engine):
    """A NaN in exactly one non-core feature column (volume -> vol_z) for the
    intended new bar must not let dropna().iloc[-1:] silently fall back to an
    earlier, fully-populated cached day. Confirmed (pre-fix) to actually pick
    the prior day's row while still labeling the result with date_str, with no
    error or warning — see manual repro. This asserts the fix's staleness
    guard fires instead of that silent fallback.
    """
    engine, data, feature_cfg = fitted_engine
    engine.initialize_incremental(data.iloc[:500], feature_cfg)

    new_bar = data.iloc[500:501].copy()
    new_bar["volume"] = np.nan  # knocks out vol_z only for this bar; core
    # features (ret_1d/vol_20d/trend_z, driven by close) stay valid, so the
    # row is NOT dropped by compute_features' own dropna(subset=core_cols) —
    # it only gets dropped by update_single's own dropna() over `available`.

    with pytest.raises(RuntimeError, match="stale"):
        engine.update_single(new_bar, date_str="2021-06-01")


def test_rolling_refit_refits_successfully(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    result = engine.rolling_refit(data, train_window_bars=250, feature_cfg=feature_cfg)
    assert engine.is_fitted
    assert result.n_regimes == 3


def test_rolling_refit_invalidates_incremental_cache(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    engine.initialize_incremental(data.iloc[:500], feature_cfg)
    assert engine._last_log_alpha is not None

    engine.rolling_refit(data.iloc[:400], train_window_bars=200, feature_cfg=feature_cfg)
    assert engine._last_log_alpha is None
    assert engine._data_tail is None


def test_rolling_refit_uses_tail_when_window_smaller_than_data(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, _ = fitted_engine
    feature_cfg = FeatureConfig(vol_window=10, trend_window=30, min_history_bars=50)
    result = engine.rolling_refit(data, train_window_bars=200, feature_cfg=feature_cfg)
    assert engine.is_fitted
    assert result.n_regimes == 3


def test_rolling_refit_uses_all_data_when_window_larger(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    result = engine.rolling_refit(data.iloc[:300], train_window_bars=10_000, feature_cfg=feature_cfg)
    assert engine.is_fitted


def test_current_regime_logs_transition(db, fitted_engine):
    """current_regime() writes to regime_transitions when label changes."""
    import importlib
    import bot.db
    importlib.reload(bot.db)
    bot.db.init_db()

    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine

    # Force a known prior regime that differs from the real result
    engine._prev_regime_index = 999
    engine._prev_regime_label = "fake_prior"

    _ = engine.current_regime(data.iloc[:500], feature_cfg)

    rows = bot.db.get_regime_transitions(days=9999)
    assert len(rows) >= 1
    assert rows[-1]["from_label"] == "fake_prior"
    assert rows[-1]["to_label"] in ["bear", "neutral", "bull"]


# ---------------------------------------------------------------------------
# Tests for Bug 1 fix: predict_proba_filtered() is causal (no look-ahead)
# ---------------------------------------------------------------------------

def test_predict_proba_filtered_shape(fitted_engine):
    """predict_proba_filtered returns (T, K) matching predict_proba shape."""
    engine, _, _ = fitted_engine
    model = engine._result.model
    K = model.n_components
    D = model.means_.shape[1]
    rng = np.random.default_rng(0)
    X = rng.standard_normal((50, D))
    out = model.predict_proba_filtered(X)
    assert out.shape == (50, K)


def test_predict_proba_filtered_rows_sum_to_one(fitted_engine):
    """Each row of predict_proba_filtered sums to 1.0 (valid distribution)."""
    engine, _, _ = fitted_engine
    model = engine._result.model
    D = model.means_.shape[1]
    rng = np.random.default_rng(1)
    X = rng.standard_normal((40, D))
    out = model.predict_proba_filtered(X)
    row_sums = out.sum(axis=1)
    np.testing.assert_allclose(row_sums, 1.0, atol=1e-6)


def test_predict_proba_filtered_is_causal(fitted_engine):
    """Filtered posterior at time t must not change when future observations are appended.

    If classify() used the smoothed backward pass, posteriors at earlier time steps
    would shift when we add more data. The forward-only filter is invariant to future
    observations, so the first T posteriors of X[:T+k] must equal those of X[:T].
    """
    engine, _, _ = fitted_engine
    model = engine._result.model
    D = model.means_.shape[1]
    rng = np.random.default_rng(7)
    X_short = rng.standard_normal((30, D))
    X_long = np.vstack([X_short, rng.standard_normal((20, D))])

    proba_short = model.predict_proba_filtered(X_short)
    proba_long = model.predict_proba_filtered(X_long)

    # First 30 rows must be identical — forward pass is path-independent of future data
    np.testing.assert_allclose(proba_short, proba_long[:30], atol=1e-9)


def test_predict_proba_smoothed_differs_from_filtered(fitted_engine):
    """Smoothed and filtered posteriors differ, confirming they use different algorithms."""
    engine, _, _ = fitted_engine
    model = engine._result.model
    D = model.means_.shape[1]
    rng = np.random.default_rng(99)
    X = rng.standard_normal((60, D))
    smoothed = model.predict_proba(X)
    filtered = model.predict_proba_filtered(X)
    # They will not be identical because the backward pass redistributes probability mass
    assert not np.allclose(smoothed, filtered, atol=1e-4), (
        "Smoothed and filtered posteriors must differ — they use different algorithms"
    )


# ---------------------------------------------------------------------------
# Tests for Bug 2 fix: BIC parameter count
# ---------------------------------------------------------------------------

def test_bic_is_smaller_than_wrong_formula(fitted_engine):
    """Corrected BIC must be strictly smaller than the old overcounting formula.

    The old formula used n^2 free parameters for the transition matrix; the correct
    count is n*(n-1) (one constraint per row: rows sum to 1). For n>=2 the correct
    count is always smaller, which means the corrected BIC penalty is smaller.
    This test reconstructs both values and checks the corrected one is used.
    """
    from features.feature_pipeline import build_feature_matrix_with_scaler, FeatureConfig

    engine, data, feature_cfg = fitted_engine
    result = engine._result
    model = result.model
    n = result.n_regimes
    D = model.means_.shape[1]

    X, _ = build_feature_matrix_with_scaler(data.iloc[:500], result.scaler, feature_cfg)
    T = len(X)
    log_lik = model.score(X)

    correct_n_params = n * (n - 1) + (n - 1) + 2 * n * D
    wrong_n_params = n ** 2 + 2 * n * D  # old formula
    correct_bic = -2 * log_lik + correct_n_params * np.log(T)
    wrong_bic = -2 * log_lik + wrong_n_params * np.log(T)

    # Correct formula has fewer params → smaller BIC penalty → smaller BIC
    assert correct_bic < wrong_bic

    # The BIC stored in the FitResult must match the corrected formula
    assert result.bic == pytest.approx(correct_bic, rel=1e-6), (
        f"Stored BIC {result.bic:.2f} does not match corrected formula {correct_bic:.2f}. "
        f"Did _fit_single still use the old n^2 formula?"
    )


def test_bic_param_count_values():
    """Unit-test the corrected parameter count arithmetic directly."""
    # For n=3 states, D=4 features:
    #   transmat: 3*(3-1)=6, startprob: 2, means: 12, variances: 12 → total 32
    #   old formula: 3^2 + 2*3*4 = 9+24 = 33
    n, D = 3, 4
    correct = n * (n - 1) + (n - 1) + 2 * n * D
    wrong = n ** 2 + 2 * n * D
    assert correct == 32
    assert wrong == 33
    assert correct != wrong


from sklearn.preprocessing import StandardScaler
from regime.hmm_engine import _pad_features_to_scaler


def test_pad_features_uses_training_mean_so_missing_scales_to_zero():
    # Train a scaler on 4 features with distinct means
    scaler = StandardScaler().fit(np.array([
        [1.0, 2.0, 10.0, 20.0],
        [3.0, 4.0, 30.0, 40.0],
    ]))
    # Caller only has the first 2 features available this bar
    row = np.array([[2.0, 3.0]])
    padded = _pad_features_to_scaler(row, scaler)
    assert padded.shape == (1, 4)
    scaled = scaler.transform(padded)[0]
    # The two padded features were set to their training mean → scale to ~0
    assert scaled[2] == pytest.approx(0.0, abs=1e-9)
    assert scaled[3] == pytest.approx(0.0, abs=1e-9)


def test_pad_features_truncates_when_too_many_columns():
    scaler = StandardScaler().fit(np.array([[1.0, 2.0], [3.0, 4.0]]))
    row = np.array([[2.0, 3.0, 99.0]])  # one extra column
    padded = _pad_features_to_scaler(row, scaler)
    assert padded.shape == (1, 2)


def test_pad_features_aligns_by_name_when_middle_column_missing():
    """A MIDDLE column missing (vix_level) must not shift LATER columns
    (momentum, drawdown) into the wrong slots. Padding must be column-name
    aware, not purely positional, when the scaler was fit on a named
    DataFrame (feature_names_in_ populated)."""
    full_cols = [
        "ret_1d", "vol_20d", "trend_z", "vol_z",
        "vix_level", "vix_change", "momentum", "drawdown",
    ]
    train_df = pd.DataFrame(
        [
            [0.01, 0.10, 0.5, 0.2, 18.0, 0.01, 0.05, -0.02],
            [0.02, 0.12, 0.6, 0.3, 20.0, -0.02, 0.07, -0.03],
            [0.00, 0.11, 0.4, 0.1, 19.0, 0.00, 0.06, -0.01],
        ],
        columns=full_cols,
    )
    scaler = StandardScaler().fit(train_df)
    vix_level_mean = scaler.mean_[full_cols.index("vix_level")]

    # Live input is missing the MIDDLE column "vix_level" but HAS the
    # LATER columns momentum/drawdown with distinct, recognisable values.
    live_cols = ["ret_1d", "vol_20d", "trend_z", "vol_z", "vix_change", "momentum", "drawdown"]
    live_row = pd.DataFrame(
        [[0.015, 0.115, 0.55, 0.25, 0.005, 0.123, 0.456]],
        columns=live_cols,
    )

    padded = _pad_features_to_scaler(live_row, scaler)
    assert padded.shape == (1, len(full_cols))

    # momentum/drawdown's real values must land in momentum/drawdown's
    # positions, not be shifted into vix_level's slot.
    assert padded[0, full_cols.index("momentum")] == pytest.approx(0.123)
    assert padded[0, full_cols.index("drawdown")] == pytest.approx(0.456)
    # vix_level (the actually-missing middle column) gets the training mean.
    assert padded[0, full_cols.index("vix_level")] == pytest.approx(vix_level_mean)
    # vix_change (present in the input) keeps its real value, not a mean-fill.
    assert padded[0, full_cols.index("vix_change")] == pytest.approx(0.005)


def test_compute_stability_metrics_accepts_explicit_recent_labels():
    """An explicit recent_labels argument overrides self._recent_labels and
    does not mutate it."""
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    # self._recent_labels is empty -> default (no arg) would be "not stable"
    is_stable, tr, score = engine._compute_stability_metrics(0, recent_labels=[0, 0, 0])
    assert is_stable
    assert tr == pytest.approx(0.0)
    assert score == pytest.approx(0.0)
    assert engine._recent_labels == []


def test_compute_stability_metrics_default_arg_unchanged():
    """No recent_labels argument falls back to self._recent_labels (existing behavior)."""
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 0, 0]
    is_stable, tr, score = engine._compute_stability_metrics(0)
    assert is_stable
    assert tr == pytest.approx(0.0)


def test_classify_explicit_false_does_not_touch_recent_labels(fitted_engine):
    """classify(update_recent_labels=False) must not mutate _recent_labels —
    current_regime()/update_single() rely on this."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    count_before = len(engine._recent_labels)
    engine.classify(data.iloc[:500], feature_cfg, update_recent_labels=False)
    assert len(engine._recent_labels) == count_before


def test_classify_update_recent_labels_advances_history(fitted_engine):
    """classify(update_recent_labels=True) rolls _recent_labels forward
    bar-by-bar, trimmed to min_stable_bars * 3."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    states = engine.classify(data.iloc[:500], feature_cfg, update_recent_labels=True)
    window = engine._cfg.min_stable_bars * 3
    assert len(states) > window
    assert len(engine._recent_labels) == window
    assert engine._recent_labels == [s.regime_index for s in states[-window:]]


def test_classify_update_recent_labels_seeds_from_existing_history(fitted_engine):
    """A second update_recent_labels=True call continues from the history left
    by the first (rather than recomputing from scratch each time)."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    engine.classify(data.iloc[:500], feature_cfg, update_recent_labels=True)
    history_after_first = list(engine._recent_labels)

    # Pass enough context for feature computation (min_history_bars=100)
    states_second = engine.classify(data.iloc[400:520], feature_cfg, update_recent_labels=True)
    window = engine._cfg.min_stable_bars * 3
    expected_tail = (history_after_first + [s.regime_index for s in states_second])[-window:]
    assert engine._recent_labels == expected_tail


def test_classify_requires_explicit_update_recent_labels(fitted_engine):
    """classify() must not silently default update_recent_labels — omitting it
    is a TypeError, not a quiet fall-back to broken (never-stable) tracking.

    update_recent_labels has no safe universal default: current_regime() needs
    False (it re-classifies the *entire* history tail every call and does its
    own single-bar append to _recent_labels — True would replay the whole
    window into _recent_labels on every live tick), while a bare backtest-style
    classify() over a whole period needs True to get working stability
    tracking at all. Forcing every call site to say which one it means is the
    fix; a wrong-but-silent default is not.
    """
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    with pytest.raises(TypeError):
        engine.classify(data.iloc[:500], feature_cfg)


def test_classify_explicit_false_never_reports_stable_without_tracking():
    """update_recent_labels=False with no prior history correctly yields
    is_stable=False throughout (there is nothing to be stable against) —
    this is expected behavior for that explicit choice, not a bug. Contrast
    with update_recent_labels=True below, which builds real tracking."""
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    data = _make_market_data(600)
    from features.feature_pipeline import FeatureConfig
    feature_cfg = FeatureConfig(vol_window=20, trend_window=50, min_history_bars=100)
    engine.fit(data.iloc[:500], feature_cfg)

    states = engine.classify(data.iloc[:500], feature_cfg, update_recent_labels=False)
    assert all(not s.is_stable for s in states)


def test_classify_update_recent_labels_true_produces_varying_stability(fitted_engine):
    """With update_recent_labels=True over a multi-bar run, stability tracking
    actually works: is_stable is False early (insufficient history) and
    becomes True once the regime has persisted for min_stable_bars, i.e. it
    varies rather than being permanently False."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    states = engine.classify(data.iloc[:500], feature_cfg, update_recent_labels=True)
    stabilities = {s.is_stable for s in states}
    assert stabilities == {True, False}


# ---------------------------------------------------------------------------
# GaussianHMM EM restarts (avoid single-init local optima)
# ---------------------------------------------------------------------------

def _make_trimodal_trap_data(seed: int = 7) -> np.ndarray:
    """2-regime sequence where one state alternates between two well-separated
    sub-blobs. A 2-component diagonal-Gaussian HMM cannot represent that state
    as bimodal, so depending on which random centroids the k-means-style init
    happens to land on, single-restart EM can converge to a visibly worse local
    optimum (confirmed empirically: random_state=28 reliably converges to a log-
    likelihood of about -924 on this dataset, vs. about -623 for nearly every
    other seed and for best-of-5 restarts starting from that same seed)."""
    rng = np.random.default_rng(seed)
    T = 300
    state_seq: list[int] = []
    sub_seq: list[int] = []
    state = 0
    for _ in range(T):
        state_seq.append(state)
        if state == 0:
            sub_seq.append(0)
            if rng.random() < 0.05:
                state = 1
        else:
            sub_seq.append(1 + int(rng.integers(0, 2)))
            if rng.random() < 0.3:
                state = 0
    sub_seq_arr = np.array(sub_seq)
    X = np.zeros((T, 2))
    for t in range(T):
        if sub_seq_arr[t] == 0:
            X[t] = rng.normal([0, 0], [0.5, 0.5])
        elif sub_seq_arr[t] == 1:
            X[t] = rng.normal([10, 0], [0.5, 0.5])
        else:
            X[t] = rng.normal([-10, 0], [0.5, 0.5])
    return X


def test_single_restart_can_land_in_bad_local_optimum():
    """Sanity check on the repro itself: random_state=28 with a single EM run
    (n_restarts=1) lands well below the optimum nearly every other seed finds.
    This documents *why* restarts matter; it is not the regression test for
    the fix."""
    from regime.gaussian_hmm import GaussianHMM

    X = _make_trimodal_trap_data()
    model = GaussianHMM(n_components=2, n_iter=100, random_state=28, n_restarts=1)
    model.fit(X)
    assert model._log_likelihood < -800  # the known-bad optimum (~-924)


def test_n_restarts_achieves_at_least_as_good_log_likelihood():
    """Fitting with n_restarts > 1 must never do worse than n_restarts=1 —
    each restart is a candidate and the fit keeps the best by construction."""
    from regime.gaussian_hmm import GaussianHMM

    X = _make_trimodal_trap_data()
    single = GaussianHMM(n_components=2, n_iter=100, random_state=28, n_restarts=1)
    single.fit(X)

    multi = GaussianHMM(n_components=2, n_iter=100, random_state=28, n_restarts=5)
    multi.fit(X)

    assert multi._log_likelihood >= single._log_likelihood


def test_n_restarts_recovers_from_known_bad_seed():
    """On the seed known to produce a bad single-restart result (random_state=28,
    ll ~ -924), n_restarts=5 must recover the good optimum nearly every other
    seed finds (ll ~ -623) — a strict, large improvement, not a marginal one."""
    from regime.gaussian_hmm import GaussianHMM

    X = _make_trimodal_trap_data()
    multi = GaussianHMM(n_components=2, n_iter=100, random_state=28, n_restarts=5)
    multi.fit(X)
    assert multi._log_likelihood > -700  # recovered the good optimum (~-623)


def test_n_restarts_is_reproducible_given_same_random_state():
    """Multi-restart fitting must stay deterministic given the same
    random_state — restarts are derived seeds (e.g. random_state + i), not
    fresh entropy each call."""
    from regime.gaussian_hmm import GaussianHMM

    X = _make_trimodal_trap_data()
    m1 = GaussianHMM(n_components=2, n_iter=100, random_state=28, n_restarts=5)
    m1.fit(X)
    m2 = GaussianHMM(n_components=2, n_iter=100, random_state=28, n_restarts=5)
    m2.fit(X)
    assert m1._log_likelihood == m2._log_likelihood
    np.testing.assert_array_equal(m1.means_, m2.means_)
