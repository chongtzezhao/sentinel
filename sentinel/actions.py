"""Action execution for Sentinel.

When triggers fire, this module performs the configured responses:
kill/restart processes, send HTTP webhooks, and log events.
"""

import json
import logging
import os
import signal
import subprocess
import time
import urllib.request
import urllib.error
from logging.handlers import RotatingFileHandler

import psutil

from sentinel.config import ActionConfig, CooldownConfig, LogConfig, NotificationsConfig
from sentinel.triggers import Alert


# ---------------------------------------------------------------------------
# Sentinel Logger
# ---------------------------------------------------------------------------

def setup_logger(log_config: LogConfig) -> logging.Logger:
    """Create and return a configured logger for Sentinel events."""
    logger = logging.getLogger("sentinel")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    # Ensure log directory exists
    log_dir = os.path.dirname(log_config.log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    handler = RotatingFileHandler(
        log_config.log_file,
        maxBytes=log_config.max_log_size_mb * 1024 * 1024,
        backupCount=3,
    )

    if log_config.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))

    logger.addHandler(handler)

    # Also log to stderr for visibility when running interactively
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console)

    return logger


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_data"):
            entry["data"] = record.extra_data
        return json.dumps(entry)


# ---------------------------------------------------------------------------
# Cooldown Tracker
# ---------------------------------------------------------------------------

class CooldownTracker:
    """Prevents repeated actions on the same metric within a cooldown window."""

    def __init__(self, config: CooldownConfig):
        self._cooldown_sec = config.cooldown_seconds
        self._max_retries = config.max_retries
        # metric -> (last_action_timestamp, consecutive_count)
        self._state: dict[str, tuple[float, int]] = {}

    def can_act(self, metric: str) -> bool:
        """Return True if the cooldown has expired for this metric."""
        if metric not in self._state:
            return True
        last_ts, count = self._state[metric]
        if count >= self._max_retries:
            return False
        return (time.time() - last_ts) >= self._cooldown_sec

    def record(self, metric: str) -> None:
        """Record that an action was taken for this metric."""
        now = time.time()
        if metric in self._state:
            _, count = self._state[metric]
            self._state[metric] = (now, count + 1)
        else:
            self._state[metric] = (now, 1)

    def reset(self, metric: str) -> None:
        """Reset the cooldown state when the alert clears."""
        self._state.pop(metric, None)


# ---------------------------------------------------------------------------
# Action Executors
# ---------------------------------------------------------------------------

def kill_processes(names: list[str], whitelist: list[str], logger: logging.Logger) -> None:
    """Send SIGTERM to processes matching *names*, skipping whitelisted ones."""
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = (proc.info["name"] or "").lower()
            if pname in (n.lower() for n in names):
                if pname in (w.lower() for w in whitelist):
                    logger.warning("Skipping whitelisted process: %s (pid %d)", pname, proc.pid)
                    continue
                logger.info("Sending SIGTERM to %s (pid %d)", pname, proc.pid)
                os.kill(proc.pid, signal.SIGTERM)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            continue


def restart_processes(names: list[str], whitelist: list[str], logger: logging.Logger) -> None:
    """Attempt to restart processes by killing and re-launching them.

    This uses a simple approach: SIGTERM the process, then try to start it
    again via ``subprocess.Popen``.  For production use, prefer systemd
    service restarts.
    """
    for name in names:
        if name.lower() in (w.lower() for w in whitelist):
            logger.warning("Skipping whitelisted process for restart: %s", name)
            continue

        # Find and kill
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() == name.lower():
                    logger.info("Killing %s (pid %d) for restart", name, proc.pid)
                    os.kill(proc.pid, signal.SIGTERM)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue

        # Brief pause then re-launch
        time.sleep(1)
        try:
            logger.info("Re-launching process: %s", name)
            subprocess.Popen(
                [name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("Cannot restart %s: executable not found in PATH", name)
        except OSError as exc:
            logger.error("Cannot restart %s: %s", name, exc)


def send_webhooks(urls: list[str], alert: Alert, logger: logging.Logger) -> None:
    """Send HTTP GET requests to each configured webhook URL."""
    for url in urls:
        try:
            full_url = f"{url}?metric={alert.metric}&value={alert.current_value}&level={alert.level.value}"
            req = urllib.request.Request(full_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info("Webhook %s responded %d", url, resp.status)
        except (urllib.error.URLError, OSError) as exc:
            logger.error("Webhook %s failed: %s", url, exc)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def handle_alerts(
    alerts: list[Alert],
    action_config: ActionConfig,
    notifications: NotificationsConfig,
    cooldown: CooldownTracker,
    logger: logging.Logger,
) -> None:
    """Process a batch of alerts: log, enforce cooldowns, and execute actions."""
    for alert in alerts:
        logger.warning("ALERT [%s] %s", alert.level.value, alert.message)

        if not cooldown.can_act(alert.metric):
            logger.info("Cooldown active for %s – skipping actions", alert.metric)
            continue

        # Kill configured processes
        if action_config.kill_processes:
            kill_processes(action_config.kill_processes, action_config.process_whitelist, logger)

        # Restart configured processes
        if action_config.restart_processes:
            restart_processes(action_config.restart_processes, action_config.process_whitelist, logger)

        # Webhooks
        if action_config.webhook_urls:
            send_webhooks(action_config.webhook_urls, alert, logger)

        # Notification hooks
        tg = notifications.telegram
        if tg.enabled and tg.bot_token and tg.chat_id:
            from hooks.telegram import send as telegram_send
            telegram_send(alert, tg.bot_token, tg.chat_id, logger)
        elif tg.enabled and (not tg.bot_token or not tg.chat_id):
            logger.warning("Telegram enabled but missing SENTINEL_TELEGRAM_BOT_TOKEN or SENTINEL_TELEGRAM_CHAT_ID")

        cooldown.record(alert.metric)
