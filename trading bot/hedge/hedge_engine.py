"""Inverse ETF hedge engine.

Given the current regime state and open portfolio positions, computes
which inverse ETF orders to open and which to close. The orchestrator
calls this as a black box — no coupling to the signal pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from regime.hmm_engine import RegimeState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HedgeOrder:
    ticker: str
    position_pct: float   # % of NAV to allocate
    rationale: str


class HedgeEngine:
    """Compute hedge entry and exit decisions from regime state."""

    def __init__(self, cfg: Any) -> None:
        self._risk_cfg = cfg.risk
        self._hedge_cfg = cfg.hedge

    def is_hedge_regime(self, regime_state: RegimeState) -> bool:
        """True if the regime label is bear, crash, or deep-bear."""
        return regime_state.regime_label in self._hedge_cfg.max_inverse_pct_by_regime

    def compute_hedge_plan(
        self,
        regime_state: RegimeState,
        open_positions_meta: list[dict],
        sector_allocation: dict[str, float],
        nav: float,
    ) -> list[HedgeOrder]:
        """Return orders for inverse ETF positions to open.

        Already-open hedges and ETFs that conflict with current long
        sector exposure are excluded. Equal-weights eligible ETFs up to
        the regime's allocation cap and the per-ETF size cap.
        """
        _ = nav  # reserved for future minimum-dollar position guard
        if not self._risk_cfg.enable_inverse_hedging:
            return []
        if not self.is_hedge_regime(regime_state):
            return []

        max_alloc = self._hedge_cfg.max_inverse_pct_by_regime[regime_state.regime_label]
        already_open = {
            p["ticker"] for p in open_positions_meta
            if p.get("signal_source") == "hedge"
        }

        eligible: list[str] = []
        for etf, conflict_sectors in self._hedge_cfg.inverse_etf_universe.items():
            if etf in already_open:
                continue
            conflicted = any(
                sector_allocation.get(s, 0.0) > self._hedge_cfg.conflict_threshold_pct
                for s in conflict_sectors
            )
            if conflicted:
                continue
            eligible.append(etf)

        if not eligible:
            log.info("HedgeEngine: no eligible ETFs (all open or conflicted)")
            return []

        alloc_per_etf = min(
            max_alloc / len(eligible),
            self._hedge_cfg.max_single_position_pct,
        )

        return [
            HedgeOrder(
                ticker=etf,
                position_pct=alloc_per_etf,
                rationale=(
                    f"Regime hedge: {etf} in {regime_state.regime_label} "
                    f"(conf={regime_state.confidence:.2f}), alloc={alloc_per_etf:.1f}%"
                ),
            )
            for etf in eligible
        ]

    def get_exits_needed(self, open_positions_meta: list[dict]) -> list[str]:
        """Return tickers of all open hedge positions (to close on regime exit)."""
        return [
            p["ticker"] for p in open_positions_meta
            if p.get("signal_source") == "hedge"
        ]
