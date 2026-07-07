"""Tests for GaussianHMM's Baum-Welch EM internals (fit()).

Critical property verified:
- The E-step's xi (pairwise state posterior) computation must combine
  log_alpha, transmat_, log_emis, and log_beta entirely in log-domain.
  transmat_ is stored in linear scale, so it must be logged before being
  added to the other (already log-domain) terms. A regression here is
  invisible on well-separated synthetic toy data (the model still finds
  the right states) but collapses a learned sticky transition matrix
  toward near-uniform when emission distributions meaningfully overlap —
  the normal case for real daily equity returns.
"""
from __future__ import annotations

import numpy as np

from regime.gaussian_hmm import GaussianHMM


def _make_sticky_overlapping_data(
    seed: int = 11, T: int = 500, n_states: int = 2
) -> tuple[np.ndarray, np.ndarray]:
    """Generate from a KNOWN sticky 2-state Gaussian HMM with overlapping
    emissions (means ~1 std apart, not well-separated) — the regime where
    the log-domain xi bug is visible. True self-transition probability is
    0.95 (a "sticky" regime-persistence matrix)."""
    rng = np.random.default_rng(seed)
    true_transmat = np.array([[0.95, 0.05], [0.05, 0.95]])
    means = np.array([0.0, 1.0])  # ~1 std apart (std=1) -> meaningfully overlapping
    stds = np.array([1.0, 1.0])
    startprob = np.array([0.5, 0.5])

    states = np.empty(T, dtype=int)
    states[0] = rng.choice(n_states, p=startprob)
    for t in range(1, T):
        states[t] = rng.choice(n_states, p=true_transmat[states[t - 1]])

    X = rng.normal(means[states], stds[states]).reshape(-1, 1)
    return X, true_transmat


def test_fit_recovers_sticky_transition_matrix_on_overlapping_emissions():
    """On synthetic data from a known sticky (diag ~0.95) 2-state HMM with
    overlapping emissions, the learned transmat_ diagonal must stay well
    above a near-uniform collapse.

    Threshold calibrated empirically against this exact dataset/config:
    - Buggy code (transmat_ added in linear scale inside log_xi): learned
      diagonal lands around 0.55-0.60 across multiple seeds — collapsed
      toward uniform (0.5).
    - Fixed code (np.log(transmat_ + 1e-300) inside log_xi): learned
      diagonal lands around 0.89-0.98 across multiple seeds — close to the
      true 0.95.
    0.80 sits clearly between these two clusters with margin on both sides.
    """
    X, true_transmat = _make_sticky_overlapping_data(seed=11, T=500)

    model = GaussianHMM(n_components=2, n_iter=80, random_state=11, n_restarts=2)
    model.fit(X)

    learned_diag = np.diag(model.transmat_)
    assert np.all(learned_diag > 0.80), (
        f"learned transmat_ diagonal collapsed toward uniform: {learned_diag} "
        f"(true diagonal was {np.diag(true_transmat)})"
    )


def test_fit_raises_clear_error_on_degenerate_input():
    """All-NaN input makes every EM restart return a NaN log-likelihood; fit()
    must raise a clear RuntimeError instead of crashing on best_params=None."""
    import pytest

    X = np.full((50, 2), np.nan)
    model = GaussianHMM(n_components=2, n_iter=5, random_state=1, n_restarts=2)
    with pytest.raises(RuntimeError, match="non-finite"):
        model.fit(X)
