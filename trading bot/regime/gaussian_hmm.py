"""Pure-NumPy Gaussian HMM — drop-in replacement for hmmlearn.GaussianHMM.

Implements:
- Baum-Welch EM for fitting (diagonal covariance only — suitable for regime detection)
- Viterbi decoding
- Forward algorithm for posterior probabilities
- Log-likelihood scoring

Designed for regime detection on ~100-5000 bar sequences with 3-7 hidden states
and 4-8 features. Does not support tied/spherical/full covariance (not needed
for this application, and keeping it diagonal makes the E-step O(T·K·D) instead
of O(T·K·D²)).

No external dependencies beyond numpy and scipy.
"""
from __future__ import annotations

import numpy as np
from scipy.special import logsumexp


class GaussianHMM:
    """Gaussian HMM with diagonal covariance — pure NumPy implementation."""

    def __init__(
        self,
        n_components: int = 3,
        n_iter: int = 100,
        tol: float = 1e-4,
        random_state: int | None = None,
        covariance_type: str = "diag",  # accepted but only diag implemented
    ) -> None:
        self.n_components = n_components
        self.n_iter = n_iter
        self.tol = tol
        self._rng = np.random.default_rng(random_state)
        self.covariance_type = covariance_type

        # Parameters (set after fit)
        self.startprob_: np.ndarray | None = None   # (K,)
        self.transmat_: np.ndarray | None = None    # (K, K)
        self.means_: np.ndarray | None = None       # (K, D)
        self.covars_: np.ndarray | None = None      # (K, D) — diagonal variances
        self._log_likelihood: float = -np.inf

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, X: np.ndarray) -> "GaussianHMM":
        """Fit by Baum-Welch EM. X shape: (T, D)."""
        T, D = X.shape
        K = self.n_components

        # --- Initialize ---
        self._init_params(X)

        prev_ll = -np.inf
        for _ in range(self.n_iter):
            # E-step
            log_emis = self._log_emission(X)         # (T, K)
            log_alpha = self._forward(log_emis)      # (T, K)
            log_beta = self._backward(log_emis)      # (T, K)
            log_gamma = log_alpha + log_beta         # (T, K) unnormalised
            log_Z = logsumexp(log_gamma[-1])
            log_gamma -= log_Z                       # normalise: sum over K at each t = 1
            log_gamma = np.clip(log_gamma, -500, 0)

            # xi: (T-1, K, K)
            log_xi = (
                log_alpha[:-1, :, None]
                + self.transmat_[None, :, :]
                + log_emis[1:, None, :]
                + log_beta[1:, None, :]
            )
            log_xi -= logsumexp(log_xi.reshape(T - 1, -1), axis=1)[:, None, None]

            # M-step
            gamma = np.exp(log_gamma)                # (T, K)
            xi = np.exp(log_xi)                      # (T-1, K, K)

            self.startprob_ = np.clip(gamma[0] / gamma[0].sum(), 1e-300, None)
            self.transmat_ = np.clip(
                xi.sum(0) / gamma[:-1].sum(0)[:, None], 1e-300, None
            )
            self.transmat_ /= self.transmat_.sum(1, keepdims=True)

            g_sum = gamma.sum(0)                     # (K,)
            self.means_ = (gamma[:, :, None] * X[:, None, :]).sum(0) / g_sum[:, None]
            diff = X[:, None, :] - self.means_[None, :, :]  # (T, K, D)
            self.covars_ = (
                gamma[:, :, None] * diff ** 2
            ).sum(0) / g_sum[:, None]
            self.covars_ = np.clip(self.covars_, 1e-6, None)

            ll = float(log_Z)
            if abs(ll - prev_ll) < self.tol:
                break
            prev_ll = ll

        self._log_likelihood = prev_ll
        return self

    def _init_params(self, X: np.ndarray) -> None:
        T, D = X.shape
        K = self.n_components
        self.startprob_ = np.full(K, 1.0 / K)
        self.transmat_ = np.full((K, K), 1.0 / K)
        # k-means-style init: pick K random centroids from data
        indices = self._rng.choice(T, size=K, replace=False)
        self.means_ = X[indices].copy()
        self.covars_ = np.tile(X.var(0) + 1e-6, (K, 1))

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def score(self, X: np.ndarray) -> float:
        """Log-likelihood of observation sequence X."""
        log_emis = self._log_emission(X)
        log_alpha = self._forward(log_emis)
        return float(logsumexp(log_alpha[-1]))

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Viterbi decoding — most likely state sequence."""
        log_emis = self._log_emission(X)
        return self._viterbi(log_emis)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Smoothed posterior P(state | all observations). Shape (T, K)."""
        log_emis = self._log_emission(X)
        log_alpha = self._forward(log_emis)
        log_beta = self._backward(log_emis)
        log_gamma = log_alpha + log_beta
        # Normalise each row
        log_gamma -= logsumexp(log_gamma, axis=1, keepdims=True)
        return np.exp(log_gamma)

    # ------------------------------------------------------------------
    # Internal algorithms
    # ------------------------------------------------------------------

    def _log_emission(self, X: np.ndarray) -> np.ndarray:
        """Diagonal Gaussian log-emission. Shape (T, K)."""
        T, D = X.shape
        K = self.n_components
        log_emis = np.zeros((T, K))
        for k in range(K):
            diff = X - self.means_[k]               # (T, D)
            log_det = np.log(self.covars_[k]).sum()  # scalar
            maha = (diff ** 2 / self.covars_[k]).sum(1)  # (T,)
            log_emis[:, k] = -0.5 * (D * np.log(2 * np.pi) + log_det + maha)
        return log_emis

    def _forward(self, log_emis: np.ndarray) -> np.ndarray:
        T, K = log_emis.shape
        log_alpha = np.empty((T, K))
        log_alpha[0] = np.log(self.startprob_ + 1e-300) + log_emis[0]
        log_trans = np.log(self.transmat_ + 1e-300)
        for t in range(1, T):
            log_alpha[t] = logsumexp(log_alpha[t - 1, :, None] + log_trans, axis=0) + log_emis[t]
        return log_alpha

    def _backward(self, log_emis: np.ndarray) -> np.ndarray:
        T, K = log_emis.shape
        log_beta = np.zeros((T, K))
        log_trans = np.log(self.transmat_ + 1e-300)
        for t in range(T - 2, -1, -1):
            log_beta[t] = logsumexp(
                log_trans + log_emis[t + 1, None, :] + log_beta[t + 1, None, :], axis=1
            )
        return log_beta

    def _viterbi(self, log_emis: np.ndarray) -> np.ndarray:
        T, K = log_emis.shape
        log_delta = np.empty((T, K))
        psi = np.empty((T, K), dtype=int)
        log_trans = np.log(self.transmat_ + 1e-300)
        log_delta[0] = np.log(self.startprob_ + 1e-300) + log_emis[0]
        for t in range(1, T):
            scores = log_delta[t - 1, :, None] + log_trans    # (K, K)
            psi[t] = scores.argmax(0)
            log_delta[t] = scores.max(0) + log_emis[t]
        # Backtrack
        states = np.empty(T, dtype=int)
        states[-1] = log_delta[-1].argmax()
        for t in range(T - 2, -1, -1):
            states[t] = psi[t + 1, states[t + 1]]
        return states

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
