# Regime Detection Engine Enhancements

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing HMM regime engine with instability/flickering metrics, true incremental forward-only inference, rolling refit, and regime transition logging.

**Architecture:** The regime engine (`regime/hmm_engine.py` + `regime/gaussian_hmm.py`) is already architecturally complete with BIC/AIC selection, economic labeling, basic stability, and no-look-ahead inference. This plan adds four missing capabilities as targeted additions — no structural changes. `RegimeState` gets two new optional fields; `HMMRegimeEngine` gets four new methods; `GaussianHMM` gets one helper method; `bot/db.py` gets a new table and two functions.

**Tech Stack:** Python 3.11+, NumPy, SciPy, pandas, scikit-learn, SQLite (all already in requirements.txt)

---

## Existing Code Reference

Before implementing, read these files (they are NOT modified except where the plan says):
- `regime/hmm_engine.py` — `HMMRegimeEngine`, `RegimeState`, `FitResult`
- `regime/gaussian_hmm.py` — `GaussianHMM` with `_log_emission`, `_forward`, `_backward`, `_viterbi`
- `features/feature_pipeline.py` — `compute_features`, `build_feature_matrix_with_scaler`, `FeatureConfig`
- `bot/db.py` — `get_conn`, `_SCHEMA`, existing table definitions

Key existing types (do not redefine):
```python
# regime/hmm_engine.py — existing
@dataclass
class FitResult:
    model: GaussianHMM
    scaler: StandardScaler
    n_regimes: int
    bic: float
    aic: float
    score: float
    label_map: list[str]
    label_options: list[str]
    state_to_rank: dict[int, int]
    train_index: pd.DatetimeIndex
    train_regimes: np.ndarray

@dataclass
class RegimeState:  # EXISTING — Task 1 adds two optional fields
    date: str
    regime_index: int
    regime_label: str
    confidence: float
    is_stable: bool
    n_regimes: int
    raw_posteriors: list[float]
```

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `regime/hmm_engine.py` | **MODIFY** | Add `transition_rate`/`instability_score` to `RegimeState`; add `_compute_stability_metrics()`; add `initialize_incremental()`, `update_single()`, `rolling_refit()`; wire transition logging in `current_regime()` |
| `regime/gaussian_hmm.py` | **MODIFY** | Add `forward_step()` method |
| `bot/db.py` | **MODIFY** | Add `regime_transitions` table + `log_regime_transition()` + `get_regime_transitions()` |
| `tests/test_regime.py` | **MODIFY** | Add tests for all new functionality |
| `tests/test_db.py` | **MODIFY** | Add tests for `log_regime_transition` and `get_regime_transitions` |

---

## Task 1: Instability Metrics on RegimeState

**Files:**
- Modify: `regime/hmm_engine.py`
- Modify: `tests/test_regime.py`

Add `transition_rate: float` and `instability_score: float` to `RegimeState`. Replace `_check_stability` with `_compute_stability_metrics` that returns all three values. Keep `_check_stability` as a backward-compatible wrapper so existing tests keep passing.

`transition_rate` = fraction of consecutive pairs in the last `min_stable_bars` window where the regime changed (0.0 = perfectly stable, 1.0 = every bar switched). `instability_score` is the same value, named semantically for callers that don't want to think about rates.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_regime.py`:

```python
def test_regime_state_has_instability_fields(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    states = engine.classify(data, feature_cfg)
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
    engine._recent_labels = [0, 1, 0, 1]  # 3 transitions in 3 consecutive pairs → rate = 1.0
    is_stable, tr, score = engine._compute_stability_metrics(1)
    assert not is_stable
    assert tr == pytest.approx(1.0)
    assert score == pytest.approx(1.0)


def test_compute_stability_metrics_partial_flickering():
    cfg = _MockRegimeCfg(min_stable_bars=4)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 0, 1, 0]  # 2 transitions in 3 pairs → rate = 2/3
    is_stable, tr, score = engine._compute_stability_metrics(0)
    assert not is_stable
    assert tr == pytest.approx(2 / 3, abs=1e-6)


def test_check_stability_backward_compat():
    """_check_stability must still return a bool (existing tests rely on it)."""
    cfg = _MockRegimeCfg(min_stable_bars=3)
    engine = HMMRegimeEngine(cfg)
    engine._recent_labels = [0, 0, 0]
    result = engine._check_stability(0)
    assert isinstance(result, bool)
    assert result is True
    engine._recent_labels = [0, 1, 0]
    assert engine._check_stability(0) is False
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_regime.py::test_regime_state_has_instability_fields tests/test_regime.py::test_compute_stability_metrics_empty_history tests/test_regime.py::test_compute_stability_metrics_fully_stable -v 2>&1 | tail -15
```

