"""Alert sender abstraction.

WebhookAlertSender — POST JSON to a Slack- or Discord-compatible webhook URL.
LogAlertSender     — fallback: emit a WARNING to the standard log.

The active sender is built lazily on first use from settings.monitoring.alert_webhook_url
(or the ALERT_WEBHOOK_URL environment variable). Set the env var or config field to enable
real webhook delivery; leave it empty to keep log-only alerts.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, UTC
from typing import Any

import requests

log = logging.getLogger(__name__)

_HIGH_PRIORITY = {
    "lockout_created",
    "circuit_breaker",
    "model_fit_failed",
    "drawdown_threshold",
}

# Mutable single-element list so tests can reset and patch it easily.
_sender_cache: list[AlertSender | None] = [None]


class AlertSender(ABC):
    @abstractmethod
    def send(self, event: str, message: str, data: dict[str, Any]) -> None: ...


class WebhookAlertSender(AlertSender):
    """POST a JSON payload compatible with Slack/Discord incoming webhooks."""

    def __init__(self, url: str, timeout: int = 5) -> None:
        self._url = url
        self._timeout = timeout

    def send(self, event: str, message: str, data: dict[str, Any]) -> None:
        payload = {
            "text": f"[{event.upper()}] {message}",
            "event": event,
            "ts": datetime.now(UTC).isoformat(),
            "data": data,
        }
        try:
            requests.post(url=self._url, json=payload, timeout=self._timeout)
        except Exception as exc:
            log.warning("Alert webhook delivery failed (%s): %s", event, exc)


class LogAlertSender(AlertSender):
    """Fallback sender — writes to the standard log as WARNING."""

    def send(self, event: str, message: str, data: dict[str, Any]) -> None:
        log.warning("[ALERT] %s | %s | %s", event, message, json.dumps(data))


def _build_sender() -> AlertSender:
    try:
        from system.config import settings
        url = settings.monitoring.alert_webhook_url
    except Exception:
        url = ""
    return WebhookAlertSender(url) if url else LogAlertSender()


def _get_sender() -> AlertSender:
    if _sender_cache[0] is None:
        _sender_cache[0] = _build_sender()
    return _sender_cache[0]


def fire_alert(event: str, message: str, data: dict[str, Any]) -> None:
    """Route an alert to the configured sender (webhook or log)."""
    level = logging.WARNING if event in _HIGH_PRIORITY else logging.INFO
    log.log(level, "[ALERT] %s | %s", event, message)
    _get_sender().send(event, message, data)
