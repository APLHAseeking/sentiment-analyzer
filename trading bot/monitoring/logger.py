"""Structured logging with event hooks for the trading system.

Every significant event emits a structured log record so the dashboard,
alerts, and audit trail can all be driven from the same event stream.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, UTC
from enum import Enum
from typing import Any

from monitoring.alerts import fire_alert


class EventType(str, Enum):
    REGIME_CHANGE = "regime_change"
    REGIME_UNSTABLE = "regime_unstable"
    SIGNAL_QUALIFIED = "signal_qualified"
    SIGNAL_REJECTED = "signal_rejected"
    ORDER_PLACED = "order_placed"
    ORDER_REJECTED = "order_rejected"
    POSITION_CLOSED = "position_closed"
    RISK_VETO = "risk_veto"
    CIRCUIT_BREAKER = "circuit_breaker"
    LOCKOUT_CREATED = "lockout_created"
    MODEL_FIT = "model_fit"
    MODEL_FIT_FAILED = "model_fit_failed"
    DATA_STALE = "data_stale"
    SLIPPAGE_HIGH = "slippage_high"
    DRAWDOWN_THRESHOLD = "drawdown_threshold"
    WEEKLY_REPORT = "weekly_report"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"


class StructuredFormatter(logging.Formatter):
    """Emit log records as JSON so they are parseable by monitoring tools."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "event"):
            payload["event"] = record.event
        if hasattr(record, "data"):
            payload["data"] = record.data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str = "INFO", structured: bool = False) -> None:
    """Call once at startup. structured=True emits JSON lines."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    root.handlers.clear()
    root.addHandler(handler)


def emit_event(
    log: logging.Logger,
    event: EventType,
    message: str,
    data: dict[str, Any] | None = None,
    level: int = logging.INFO,
    alert: bool = False,
) -> None:
    """Emit a structured event log record and optionally fire an alert."""
    extra = {"event": event.value, "data": data or {}}
    log.log(level, message, extra=extra)
    if alert:
        fire_alert(event=event.value, message=message, data=data or {})
