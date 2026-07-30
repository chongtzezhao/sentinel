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
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler

import psutil

from sentinel.config import ActionConfig, LogConfig, NotificationsConfig
from sentinel.state import NotificationEvent
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

    if log_config.log_to_file:
        log_dir = os.path.dirname(log_config.log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        handler = RotatingFileHandler(
            log_config.log_file,
            maxBytes=log_config.max_log_size_mb * 1024 * 1024,
            backupCount=log_config.backup_count,
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


def send_webhooks(urls: list[str], alert: Alert, logger: logging.Logger) -> bool:
    """Send HTTP GET requests to each configured webhook URL."""
    success = True
    for url in urls:
        try:
            full_url = f"{url}?metric={alert.metric}&value={alert.current_value}&level={alert.level.value}"
            req = urllib.request.Request(full_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info("Webhook %s responded %d", url, resp.status)
        except (urllib.error.URLError, OSError) as exc:
            logger.error("Webhook %s failed: %s", url, exc)
            success = False
    return success


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def dispatch_notification(
    event: NotificationEvent,
    action_config: ActionConfig,
    notifications: NotificationsConfig,
    logger: logging.Logger,
) -> bool:
    """Execute one due alert/recovery event and report delivery success."""
    alert = event.alert
    if event.kind == "recovery":
        logger.info(
            "RECOVERY %s after %.0fs: %s",
            alert.metric,
            event.duration_seconds,
            alert.message,
        )
    else:
        logger.warning(
            "ALERT [%s] %s (notification reason: %s)",
            alert.level.value,
            alert.message,
            event.reason,
        )

    if event.kind == "alert":
        # Kill configured processes
        if action_config.kill_processes:
            kill_processes(action_config.kill_processes, action_config.process_whitelist, logger)

        # Restart configured processes
        if action_config.restart_processes:
            restart_processes(action_config.restart_processes, action_config.process_whitelist, logger)

    results: list[bool] = []

    if action_config.webhook_urls:
        results.append(send_webhooks(action_config.webhook_urls, alert, logger))

    tg = notifications.telegram
    if tg.enabled and tg.bot_token and tg.chat_id:
        from hooks.telegram import send as telegram_send

        result = telegram_send(
            alert,
            tg.bot_token,
            tg.chat_id,
            logger,
            kind=event.kind,
            first_seen=event.first_seen,
            duration_seconds=event.duration_seconds,
        )
        results.append(result.success)
    elif tg.enabled:
        logger.error(
            "Telegram enabled but missing SENTINEL_TELEGRAM_BOT_TOKEN "
            "or SENTINEL_TELEGRAM_CHAT_ID"
        )
        results.append(False)

    # With no external destinations, the state transition was still handled
    # locally and should not be retried forever.
    return all(results) if results else True
