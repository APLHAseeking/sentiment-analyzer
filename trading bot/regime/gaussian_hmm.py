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


# Default number of random-initialization restarts for fit(). A single
# k-means-style init + EM run can converge to a poor local optimum (regime
# count/labels unstable across rolling refits); best-of-N restarts trades a
# constant-factor runtime cost for a materially better-and-more-stable fit.
DEFAULT_N_RESTARTS = 5


class GaussianHMM:
    """Gaussian HMM with diagonal covariance — pure NumPy implementation."""

    def __init__(
        self,
        n_components: int = 3,
        n_iter: int = 100,
        tol: float = 1e-4,
        random_state: int | None = None,
        covariance_type: str = "diag",  # accepted but only diag implemented
        n_restarts: int = DEFAULT_N_RESTARTS,
    ) -> None:
        self.n_components = n_components
        self.n_iter = n_iter
        self.tol = tol
        self.random_state = random_state
        self.covariance_type = covariance_type
        self.n_restarts = n_restarts

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
        """Fit by Baum-Welch EM, keeping the best of ``n_restarts`` random
        initializations (by final log-likelihood).

        Each restart uses a derived seed (``random_state + restart_index``)
        so the overall result is reproducible given the same random_state,
        while different restarts explore different k-means-style inits.
        """
        n_restarts = max(1, self.n_restarts)
        base_seed = self.random_state

        best_params: dict | None = None
        best_ll = -np.inf
        for i in range(n_restarts):
            seed = None if base_seed is None else base_seed + i
            rng = np.random.default_rng(seed)
            params, ll = self._fit_once(X, rng)
            if ll > best_ll:
                best_ll = ll
                best_params = params

        self.startprob_ = best_params["startprob_"]
        self.transmat_ = best_params["transmat_"]
        self.means_ = best_params["means_"]
        self.covars_ = best_params["covars_"]
        self._log_likelihood = best_ll
        return self

    def _fit_once(self, X: np.ndarray, rng: np.random.Generator) -> tuple[dict, float]:
        """Single k-means-style init + Baum-Welch EM run.

        Returns the fitted parameters (as a dict, so the caller can compare
        candidates without mutating self) and the final log-likelihood.
        """
        T, D = X.shape
        K = self.n_components

        startprob_, transmat_, means_, covars_ = self._init_params(X, rng)

        prev_ll = -np.inf
        for _ in range(self.n_iter):
            # E-step
            log_emis = self._log_emission(X, means_, covars_)  # (T, K)
            log_alpha = self._forward(log_emis, startprob_, transmat_)  # (T, K)
            log_beta = self._backward(log_emis, transmat_)      # (T, K)
            log_gamma = log_alpha + log_beta         # (T, K) unnormalised
            log_Z = logsumexp(log_gamma[-1])
            log_gamma -= log_Z                       # normalise: sum over K at each t = 1
            log_gamma = np.clip(log_gamma, -500, 0)

            # xi: (T-1, K, K)
            log_xi = (
                log_alpha[:-1, :, None]
                + np.log(transmat_[None, :, :] + 1e-300)
                + log_emis[1:, None, :]
                + log_beta[1:, None, :]
            )
            log_xi -= logsumexp(log_xi.reshape(T - 1, -1), axis=1)[:, None, None]

            # M-step
            gamma = np.exp(log_gamma)                # (T, K)
            xi = np.exp(log_xi)                      # (T-1, K, K)

            startprob_ = np.clip(gamma[0] / gamma[0].sum(), 1e-300, None)
            transmat_ = np.clip(
                xi.sum(0) / gamma[:-1].sum(0)[:, None], 1e-300, None
            )
            transmat_ /= transmat_.sum(1, keepdims=True)

            g_sum = gamma.sum(0)                     # (K,)
            means_ = (gamma[:, :, None] * X[:, None, :]).sum(0) / g_sum[:, None]
            diff = X[:, None, :] - means_[None, :, :]  # (T, K, D)
            covars_ = (
                gamma[:, :, None] * diff ** 2
            ).sum(0) / g_sum[:, None]
            covars_ = np.clip(covars_, 1e-6, None)

            ll = float(log_Z)
            if abs(ll - prev_ll) < self.tol:
                break
            prev_ll = ll

        params = {
            "startprob_": startprob_,
            "transmat_": transmat_,
            "means_": means_,
            "covars_": covars_,
        }
        return params, prev_ll

    def _init_params(
        self, X: np.ndarray, rng: np.random.Generator
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        T, D = X.shape
        K = self.n_components
        startprob_ = np.full(K, 1.0 / K)
        transmat_ = np.full((K, K), 1.0 / K)
        # k-means-style init: pick K random centroids from data
        indices = rng.choice(T, size=K, replace=False)
        means_ = X[indices].copy()
        covars_ = np.tile(X.var(0) + 1e-6, (K, 1))
        return startprob_, transmat_, means_, covars_

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
        """Smoothed posterior P(state | ALL observations). Shape (T, K).

        WARNING: This uses the forward-backward (Baum-Welch) smoothing algorithm.
        The backward pass incorporates future observations, so posteriors at time t
        depend on obs_{t+1}, ..., obs_T. This introduces look-ahead bias and must
        NOT be used for backtesting or live inference. Use predict_proba_filtered()
        for causal (real-time) inference.
        """
        log_emis = self._log_emission(X)
        log_alpha = self._forward(log_emis)
        log_beta = self._backward(log_emis)
        log_gamma = log_alpha + log_beta
        # Normalise each row
        log_gamma -= logsumexp(log_gamma, axis=1, keepdims=True)
        return np.exp(log_gamma)

    def predict_proba_filtered(self, X: np.ndarray) -> np.ndarray:
        """Filtered (causal) posterior P(state_t | obs_1..t). No look-ahead.

        Uses only the forward algorithm — each row t reflects only past observations.
        Use this for live inference and backtesting. predict_proba() is smoothed
        (uses future observations) and must not be used causally.
        """
        log_emis = self._log_emission(X)
        log_alpha = self._forward(log_emis)
        log_alpha -= logsumexp(log_alpha, axis=1, keepdims=True)
        return np.exp(log_alpha)

    # ------------------------------------------------------------------
    # Internal algorithms
    # ------------------------------------------------------------------

    def _log_emission(
        self,
        X: np.ndarray,
        means_: np.ndarray | None = None,
        covars_: np.ndarray | None = None,
    ) -> np.ndarray:
        """Diagonal Gaussian log-emission. Shape (T, K).

        means_/covars_ default to the fitted self.means_/self.covars_; a
        restart candidate mid-fit() passes its own (not-yet-committed-to-self)
        parameters explicitly instead.
        """
        if means_ is None:
            means_ = self.means_
        if covars_ is None:
            covars_ = self.covars_
        T, D = X.shape
        K = self.n_components
        log_emis = np.zeros((T, K))
        for k in range(K):
            diff = X - means_[k]               # (T, D)
            log_det = np.log(covars_[k]).sum()  # scalar
            maha = (diff ** 2 / covars_[k]).sum(1)  # (T,)
            log_emis[:, k] = -0.5 * (D * np.log(2 * np.pi) + log_det + maha)
        return log_emis

    def _forward(
        self,
        log_emis: np.ndarray,
        startprob_: np.ndarray | None = None,
        transmat_: np.ndarray | None = None,
    ) -> np.ndarray:
        if startprob_ is None:
            startprob_ = self.startprob_
        if transmat_ is None:
            transmat_ = self.transmat_
        T, K = log_emis.shape
        log_alpha = np.empty((T, K))
        log_alpha[0] = np.log(startprob_ + 1e-300) + log_emis[0]
        log_trans = np.log(transmat_ + 1e-300)
        for t in range(1, T):
            log_alpha[t] = logsumexp(log_alpha[t - 1, :, None] + log_trans, axis=0) + log_emis[t]
        return log_alpha

    def _backward(
        self, log_emis: np.ndarray, transmat_: np.ndarray | None = None
    ) -> np.ndarray:
        if transmat_ is None:
            transmat_ = self.transmat_
        T, K = log_emis.shape
        log_beta = np.zeros((T, K))
        log_trans = np.log(transmat_ + 1e-300)
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
