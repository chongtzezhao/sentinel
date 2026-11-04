"""Action execution for Sentinel.

When triggers fire, this module performs the configured responses:
kill/restart processes, send HTTP webhooks, and log events.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import psutil

from sentinel.config import ActionConfig, LogConfig, NotificationsConfig
from sentinel.state import NotificationEvent
from sentinel.triggers import Alert, format_value

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


def _restart_via_systemd(name: str, logger: logging.Logger) -> bool | None:
    """Restart a named systemd unit when one exists.

    Returns None when *name* is not a unit, True on success, and False when a
    unit exists but the restart failed. Callers must not fall back on False,
    or they could start a duplicate copy.
    """
    unit = name if "." in name else f"{name}.service"
    try:
        probe = subprocess.run(
            ["systemctl", "cat", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if probe.returncode != 0:
        return None

    try:
        result = subprocess.run(
            ["systemctl", "restart", unit],
            check=False,
        )
    except OSError as exc:
        logger.error("Cannot restart %s via systemd: %s", unit, exc)
        return False
    if result.returncode != 0:
        logger.error(
            "systemctl restart %s failed (status %d)",
            unit,
            result.returncode,
        )
        return False
    logger.info("Restarted %s via systemd", unit)
    return True


def _terminate_and_wait(
    pids: list[int],
    name: str,
    logger: logging.Logger,
    timeout: float = 5.0,
) -> bool:
    """SIGTERM *pids* and wait for them to exit."""
    for pid in pids:
        try:
            logger.info("Killing %s (pid %d) for restart", name, pid)
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(psutil.pid_exists(pid) for pid in pids):
            return True
        time.sleep(0.1)
    logger.error(
        "Process %s did not exit within %.0fs; not starting a duplicate",
        name,
        timeout,
    )
    return False


def restart_processes(names: list[str], whitelist: list[str], logger: logging.Logger) -> None:
    """Restart processes by unit where possible, else by executable name.

    For systemd-managed services, ``systemctl restart <unit>`` is preferred.
    The fallback starts the executable in a new session so it survives a
    Sentinel restart. It cannot supply service-specific args, user, or env;
    use a systemd unit for those.
    """
    for name in names:
        if name.lower() in (w.lower() for w in whitelist):
            logger.warning("Skipping whitelisted process for restart: %s", name)
            continue

        systemd_result = _restart_via_systemd(name, logger)
        if systemd_result is not None:
            continue

        pids: list[int] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").lower() == name.lower():
                    pids.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if pids and not _terminate_and_wait(pids, name, logger):
            continue

        executable = shutil.which(name)
        if executable is None:
            logger.error(
                "Cannot restart %s: executable not found in PATH", name
            )
            continue

        try:
            logger.info("Re-launching process: %s", name)
            subprocess.Popen(
                [executable],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            logger.error("Cannot restart %s: %s", name, exc)


def _mask_url(url: str) -> str:
    """Return a log-safe webhook URL without path or query secrets."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or "unknown"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    scheme = parts.scheme or "?"
    return f"{scheme}://{host}{port}/…"


def _webhook_url(url: str, alert: Alert) -> str:
    """Append alert fields to an existing query string safely."""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update({
        "metric": alert.metric,
        "value": f"{alert.current_value}",
        "level": alert.level.value,
    })
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urllib.parse.urlencode(query),
        parts.fragment,
    ))


def send_webhooks(
    urls: list[str],
    alert: Alert,
    logger: logging.Logger,
    already_delivered: set[str] | None = None,
) -> tuple[bool, set[str]]:
    """Send HTTP GET requests to each configured webhook URL.

    Returns ``(all_succeeded, delivered_urls)`` so callers can skip
    destinations that already accepted the same event on a retry.
    """
    delivered = set(already_delivered or ())
    success = True
    for url in urls:
        if url in delivered:
            continue
        try:
            full_url = _webhook_url(url, alert)
            req = urllib.request.Request(full_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(
                    "Webhook %s responded %d", _mask_url(url), resp.status
                )
                delivered.add(url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            status = getattr(exc, "code", None)
            logger.error(
                "Webhook %s failed%s: %s",
                _mask_url(url),
                f" (status {status})" if status else "",
                type(exc).__name__,
            )
            success = False
    return success, delivered


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one notification dispatch."""
    success: bool
    retry_after: int | None = None
    delivered: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return self.success


def dispatch_notification(
    event: NotificationEvent,
    action_config: ActionConfig,
    notifications: NotificationsConfig,
    logger: logging.Logger,
) -> DeliveryResult:
    """Execute one due alert/recovery event and report delivery outcome."""
    alert = event.alert
    if event.kind == "recovery":
        if event.undelivered_alert:
            logger.warning(
                "RECOVERY %s after %.0fs: alert was never delivered; now %s "
                "(previous: %s)",
                alert.metric,
                event.duration_seconds,
                format_value(alert),
                alert.message,
            )
        else:
            logger.info(
                "RECOVERY %s after %.0fs: now %s (previous: %s)",
                alert.metric,
                event.duration_seconds,
                format_value(alert),
                alert.message,
            )
    else:
        logger.warning(
            "ALERT [%s] %s (notification reason: %s)",
            alert.level.value,
            alert.message,
            event.reason,
        )

    if event.kind == "alert" and alert.breaching:
        # Kill configured processes
        if action_config.kill_processes:
            kill_processes(action_config.kill_processes, action_config.process_whitelist, logger)

        # Restart configured processes
        if action_config.restart_processes:
            restart_processes(action_config.restart_processes, action_config.process_whitelist, logger)

    results: list[bool] = []
    retry_after: int | None = None
    delivered: set[str] = set(event.delivered_destinations)

    if action_config.webhook_urls:
        webhook_ok, webhook_delivered = send_webhooks(
            action_config.webhook_urls,
            alert,
            logger,
            delivered,
        )
        results.append(webhook_ok)
        delivered |= webhook_delivered

    tg = notifications.telegram
    if tg.enabled and tg.bot_token and tg.chat_id:
        if "telegram" in delivered:
            results.append(True)
        else:
            from hooks.telegram import send as telegram_send

            result = telegram_send(
                alert,
                tg.bot_token,
                tg.chat_id,
                logger,
                kind=event.kind,
                first_seen=event.first_seen,
                duration_seconds=event.duration_seconds,
                undelivered_alert=event.undelivered_alert,
            )
            results.append(result.success)
            if result.success:
                delivered.add("telegram")
            retry_after = getattr(result, "retry_after", None)
    elif tg.enabled:
        logger.error(
            "Telegram enabled but missing SENTINEL_TELEGRAM_BOT_TOKEN "
            "or SENTINEL_TELEGRAM_CHAT_ID"
        )
        results.append(False)

    # With no external destinations, the state transition was still handled
    # locally and should not be retried forever.
    return DeliveryResult(
        all(results) if results else True,
        retry_after=retry_after,
        delivered=frozenset(delivered),
    )
