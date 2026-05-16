"""Independent risk management engine with hard veto power.

The risk manager is independent of the regime model and always has final
authority. It can override any strategy signal.

Circuit breakers:
- daily_loss_reduce_pct  → cut new-position sizes 50% for rest of day
- daily_loss_halt_pct    → stop new entries for rest of day
- weekly_loss_halt_pct   → stop new entries for rest of week
- max_drawdown_lockout_pct → create lock file; manual unlock required

The lock file mechanism is a deliberate hard stop that requires human
intervention to resume trading — it is not automatically cleared.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any

import bot.db as db
from monitoring.logger import EventType, emit_event

log = logging.getLogger(__name__)


class RiskState(str, Enum):
    NORMAL = "normal"
    SIZES_REDUCED = "sizes_reduced"     # daily loss reduce threshold hit
    ENTRIES_HALTED = "entries_halted"   # daily loss halt threshold hit
    DELEVERAGE = "deleverage"           # force-close all positions
    WEEKLY_HALT = "weekly_halt"         # weekly loss halt threshold hit
    LOCKED_OUT = "locked_out"           # max drawdown lockout — lock file exists


@dataclass
class RiskVeto:
    allowed: bool
    reason: str
    size_multiplier: float = 1.0        # 1.0 = full size, 0.5 = half, 0.0 = blocked


@dataclass
class EquitySnapshot:
    date: str
    nav: float


class RiskManager:
    """Portfolio-level risk engine with circuit breakers and lock file support."""

    def __init__(self, cfg: Any) -> None:
        """
        Parameters
        ----------
        cfg : system.config.Settings (or any object with .risk attribute)
        """
        self._risk = cfg.risk
        self._state: RiskState = RiskState.NORMAL
        self._peak_nav: float = 0.0
        self._week_start_nav: float = 0.0
        self._day_start_nav: float = 0.0
        self._current_week_start: str = self._get_week_start()
        self._current_day: str = date.today().isoformat()

    # ------------------------------------------------------------------
    # Daily / weekly refresh
    # ------------------------------------------------------------------

    def start_of_day(self, current_nav: float) -> None:
        """Call at the start of each trading day to refresh baseline NAV."""
        today = date.today().isoformat()
        week_start = self._get_week_start()

        if today != self._current_day:
            self._day_start_nav = current_nav
            self._current_day = today
            # Only halted states due to daily circuit breakers are reset daily
            if self._state in (RiskState.SIZES_REDUCED, RiskState.ENTRIES_HALTED, RiskState.DELEVERAGE):
                log.info("Risk state reset to NORMAL at start of new trading day")
                self._state = RiskState.NORMAL

        if week_start != self._current_week_start:
            self._week_start_nav = current_nav
            self._current_week_start = week_start
            if self._state == RiskState.WEEKLY_HALT:
                log.info("Weekly halt cleared at start of new week")
                self._state = RiskState.NORMAL

        if current_nav > self._peak_nav:
            self._peak_nav = current_nav

        if self._day_start_nav == 0:
            self._day_start_nav = current_nav
        if self._week_start_nav == 0:
            self._week_start_nav = current_nav

    def check_circuit_breakers(self, current_nav: float) -> None:
        """Evaluate all circuit breakers and update state. Call after any NAV change."""
        if self._state == RiskState.LOCKED_OUT:
            return  # lock file takes precedence; nothing to change

        if self._peak_nav > 0 and self._day_start_nav > 0:
            drawdown_from_peak = (self._peak_nav - current_nav) / self._peak_nav * 100
            daily_loss = (self._day_start_nav - current_nav) / self._day_start_nav * 100
            weekly_loss = (
                (self._week_start_nav - current_nav) / self._week_start_nav * 100
                if self._week_start_nav > 0 else 0.0
            )

            if drawdown_from_peak >= self._risk.max_drawdown_lockout_pct:
                self._trigger_lockout(current_nav, drawdown_from_peak)
                return

            if weekly_loss >= self._risk.weekly_loss_halt_pct:
                if self._state != RiskState.WEEKLY_HALT:
                    log.warning(
                        "Weekly loss %.2f%% ≥ halt threshold %.2f%% — halting entries for week",
                        weekly_loss, self._risk.weekly_loss_halt_pct,
                    )
                    self._state = RiskState.WEEKLY_HALT
                    db.log_risk_event("weekly_halt", f"Weekly loss {weekly_loss:.2f}%",
                                      {"nav": current_nav, "loss_pct": weekly_loss})
                    emit_event(log, EventType.CIRCUIT_BREAKER,
                               f"Weekly loss {weekly_loss:.2f}% — entries halted",
                               alert=True)
                return

            if daily_loss >= self._risk.daily_loss_deleverage_pct:
                if self._state not in (RiskState.DELEVERAGE, RiskState.WEEKLY_HALT, RiskState.LOCKED_OUT):
                    log.warning(
                        "Daily loss %.2f%% ≥ deleverage threshold %.2f%% — forcing position close",
                        daily_loss, self._risk.daily_loss_deleverage_pct,
                    )
                    self._state = RiskState.DELEVERAGE
                    db.log_risk_event("deleverage", f"Daily loss {daily_loss:.2f}%",
                                      {"nav": current_nav, "loss_pct": daily_loss})
                    emit_event(log, EventType.CIRCUIT_BREAKER,
                               f"Daily loss {daily_loss:.2f}% — DELEVERAGE: closing all positions",
                               alert=True)
                return

            if daily_loss >= self._risk.daily_loss_halt_pct:
                if self._state not in (RiskState.ENTRIES_HALTED, RiskState.WEEKLY_HALT, RiskState.DELEVERAGE):
                    log.warning(
                        "Daily loss %.2f%% ≥ halt threshold %.2f%% — stopping new entries",
                        daily_loss, self._risk.daily_loss_halt_pct,
                    )
                    self._state = RiskState.ENTRIES_HALTED
                    db.log_risk_event("daily_halt", f"Daily loss {daily_loss:.2f}%",
                                      {"nav": current_nav, "loss_pct": daily_loss})
                    emit_event(log, EventType.CIRCUIT_BREAKER,
                               f"Daily loss {daily_loss:.2f}% — new entries halted")
                return

            if daily_loss >= self._risk.daily_loss_reduce_pct:
                if self._state == RiskState.NORMAL:
                    log.info(
                        "Daily loss %.2f%% ≥ reduce threshold %.2f%% — cutting position sizes",
                        daily_loss, self._risk.daily_loss_reduce_pct,
                    )
                    self._state = RiskState.SIZES_REDUCED
                    db.log_risk_event("size_reduction", f"Daily loss {daily_loss:.2f}%",
                                      {"nav": current_nav, "loss_pct": daily_loss})

    def _trigger_lockout(self, nav: float, drawdown_pct: float) -> None:
        self._state = RiskState.LOCKED_OUT
        lock_path = self._risk.lock_file_path
        try:
            with open(lock_path, "w") as f:
                f.write(
                    f"RISK LOCKOUT\n"
                    f"Date: {date.today().isoformat()}\n"
                    f"Peak NAV: {self._peak_nav:.2f}\n"
                    f"Current NAV: {nav:.2f}\n"
                    f"Drawdown: {drawdown_pct:.2f}%\n"
                    f"Threshold: {self._risk.max_drawdown_lockout_pct:.1f}%\n"
                    f"Manual intervention required to resume trading.\n"
                    f"Delete this file after reviewing the situation.\n"
                )
        except Exception as exc:
            log.error("Failed to write lock file: %s", exc)

        db.log_risk_event(
            "lockout", f"Drawdown {drawdown_pct:.2f}% — lock file created",
            {"nav": nav, "peak_nav": self._peak_nav, "drawdown_pct": drawdown_pct},
        )
        emit_event(
            log, EventType.LOCKOUT_CREATED,
            f"Max drawdown {drawdown_pct:.2f}% exceeded — LOCK FILE CREATED at {lock_path}",
            data={"nav": nav, "drawdown_pct": drawdown_pct},
            level=logging.CRITICAL,
            alert=True,
        )

    # ------------------------------------------------------------------
    # Trade veto interface
    # ------------------------------------------------------------------

    def veto_new_entry(self, ticker: str, proposed_pct: float) -> RiskVeto:
        """Return whether a new entry is allowed and at what size."""
        # Lock file takes absolute precedence
        if os.path.exists(self._risk.lock_file_path):
            self._state = RiskState.LOCKED_OUT
            return RiskVeto(allowed=False, reason="Risk lockout active — delete lock file to resume")

        if self._state in (RiskState.ENTRIES_HALTED, RiskState.WEEKLY_HALT, RiskState.LOCKED_OUT, RiskState.DELEVERAGE):
            return RiskVeto(allowed=False, reason=f"Entries blocked by circuit breaker: {self._state}")

        if self._state == RiskState.SIZES_REDUCED:
            new_pct = proposed_pct * 0.5
            return RiskVeto(
                allowed=True,
                reason=f"Size reduced 50% due to daily loss circuit breaker",
                size_multiplier=0.5,
            )

        return RiskVeto(allowed=True, reason="OK", size_multiplier=1.0)

    def validate_order(
        self,
        ticker: str,
        position_pct: float,
        sector: str,
        sector_allocation: dict[str, float],
        position_size_usd: float,
        adv_usd: float | None,
    ) -> RiskVeto:
        """Validate a proposed order against all position-level risk rules."""
        veto = self.veto_new_entry(ticker, position_pct)
        if not veto.allowed:
            return veto

        # Sector concentration
        from system.config import settings
        if sector_allocation.get(sector, 0.0) >= self._risk.max_sector_pct:
            return RiskVeto(
                allowed=False,
                reason=f"Sector cap: {sector} at {sector_allocation.get(sector, 0):.1f}%",
            )

        # Liquidity
        if adv_usd and adv_usd > 0:
            adv_pct = position_size_usd / adv_usd * 100
            if adv_pct > self._risk.max_adv_pct:
                return RiskVeto(
                    allowed=False,
                    reason=f"Illiquid: {adv_pct:.1f}% of ADV (max {self._risk.max_adv_pct}%)",
                )

        return RiskVeto(
            allowed=True,
            reason="Order validated",
            size_multiplier=veto.size_multiplier,
        )

    # ------------------------------------------------------------------
    # State introspection
    # ------------------------------------------------------------------

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def is_locked_out(self) -> bool:
        return os.path.exists(self._risk.lock_file_path) or self._state == RiskState.LOCKED_OUT

    def status_dict(self) -> dict:
        return {
            "state": self._state.value,
            "peak_nav": self._peak_nav,
            "day_start_nav": self._day_start_nav,
            "week_start_nav": self._week_start_nav,
            "lock_file_exists": os.path.exists(self._risk.lock_file_path),
            "max_invested_pct": self._risk.max_invested_pct,
        }

    @staticmethod
    def _get_week_start() -> str:
        today = date.today()
        return (today - timedelta(days=today.weekday())).isoformat()
