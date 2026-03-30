"""Configuration loading and validation for Sentinel."""

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TriggerConfig:
    """Thresholds that trigger actions."""
    cpu_percent: float = 90.0
    memory_percent: float = 85.0
    disk_percent: float = 90.0
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
class CooldownConfig:
    """Cooldown/retry settings to prevent repeated actions."""
    cooldown_seconds: int = 300
    max_retries: int = 3


@dataclass
class LogConfig:
    """Logging configuration."""
    log_file: str = "/var/log/sentinel.log"
    log_format: str = "json"  # "json" or "text"
    max_log_size_mb: int = 50


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
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    log: LogConfig = field(default_factory=LogConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


def load_config(path: Optional[str] = None) -> SentinelConfig:
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
            memory_percent=t.get("memory_percent", 85.0),
            disk_percent=t.get("disk_percent", 90.0),
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

    # Cooldown
    if "cooldown" in raw:
        c = raw["cooldown"]
        config.cooldown = CooldownConfig(
            cooldown_seconds=c.get("cooldown_seconds", 300),
            max_retries=c.get("max_retries", 3),
        )

    # Log
    if "log" in raw:
        lg = raw["log"]
        config.log = LogConfig(
            log_file=lg.get("log_file", "/var/log/sentinel.log"),
            log_format=lg.get("log_format", "json"),
            max_log_size_mb=lg.get("max_log_size_mb", 50),
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