Expected: `AttributeError: 'RegimeState' has no field 'transition_rate'` or `AttributeError: 'HMMRegimeEngine' object has no attribute '_compute_stability_metrics'`.

- [ ] **Step 3: Add `transition_rate` and `instability_score` to `RegimeState`**

In `regime/hmm_engine.py`, add two optional fields to the existing `RegimeState` dataclass (after `raw_posteriors`):

```python
@dataclass
class RegimeState:
    date: str
    regime_index: int
    regime_label: str
    confidence: float
    is_stable: bool
    n_regimes: int
    raw_posteriors: list[float]
    transition_rate: float = 0.0    # fraction of last N bar-pairs where regime changed
    instability_score: float = 0.0  # same value, alias for callers reading it as a score
```

- [ ] **Step 4: Add `_compute_stability_metrics` to `HMMRegimeEngine`**

In `regime/hmm_engine.py`, add this method to `HMMRegimeEngine` (insert after `_check_stability`):

```python
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
```

- [ ] **Step 5: Update `_check_stability` to delegate (backward compat)**

Replace the existing `_check_stability` body:

```python
    def _check_stability(self, current_rank: int) -> bool:
        is_stable, _, _ = self._compute_stability_metrics(current_rank)
        return is_stable
```

- [ ] **Step 6: Update `classify()` to populate new fields**

In `classify()`, inside the state-building loop, replace the current `RegimeState(...)` constructor call with:

```python
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
```

- [ ] **Step 7: Run all regime tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_regime.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add regime/hmm_engine.py tests/test_regime.py
git commit -m "feat: add transition_rate and instability_score to RegimeState

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Incremental Forward-Only Inference

**Files:**
- Modify: `regime/gaussian_hmm.py`
- Modify: `regime/hmm_engine.py`
- Modify: `tests/test_regime.py`

Add `GaussianHMM.forward_step()` — one step of the forward (filter) algorithm. Then add `HMMRegimeEngine.initialize_incremental()` (runs full forward pass, caches final alpha) and `update_single()` (processes one new bar using only the cached alpha — O(K²) per bar, fully causal, no backward pass).

The distinction matters: `classify()` uses the Viterbi path + smoothed posteriors (forward+backward), which is NOT causal for live inference. `update_single()` uses only the filtered distribution — exactly what a live system should use.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_regime.py`:

```python
def test_forward_step_output_shape(fitted_engine):
    """forward_step(log_alpha, obs) → (K,) array."""
    engine, _, _ = fitted_engine
    model = engine._result.model
    K = model.n_components
    D = model.means_.shape[1]
    log_alpha_prev = np.zeros(K)
    obs = np.zeros(D)
    result = model.forward_step(log_alpha_prev, obs)
    assert result.shape == (K,)


def test_forward_step_probabilities_sum_to_one(fitted_engine):
    """Normalised forward step gives a valid distribution."""
    from scipy.special import logsumexp
    engine, _, _ = fitted_engine
    model = engine._result.model
    K = model.n_components
    D = model.means_.shape[1]
    log_alpha_prev = np.log(np.ones(K) / K)
    obs = model.means_[0]  # use a known centroid as observation
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
    """Same bar twice → same regime from a reset state."""
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


