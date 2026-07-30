"""Telegram notification hook for Sentinel.

Sends alert messages to a Telegram chat via the Bot API.

Configuration in sentinel_config.json:
  "notifications": {
      "telegram": {
          "bot_token": "123456:ABC-DEF...",
          "chat_id": "-1001234567890"
      }
  }
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from sentinel.triggers import Alert

TELEGRAM_API = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramResult:
    success: bool
    status: int | None = None
    message_id: int | None = None
    error: str | None = None


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def send(
    alert: Alert,
    bot_token: str,
    chat_id: str,
    logger: logging.Logger,
    *,
    kind: str = "alert",
    first_seen: float | None = None,
    duration_seconds: float = 0.0,
) -> TelegramResult:
    """Send an alert or recovery and verify Telegram's JSON response."""
    if kind == "recovery":
        text = (
            "✅ Sentinel Recovery\n"
            f"Metric: {alert.metric}\n"
            f"Condition active for: {_duration(duration_seconds)}\n"
            f"Previous condition: {alert.message}"
        )
    else:
        icon = "🚨" if alert.level.value == "critical" else "⚠️"
        first_seen_text = (
            datetime.fromtimestamp(first_seen, UTC)
            .astimezone()
            .isoformat(timespec="seconds")
            if first_seen is not None
            else "unknown"
        )
        value = f"{alert.current_value:.2f}"
        if alert.unit:
            value = f"{value} {alert.unit}"
        text = (
            f"{icon} Sentinel Alert\n"
            f"Level: {alert.level.value}\n"
            f"Metric: {alert.metric}\n"
            f"Value: {value}\n"
            f"First detected: {first_seen_text}\n"
            f"Active for: {_duration(duration_seconds)}\n"
            f"Message: {alert.message}"
        )

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            try:
                body = json.loads(resp.read().decode())
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.error(
                    "Telegram notification failed: invalid response body (status %d): %s",
                    status,
                    exc,
                )
                return TelegramResult(False, status=status, error="invalid response body")

            if status == 200 and body.get("ok") is True:
                message_id = body.get("result", {}).get("message_id")
                logger.info(
                    "Telegram notification delivered (status %d, message_id %s)",
                    status,
                    message_id if message_id is not None else "unknown",
                )
                return TelegramResult(True, status=status, message_id=message_id)

            description = body.get("description", "Telegram returned ok=false")
            logger.error(
                "Telegram notification failed (status %d): %s",
                status,
                description,
            )
            return TelegramResult(False, status=status, error=description)
    except (urllib.error.URLError, OSError) as exc:
        logger.error("Telegram notification failed: %s", exc)
        status = getattr(exc, "code", None)
        return TelegramResult(False, status=status, error=str(exc))
