"""Alerting integration for critical CI agent events.

Supports webhook (generic HTTP POST) and Slack-compatible notifications.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger(__name__)


@dataclass
class AlertConfig:
    """Configuration for alert destinations."""

    webhook_url: str = ""
    slack_webhook_url: str = ""
    enabled: bool = False
    min_severity: str = "high"  # "low", "medium", "high", "critical"

    _SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    def meets_severity(self, severity: str) -> bool:
        return self._SEVERITY_RANK.get(severity, 0) >= self._SEVERITY_RANK.get(
            self.min_severity, 2
        )


@dataclass
class Alert:
    """A single alert event."""

    title: str
    message: str
    severity: str = "high"
    source: str = "valkey-ci-agent"
    metadata: dict[str, Any] = field(default_factory=dict)


class AlertDispatcher:
    """Dispatches alerts to configured destinations."""

    def __init__(self, config: AlertConfig) -> None:
        self._config = config

    def send(self, alert: Alert) -> bool:
        """Send an alert. Returns True if at least one destination succeeded."""
        if not self._config.enabled:
            return False
        if not self._config.meets_severity(alert.severity):
            logger.debug("Alert '%s' below severity threshold, skipping.", alert.title)
            return False

        sent = False
        if self._config.slack_webhook_url:
            sent = self._send_slack(alert) or sent
        if self._config.webhook_url:
            sent = self._send_webhook(alert) or sent
        return sent

    def _send_slack(self, alert: Alert) -> bool:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
            alert.severity, "⚪"
        )
        payload = {
            "text": f"{icon} *{alert.title}*\n{alert.message}",
            "username": alert.source,
        }
        return self._post_json(self._config.slack_webhook_url, payload)

    def _send_webhook(self, alert: Alert) -> bool:
        payload = {
            "title": alert.title,
            "message": alert.message,
            "severity": alert.severity,
            "source": alert.source,
            "metadata": alert.metadata,
        }
        return self._post_json(self._config.webhook_url, payload)

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any]) -> bool:
        data = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=10):
                return True
        except (urllib_error.URLError, OSError) as exc:
            logger.warning("Alert delivery failed to %s: %s", url, exc)
            return False
