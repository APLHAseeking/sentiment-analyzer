"""Alert hooks — log-based and optional webhook stubs.

No live external alerting platform is connected. All alerts write to the
standard log and optionally to a local webhook URL if configured.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, UTC
from typing import Any

import requests

log = logging.getLogger(__name__)

_WEBHOOK_URL: str = os.environ.get("ALERT_WEBHOOK_URL", "")

# Events that are always shown as warnings regardless of caller level
_HIGH_PRIORITY = {
    "lockout_created",
    "circuit_breaker",
    "model_fit_failed",
    "drawdown_threshold",
}


def fire_alert(event: str, message: str, data: dict[str, Any]) -> None:
    """Write alert to log and optionally POST to a webhook."""
    level = logging.WARNING if event in _HIGH_PRIORITY else logging.INFO
    log.log(level, "[ALERT] %s | %s | %s", event, message, json.dumps(data))

    if _WEBHOOK_URL:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "message": message,
            "data": data,
        }
        try:
            requests.post(_WEBHOOK_URL, json=payload, timeout=5)
        except Exception as exc:
            log.warning("Alert webhook failed: %s", exc)
