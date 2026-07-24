"""Tests for the AlertSender abstraction and fire_alert() dispatch."""
import json
import logging
import pytest
from unittest.mock import MagicMock


def test_webhook_sender_posts_correct_payload(mocker):
    from monitoring.alerts import WebhookAlertSender
    mock_post = mocker.patch("monitoring.alerts.requests.post")
    sender = WebhookAlertSender(url="https://hooks.example.com/test")
    sender.send("circuit_breaker", "Daily loss exceeded", {"loss_pct": 4.1})
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]
    assert call_kwargs["url"] == "https://hooks.example.com/test"
    payload = call_kwargs["json"]
    assert payload["event"] == "circuit_breaker"
    assert "Daily loss exceeded" in payload["text"]


def test_webhook_sender_handles_network_error_silently(mocker):
    from monitoring.alerts import WebhookAlertSender
    import requests as req
    mocker.patch("monitoring.alerts.requests.post",
                 side_effect=req.exceptions.Timeout)
    sender = WebhookAlertSender(url="https://hooks.example.com/test")
    sender.send("lockout_created", "Lockout triggered", {})  # must not raise


def test_log_sender_emits_warning(caplog):
    from monitoring.alerts import LogAlertSender
    sender = LogAlertSender()
    with caplog.at_level(logging.WARNING, logger="monitoring.alerts"):
        sender.send("circuit_breaker", "Test warning", {"x": 1})
    assert any("circuit_breaker" in r.message for r in caplog.records)


def test_fire_alert_delegates_to_sender(mocker):
    from monitoring import alerts
    mock_sender = MagicMock()
    mocker.patch("monitoring.alerts._get_sender", return_value=mock_sender)
    alerts.fire_alert("startup", "Hello", {"k": "v"})
    mock_sender.send.assert_called_once_with("startup", "Hello", {"k": "v"})


def test_conftest_never_leaks_the_real_alert_webhook_into_tests():
    """A test that exercises an alert=True code path (e.g. an ORDER_REJECTED
    close_position rejection) must never reach the real Slack webhook -- the
    real .env ALERT_WEBHOOK_URL was leaking in via system.config's unconditional
    load_dotenv() because conftest.py pre-stubbed every other secret except
    this one. conftest.py's _DEFAULTS block must stub it to "" too."""
    import os
    assert os.environ.get("ALERT_WEBHOOK_URL", "") == ""


def test_fire_alert_uses_log_sender_when_no_url_configured(mocker):
    from monitoring import alerts
    # Reset cache so _build_sender is called fresh
    alerts._sender_cache[0] = None
    mocker.patch("monitoring.alerts._build_sender",
                 return_value=alerts.LogAlertSender())
    post_spy = mocker.patch("monitoring.alerts.requests.post")
    alerts.fire_alert("startup", "No webhook", {})
    post_spy.assert_not_called()
