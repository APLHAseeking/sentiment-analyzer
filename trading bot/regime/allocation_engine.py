"""Regime-aware allocation engine.

Translates a RegimeState + AI conviction score into a final position size.
The AI score is the "alpha signal"; the regime is the "market filter" that
scales exposure up or down based on market environment.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from regime.hmm_engine import RegimeState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllocationDecision:
    ticker: str
    base_position_pct: float        # what the AI recommended
    regime_multiplier: float        # from regime label
    confidence_multiplier: float    # from regime confidence
    stability_multiplier: float     # 1.0 if stable, instability_penalty if not
    seasonal_multiplier: float      # Halloween effect (1.1 Nov–Apr, 0.9 May–Oct)
    final_position_pct: float       # base × regime × confidence × stability × seasonal
    rationale: str
    regime_label: str
    regime_confidence: float
    is_stable: bool


class AllocationEngine:
    """Convert an AI conviction score into a regime-scaled position size."""

    def __init__(self, cfg: Any) -> None:
        """
        Parameters
        ----------
        cfg : system.config.AllocationConfig + system.config.RiskConfig
              Any object with the expected attributes.
        """
        self._alloc_cfg = cfg.allocation
        self._risk_cfg = cfg.risk

    def _seasonality_mult(self, date: datetime | None = None) -> float:
        """Halloween effect overlay: position sizes are 10% larger Nov–Apr, 10% smaller May–Oct."""
        if not self._alloc_cfg.enable_seasonality:
            return 1.0
        month = (date or datetime.now(UTC)).month
        if month in (11, 12, 1, 2, 3, 4):
            return self._alloc_cfg.halloween_mult_active
        return self._alloc_cfg.halloween_mult_inactive

    def compute(
        self,
        ticker: str,
        ai_position_pct: float,
        regime_state: RegimeState,
        date: datetime | None = None,
    ) -> AllocationDecision:
        """Apply regime scaling to the AI-recommended position size.

        Returns AllocationDecision with final_position_pct = 0 if the
        regime confidence is too low or the regime blocks new entries.
        """
        label = regime_state.regime_label
        confidence = regime_state.confidence
        is_stable = regime_state.is_stable

        # --- Regime multiplier -------------------------------------------
        regime_mult = self._alloc_cfg.regime_size_multiplier.get(label, 1.0)

        # --- Confidence gate + multiplier --------------------------------
        if confidence < self._alloc_cfg.min_confidence_to_trade:
            log.info(
                "%s: confidence %.2f below threshold %.2f — skipping",
                ticker, confidence, self._alloc_cfg.min_confidence_to_trade,
            )
            return self._zero_decision(ticker, ai_position_pct, regime_state,
                                       reason=f"confidence {confidence:.2f} below threshold")

        if self._alloc_cfg.confidence_scale:
            # Scale linearly between threshold (→ 0.5×) and 1.0 (→ 1.0×)
            lo = self._alloc_cfg.min_confidence_to_trade
            conf_mult = 0.5 + 0.5 * (confidence - lo) / max(1.0 - lo, 1e-6)
            conf_mult = min(conf_mult, 1.0)
        else:
            conf_mult = 1.0

        # --- Stability penalty -------------------------------------------
        stab_mult = 1.0 if is_stable else self._alloc_cfg.instability_penalty

        # --- Seasonality multiplier (Halloween effect) -------------------
        seasonal = self._seasonality_mult(date)

        # --- Final size (capped at risk config max) ----------------------
        final = ai_position_pct * regime_mult * conf_mult * stab_mult * seasonal
        final = min(final, self._risk_cfg.max_position_pct)
        final = max(final, 0.0)

        rationale = (
            f"regime={label} (mult={regime_mult:.2f}) × "
            f"conf={confidence:.2f} (mult={conf_mult:.2f}) × "
            f"stability={'OK' if is_stable else f'UNSTABLE ({stab_mult:.2f})'} × "
            f"seasonal={seasonal:.2f} "
            f"→ {ai_position_pct:.1f}% → {final:.1f}%"
        )

        if final < 0.1:
            log.info("%s: allocation reduced to zero by regime (%s)", ticker, rationale)
            return self._zero_decision(ticker, ai_position_pct, regime_state,
                                       reason=rationale)

        log.info("%s: %s", ticker, rationale)
        return AllocationDecision(
            ticker=ticker,
            base_position_pct=ai_position_pct,
            regime_multiplier=regime_mult,
            confidence_multiplier=conf_mult,
            stability_multiplier=stab_mult,
            seasonal_multiplier=seasonal,
            final_position_pct=final,
            rationale=rationale,
            regime_label=label,
            regime_confidence=confidence,
            is_stable=is_stable,
        )

    def _zero_decision(
        self,
        ticker: str,
        base_pct: float,
        regime_state: RegimeState,
        reason: str,
    ) -> AllocationDecision:
        return AllocationDecision(
            ticker=ticker,
            base_position_pct=base_pct,
            regime_multiplier=0.0,
            confidence_multiplier=0.0,
            stability_multiplier=0.0,
            seasonal_multiplier=1.0,
            final_position_pct=0.0,
            rationale=reason,
            regime_label=regime_state.regime_label,
            regime_confidence=regime_state.confidence,
            is_stable=regime_state.is_stable,
        )