def test_update_single_advances_internal_state(fitted_engine):
    """Multiple update_single calls accumulate recent_labels."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    engine.initialize_incremental(data.iloc[:500], feature_cfg)
    labels_before = len(engine._recent_labels)
    for i in range(3):
        engine.update_single(data.iloc[500 + i : 501 + i])
    assert len(engine._recent_labels) == labels_before + 3
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_regime.py::test_forward_step_output_shape tests/test_regime.py::test_initialize_incremental_returns_regime_state -v 2>&1 | tail -10
```

Expected: `AttributeError: 'GaussianHMM' object has no attribute 'forward_step'`.

- [ ] **Step 3: Add `forward_step` to `GaussianHMM`**

In `regime/gaussian_hmm.py`, add this method after `_viterbi` (at the bottom of the class):

```python
    def forward_step(self, log_alpha_prev: np.ndarray, obs: np.ndarray) -> np.ndarray:
        """One step of the forward (filter) algorithm for causal inference.

        Parameters
        ----------
        log_alpha_prev : (K,) log-scaled filter from the previous time step
        obs : (D,) scaled observation vector for the new bar

        Returns
        -------
        log_alpha_new : (K,) updated log-filter (unnormalised)
        """
        log_emis_new = self._log_emission(obs.reshape(1, -1))[0]  # (K,)
        log_trans = np.log(self.transmat_ + 1e-300)
        return logsumexp(log_alpha_prev[:, None] + log_trans, axis=0) + log_emis_new
```

- [ ] **Step 4: Add incremental state fields to `HMMRegimeEngine.__init__`**

In `regime/hmm_engine.py`, in `HMMRegimeEngine.__init__`, add after the existing instance variables:

```python
        # Incremental inference state (populated by initialize_incremental)
        self._last_log_alpha: np.ndarray | None = None
        self._feature_cfg_cache: FeatureConfig | None = None
        self._data_tail: pd.DataFrame | None = None
```

- [ ] **Step 5: Add `compute_features` to imports**

In `regime/hmm_engine.py`, update the import from `features.feature_pipeline`:

```python
from features.feature_pipeline import (
    FeatureConfig,
    build_feature_matrix,
    build_feature_matrix_with_scaler,
    compute_features,
)
```

- [ ] **Step 6: Add `initialize_incremental` to `HMMRegimeEngine`**

Add this method after `current_regime`:

```python
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
```

- [ ] **Step 7: Add `update_single` to `HMMRegimeEngine`**

Add this method after `initialize_incremental`:

```python
    def update_single(
        self,
        new_bar: pd.DataFrame,
        date_str: str | None = None,
    ) -> RegimeState:
        """Classify one new bar using the cached forward state.

        Causal (filter-only): uses only the forward algorithm, no backward pass.
        This is the correct inference path for live trading — it makes no use
        of observations after the current bar.

        Parameters
        ----------
        new_bar : single-row DataFrame with close, volume, vix columns
        date_str : date label for the returned RegimeState (uses today if None)

        Requires initialize_incremental() to have been called first.
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
        from scipy.special import logsumexp as _logsumexp
        log_z = _logsumexp(new_log_alpha)
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

        from datetime import date as _date
        return RegimeState(
            date=date_str or _date.today().isoformat(),
            regime_index=rank,
            regime_label=label,
            confidence=confidence,
            is_stable=is_stable,
            n_regimes=n,
            raw_posteriors=ranked_posteriors,
            transition_rate=transition_rate,
            instability_score=instability_score,
        )
```

- [ ] **Step 8: Run Task 2 tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_regime.py -v 2>&1 | tail -25
```

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add regime/gaussian_hmm.py regime/hmm_engine.py tests/test_regime.py
git commit -m "feat: add GaussianHMM.forward_step and HMMRegimeEngine incremental update

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Rolling Refit

**Files:**
- Modify: `regime/hmm_engine.py`
- Modify: `tests/test_regime.py`

