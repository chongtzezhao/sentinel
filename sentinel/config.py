"""Configuration loading and validation for Sentinel."""

import json
import os
from dataclasses import dataclass, field


@dataclass
class TriggerConfig:
    """Thresholds that trigger actions."""
    cpu_percent: float = 90.0
    memory_warning_available_mb: float = 1024.0
    memory_critical_available_mb: float = 50.0
    disk_warning_free_gb: float = 10.0
    disk_critical_free_gb: float = 1.0
    disk_growth_mb_per_sec: float = 50.0


@dataclass
class ActionConfig:
    """Actions to perform when triggers fire."""
    kill_processes: list[str] = field(default_factory=list)
    restart_processes: list[str] = field(default_factory=list)
    webhook_urls: list[str] = field(default_factory=list)
    process_whitelist: list[str] = field(default_factory=lambda: [
        "systemd", "init", "sshd", "kernel"
    ])


@dataclass
class NotificationPolicyConfig:
    """Stateful notification, retry, and reminder settings."""
    critical_repeat_seconds: int = 1800
    persistent_repeat_seconds: int = 21600
    rapid_repeat_count: int = 3
    delivery_retry_seconds: list[int] = field(
        default_factory=lambda: [60, 300, 900, 1800]
    )
    send_recovery: bool = True
    state_file: str = "/var/lib/sentinel/alert-state.json"


@dataclass
class LogConfig:
    """Logging configuration."""
    log_file: str = "/var/log/sentinel.log"
    log_format: str = "json"  # "json" or "text"
    log_to_file: bool = False
    max_log_size_mb: int = 10
    backup_count: int = 2


@dataclass
class TelegramConfig:
    """Telegram Bot API notification settings."""
    enabled: bool = False
    bot_token: str = ""  # from SENTINEL_TELEGRAM_BOT_TOKEN env var
    chat_id: str = ""    # from SENTINEL_TELEGRAM_CHAT_ID env var


@dataclass
class NotificationsConfig:
    """Notification hook settings."""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


@dataclass
class SentinelConfig:
    """Top-level Sentinel configuration."""
    poll_interval_seconds: int = 10
    disk_paths: list[str] = field(default_factory=lambda: ["/"])
    top_process_count: int = 5
    triggers: TriggerConfig = field(default_factory=TriggerConfig)
    actions: ActionConfig = field(default_factory=ActionConfig)
    notification_policy: NotificationPolicyConfig = field(
        default_factory=NotificationPolicyConfig
    )
    log: LogConfig = field(default_factory=LogConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


def load_config(path: str | None = None) -> SentinelConfig:
    """Load configuration from a JSON file, falling back to defaults."""
    if path is None:
        path = os.environ.get("SENTINEL_CONFIG", "sentinel_config.json")

    if not os.path.isfile(path):
        return SentinelConfig()

    with open(path, "r") as f:
        raw = json.load(f)

    config = SentinelConfig()

    # Top-level scalars
    config.poll_interval_seconds = raw.get("poll_interval_seconds", config.poll_interval_seconds)
    config.disk_paths = raw.get("disk_paths", config.disk_paths)
    config.top_process_count = raw.get("top_process_count", config.top_process_count)

    # Triggers
    if "triggers" in raw:
        t = raw["triggers"]
        config.triggers = TriggerConfig(
            cpu_percent=t.get("cpu_percent", 90.0),
            memory_warning_available_mb=t.get(
                "memory_warning_available_mb", 1024.0
            ),
            memory_critical_available_mb=t.get(
                "memory_critical_available_mb", 50.0
            ),
            disk_warning_free_gb=t.get("disk_warning_free_gb", 10.0),
            disk_critical_free_gb=t.get("disk_critical_free_gb", 1.0),
            disk_growth_mb_per_sec=t.get("disk_growth_mb_per_sec", 50.0),
        )

    # Actions
    if "actions" in raw:
        a = raw["actions"]
        config.actions = ActionConfig(
            kill_processes=a.get("kill_processes", []),
            restart_processes=a.get("restart_processes", []),
            webhook_urls=a.get("webhook_urls", []),
            process_whitelist=a.get("process_whitelist", [
                "systemd", "init", "sshd", "kernel"
            ]),
        )

    # Stateful notification/reminder policy
    if "notification_policy" in raw:
        p = raw["notification_policy"]
        config.notification_policy = NotificationPolicyConfig(
            critical_repeat_seconds=p.get("critical_repeat_seconds", 1800),
            persistent_repeat_seconds=p.get("persistent_repeat_seconds", 21600),
            rapid_repeat_count=p.get("rapid_repeat_count", 3),
            delivery_retry_seconds=p.get(
                "delivery_retry_seconds", [60, 300, 900, 1800]
            ),
            send_recovery=p.get("send_recovery", True),
            state_file=p.get(
                "state_file", "/var/lib/sentinel/alert-state.json"
            ),
        )

    # Log
    if "log" in raw:
        lg = raw["log"]
        config.log = LogConfig(
            log_file=lg.get("log_file", "/var/log/sentinel.log"),
            log_format=lg.get("log_format", "json"),
            log_to_file=lg.get("log_to_file", False),
            max_log_size_mb=lg.get("max_log_size_mb", 10),
            backup_count=lg.get("backup_count", 2),
        )

    # Notifications – enabled flags from config, secrets from env vars
    if "notifications" in raw:
        n = raw["notifications"]
        tg = n.get("telegram", {})
        config.notifications = NotificationsConfig(
            telegram=TelegramConfig(
                enabled=tg.get("enabled", False),
                bot_token=os.environ.get("SENTINEL_TELEGRAM_BOT_TOKEN", ""),
                chat_id=os.environ.get("SENTINEL_TELEGRAM_CHAT_ID", ""),
            ),
        )

    # Webhook URLs – config file list merged with env var (comma-separated)
    env_urls = os.environ.get("SENTINEL_WEBHOOK_URLS", "")
    if env_urls:
        extra = [u.strip() for u in env_urls.split(",") if u.strip()]
        config.actions.webhook_urls.extend(extra)

    return config
