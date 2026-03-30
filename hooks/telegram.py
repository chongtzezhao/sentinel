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
import urllib.request
import urllib.error

from sentinel.triggers import Alert

TELEGRAM_API = "https://api.telegram.org"


def send(alert: Alert, bot_token: str, chat_id: str, logger: logging.Logger) -> None:
    """Send an alert message to a Telegram chat."""
    text = (
        f"🚨 *Sentinel Alert*\n"
        f"*Level:* {alert.level.value}\n"
        f"*Metric:* `{alert.metric}`\n"
        f"*Value:* {alert.current_value} (threshold: {alert.threshold})\n"
        f"*Message:* {alert.message}"
    )

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Telegram notification sent (status %d)", resp.status)
    except (urllib.error.URLError, OSError) as exc:
        logger.error("Telegram notification failed: %s", exc)