Add `rolling_refit(data, train_window_bars, feature_cfg)` — refits the HMM on the most recent `train_window_bars` of data and invalidates the incremental cache (forcing `initialize_incremental()` to be called again before the next `update_single()`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_regime.py`:

```python
def test_rolling_refit_refits_successfully(fitted_engine):
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    result = engine.rolling_refit(
        data, train_window_bars=250, feature_cfg=feature_cfg
    )
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
    """When train_window_bars < len(data), only the most recent bars are used."""
    from features.feature_pipeline import FeatureConfig
    engine, data, _ = fitted_engine
    # Use a small window to force the tail slice
    feature_cfg = FeatureConfig(vol_window=10, trend_window=30, min_history_bars=50)
    result = engine.rolling_refit(data, train_window_bars=200, feature_cfg=feature_cfg)
    assert engine.is_fitted
    assert result.n_regimes == 3


def test_rolling_refit_uses_all_data_when_window_larger(fitted_engine):
    """When train_window_bars >= len(data), all data is used (no truncation)."""
    from features.feature_pipeline import FeatureConfig
    engine, data, feature_cfg = fitted_engine
    result = engine.rolling_refit(
        data.iloc[:300], train_window_bars=10_000, feature_cfg=feature_cfg
    )
    assert engine.is_fitted
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_regime.py::test_rolling_refit_refits_successfully -v 2>&1 | tail -8
```

Expected: `AttributeError: 'HMMRegimeEngine' object has no attribute 'rolling_refit'`.

- [ ] **Step 3: Add `rolling_refit` to `HMMRegimeEngine`**

Add after `update_single` (before `save`):

```python
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
```

- [ ] **Step 4: Run all regime tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_regime.py -v 2>&1 | tail -25
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add regime/hmm_engine.py tests/test_regime.py
git commit -m "feat: add HMMRegimeEngine.rolling_refit for periodic model refreshes

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Regime Transition Logging

**Files:**
- Modify: `bot/db.py`
- Modify: `regime/hmm_engine.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_regime.py`

Add a `regime_transitions` SQLite table that records every time the regime label changes. Wire transition detection into `current_regime()` and `update_single()` using a lazy `bot.db` import (avoids hard cross-package dependency at module level). Also add `_prev_regime_index` / `_prev_regime_label` tracking to `HMMRegimeEngine.__init__`.

- [ ] **Step 1: Write the DB tests**

Append to `tests/test_db.py`:

```python
def test_log_regime_transition(db):
    db.log_regime_transition(
        date="2026-01-15",
        from_label="bear",
        to_label="bull",
        from_index=0,
        to_index=2,
        confidence=0.75,
        n_regimes=3,
    )
    rows = db.get_regime_transitions(days=365)
    assert len(rows) == 1
    assert rows[0]["from_label"] == "bear"
    assert rows[0]["to_label"] == "bull"
    assert float(rows[0]["confidence"]) == pytest.approx(0.75)
    assert int(rows[0]["n_regimes"]) == 3


def test_get_regime_transitions_empty(db):
    rows = db.get_regime_transitions(days=90)
    assert rows == []


def test_get_regime_transitions_filters_by_days(db):
    db.log_regime_transition("2020-01-01", "bear", "bull", 0, 2, 0.8, 3)
    db.log_regime_transition("2026-01-01", "bull", "neutral", 2, 1, 0.7, 3)
    all_rows = db.get_regime_transitions(days=9999)
    recent_rows = db.get_regime_transitions(days=30)
    assert len(all_rows) == 2
    # Only the 2026-01-01 row should appear in a 30-day window (today is 2026-04-29)
    assert len(recent_rows) >= 1
    assert all(r["date"] >= "2026-01-01" for r in recent_rows)
```

- [ ] **Step 2: Run DB tests to verify they fail**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_db.py::test_log_regime_transition tests/test_db.py::test_get_regime_transitions_empty -v 2>&1 | tail -10
```

Expected: `AttributeError: module 'bot.db' has no attribute 'log_regime_transition'`.

- [ ] **Step 3: Add `regime_transitions` table to `bot/db.py` schema**

In `bot/db.py`, inside the `_SCHEMA` string, append these two statements (before the closing `"""`):

```python
CREATE TABLE IF NOT EXISTS regime_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    from_label TEXT NOT NULL,
    to_label TEXT NOT NULL,
    from_index INTEGER NOT NULL,
    to_index INTEGER NOT NULL,
    confidence REAL NOT NULL,
    n_regimes INTEGER NOT NULL,
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regime_transitions_date ON regime_transitions(date);
```

- [ ] **Step 4: Add `log_regime_transition` and `get_regime_transitions` to `bot/db.py`**

Append after `log_risk_event`:

```python
def log_regime_transition(
    date: str,
    from_label: str,
    to_label: str,
    from_index: int,
    to_index: int,
    confidence: float,
    n_regimes: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO regime_transitions
               (date, from_label, to_label, from_index, to_index,
                confidence, n_regimes, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, from_label, to_label, from_index, to_index,
             confidence, n_regimes, datetime.now(UTC).isoformat()),
        )


def get_regime_transitions(days: int = 90) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM regime_transitions
               WHERE date >= date('now', ?)
               ORDER BY date ASC""",
            (f"-{days} days",),
        ).fetchall()
```

- [ ] **Step 5: Run DB tests**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest tests/test_db.py -v 2>&1 | tail -15
```

Expected: all DB tests pass.

- [ ] **Step 6: Add transition tracking state to `HMMRegimeEngine.__init__`**

In `regime/hmm_engine.py`, in `__init__`, add after the existing `_recent_labels` line:

```python
        self._prev_regime_index: int | None = None
        self._prev_regime_label: str = ""
```

- [ ] **Step 7: Wire transition detection into `current_regime()`**

Replace the existing `current_regime` method body with:

```python
    def current_regime(
        self, data: pd.DataFrame, feature_cfg: FeatureConfig | None = None
    ) -> RegimeState:
        """Return the regime for the latest bar in data."""
        states = self.classify(data, feature_cfg)
        if not states:
            raise RuntimeError("No regime states computed — insufficient data")
        state = states[-1]

        # Detect and log regime transition
        if (self._prev_regime_index is not None
                and self._prev_regime_index != state.regime_index):
            self._log_transition(state)

        self._prev_regime_index = state.regime_index
        self._prev_regime_label = state.regime_label

        self._recent_labels.append(state.regime_index)
        window = self._cfg.min_stable_bars * 3
        if len(self._recent_labels) > window:
            self._recent_labels = self._recent_labels[-window:]
        return state
```

- [ ] **Step 8: Add `_log_transition` helper to `HMMRegimeEngine`**

Add after `current_regime`:

```python
    def _log_transition(self, new_state: RegimeState) -> None:
        """Log a regime change to the database. Silently skips on any error."""
        try:
            import bot.db as _db  # lazy import — avoids hard cross-package dep
            _db.log_regime_transition(
                date=new_state.date,
                from_label=self._prev_regime_label,
                to_label=new_state.regime_label,
                from_index=self._prev_regime_index,
                to_index=new_state.regime_index,
                confidence=new_state.confidence,
                n_regimes=new_state.n_regimes,
            )
            log.info(
                "Regime transition: %s → %s (conf=%.2f)",
                self._prev_regime_label, new_state.regime_label, new_state.confidence,
            )
        except Exception as exc:
            log.debug("Could not log regime transition: %s", exc)
```

- [ ] **Step 9: Wire transition detection into `update_single()` too**

In `update_single`, just before the final `return` statement, add:

```python
        # Detect and log transition (same logic as current_regime)
        if self._prev_regime_index is not None and self._prev_regime_index != rank:
            self._log_transition(RegimeState(
                date=date_str or _date.today().isoformat(),
                regime_index=rank,
                regime_label=label,
                confidence=confidence,
                is_stable=is_stable,
                n_regimes=n,
                raw_posteriors=ranked_posteriors,
                transition_rate=transition_rate,
                instability_score=instability_score,
            ))
        self._prev_regime_index = rank
        self._prev_regime_label = label
```

Note: this duplicates the `RegimeState` construction that already happens in the `return` statement below. Move the construction to a local variable `result` and use it in both places:

```python
        # (replace the final `return RegimeState(...)` block with:)
        result = RegimeState(
            date=date_str or _date.today().isoformat(),
            regime_index=rank,
            regime_label=label,
            confidence=confidence,
            is_stable=is_stable,
            n_regimes=n,
            raw_posteriors=ranked_posteriors,
            transition_rate=transition_rate,
            instability_score=instability_score,
        )
        if self._prev_regime_index is not None and self._prev_regime_index != rank:
            self._log_transition(result)
        self._prev_regime_index = rank
        self._prev_regime_label = label
        return result
```

- [ ] **Step 10: Write the regime-level transition test**

Append to `tests/test_regime.py`:

```python
def test_current_regime_logs_transition(db, fitted_engine):
    """current_regime() writes to regime_transitions when label changes."""
    from features.feature_pipeline import FeatureConfig
    import importlib
    import bot.db
    importlib.reload(bot.db)
    bot.db.init_db()

    engine, data, feature_cfg = fitted_engine
    # Prime the engine with a fake prior regime different from whatever the real one will be
    engine._prev_regime_index = 999
    engine._prev_regime_label = "fake_prior"

    _ = engine.current_regime(data.iloc[:500], feature_cfg)

    rows = bot.db.get_regime_transitions(days=9999)
    assert len(rows) >= 1
    assert rows[-1]["from_label"] == "fake_prior"
    assert rows[-1]["to_label"] in ["bear", "neutral", "bull"]
```

- [ ] **Step 11: Run the full test suite**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
python3 -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all tests pass (243 + new tests).

- [ ] **Step 12: Commit**

```bash
cd "/Users/thomasvromen/Documents/Claude code test/trading bot"
git add bot/db.py regime/hmm_engine.py tests/test_db.py tests/test_regime.py
git commit -m "feat: regime transition logging — DB table + detection in current_regime and update_single

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|-------------|------|
| Historical price/volume features | Pre-existing ✅ |
| Multiple candidate regime counts (3–7) | Pre-existing ✅ |
| BIC/AIC model selection | Pre-existing ✅ |
| Economic labeling (bear/neutral/bull etc.) | Pre-existing ✅ |
| Regime confidence scoring | Pre-existing ✅ |
| Stability filter — min persistent bars | Pre-existing ✅ |
| Stability filter — flickering/instability score | Task 1 |
| Reduce allocations during unstable periods | `transition_rate`/`instability_score` on `RegimeState` — `AllocationEngine` already uses `is_stable`; callers can now also scale by `instability_score` |
| CRITICAL no look-ahead bias | Pre-existing (scaler frozen at fit); Task 2 adds forward-filter-only path |
| `fit()` method | Pre-existing ✅ |
| Rolling/incremental updates | Task 2 (`initialize_incremental` + `update_single`) + Task 3 (`rolling_refit`) |
| Current regime detection | Pre-existing ✅ |
| Confidence estimation | Pre-existing ✅ |
| Stability filtering | Tasks 1 + (pre-existing) |
| Regime transition logging | Task 4 |

**Placeholder scan:** No TBDs. All code blocks are complete.

**Type consistency:**
- `_compute_stability_metrics(rank: int) -> tuple[bool, float, float]` — used in Task 1, referenced in Tasks 2 and 4 ✓
- `RegimeState.transition_rate`, `RegimeState.instability_score` — added in Task 1, populated in Task 2 (`update_single`) ✓
- `_log_transition(new_state: RegimeState)` — defined in Task 4, called from `current_regime()` and `update_single()` ✓
- `_prev_regime_index: int | None`, `_prev_regime_label: str` — added in Task 4 init, used in `_log_transition` ✓
- `log_regime_transition(date, from_label, to_label, from_index, to_index, confidence, n_regimes)` — defined in Task 4 DB step, called by `_log_transition` ✓
- `forward_step(log_alpha_prev: np.ndarray, obs: np.ndarray) -> np.ndarray` — defined in Task 2, called in `update_single` ✓
