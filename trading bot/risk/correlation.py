"""Correlation-aware position sizing filter.

Reduces position sizes when a candidate is highly correlated with an
existing holding, using rolling Pearson correlation on daily returns.

Usage:
    At pipeline start:  corr_filter.load_holdings_returns(holding_tickers)
    Per candidate:      multiplier = corr_filter.size_multiplier(ticker)
    At pipeline end:    corr_filter.clear()
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


class CorrelationFilter:
    """Compute a position-size multiplier based on pairwise return correlation."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg.correlation
        self._holdings_returns: dict[str, pd.Series] = {}
        self._candidate_cache: dict[str, pd.Series] = {}

    def load_holdings_returns(self, tickers: list[str]) -> None:
        """Pre-fetch window_days returns for long holdings. Call once per morning."""
        self._holdings_returns = {}
        if not tickers:
            return
        try:
            raw = yf.download(tickers, period=f"{self._cfg.window_days}d", auto_adjust=True, progress=False)
            if raw.empty:
                return
            close_data = raw["Close"]
            if not isinstance(close_data, pd.DataFrame):
                # Single ticker returned as Series — wrap in DataFrame
                close_data = close_data.to_frame(name=tickers[0])
            returns = close_data.pct_change().dropna()
            for ticker in tickers:
                if ticker in returns.columns:
                    series = returns[ticker].dropna()
                    if not series.empty:
                        self._holdings_returns[ticker] = series
        except Exception as exc:
            log.warning("load_holdings_returns failed: %s", exc)
            self._holdings_returns = {}

    def size_multiplier(self, candidate_ticker: str) -> float:
        """Return a multiplier ∈ [0.0, 1.0] to apply to candidate's position size.

        Returns 1.0 (no penalty) when holdings cache is empty, when max pairwise
        ρ is at or below threshold, or on any data error.
        """
        if not self._holdings_returns:
            return 1.0

        if candidate_ticker not in self._candidate_cache:
            try:
                raw = yf.download(
                    [candidate_ticker], period=f"{self._cfg.window_days}d", auto_adjust=True, progress=False
                )
                if raw.empty:
                    return 1.0
                close_data = raw["Close"]
                if isinstance(close_data, pd.DataFrame):
                    close_data = close_data[candidate_ticker]
                # close_data is now a Series
                self._candidate_cache[candidate_ticker] = close_data.pct_change().dropna()
            except Exception as exc:
                log.warning(
                    "size_multiplier: could not fetch returns for %s: %s",
                    candidate_ticker, exc,
                )
                return 1.0

        cand_returns = self._candidate_cache[candidate_ticker]
        max_rho = 0.0
        valid = 0

        for hold_returns in self._holdings_returns.values():
            aligned_cand, aligned_hold = cand_returns.align(hold_returns, join="inner")
            if len(aligned_cand) < self._cfg.min_overlap_days:
                continue
            rho = float(aligned_cand.corr(aligned_hold))
            if pd.isna(rho):
                continue
            if rho > max_rho:
                max_rho = rho
            valid += 1

        if valid == 0:
            return 1.0

        return self._multiplier_from_rho(max_rho)

    def _multiplier_from_rho(self, rho: float) -> float:
        """Linear decay from 1.0 at threshold to 0.0 at perfect correlation."""
        if rho <= self._cfg.threshold:
            return 1.0
        return max(0.0, 1.0 - (rho - self._cfg.threshold) / (1.0 - self._cfg.threshold))

    def clear(self) -> None:
        """Reset returns caches. Call at end of morning pipeline."""
        self._holdings_returns = {}
        self._candidate_cache = {}
