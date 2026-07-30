"""Tests for sentinel.config.load_config."""

import json

import pytest

from sentinel.config import SentinelConfig, load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure secrets-via-env tests start from a clean slate."""
    for key in (
        "SENTINEL_CONFIG",
        "SENTINEL_TELEGRAM_BOT_TOKEN",
        "SENTINEL_TELEGRAM_CHAT_ID",
        "SENTINEL_WEBHOOK_URLS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "does-not-exist.json"))
    assert isinstance(cfg, SentinelConfig)
    assert cfg.poll_interval_seconds == 10
    assert cfg.triggers.cpu_percent == 90.0
    assert cfg.notifications.telegram.enabled is False


def test_json_overrides_are_applied(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "poll_interval_seconds": 5,
        "disk_paths": ["/", "/var"],
        "triggers": {
            "cpu_percent": 70.0,
            "memory_warning_available_mb": 2048.0,
            "disk_critical_free_gb": 2.0,
        },
        "notification_policy": {
            "critical_repeat_seconds": 30,
            "delivery_retry_seconds": [5, 10],
            "state_file": "/tmp/sentinel-test-state.json",
        },
    }))
    cfg = load_config(str(path))
    assert cfg.poll_interval_seconds == 5
    assert cfg.disk_paths == ["/", "/var"]
    assert cfg.triggers.cpu_percent == 70.0
    assert cfg.triggers.memory_warning_available_mb == 2048.0
    assert cfg.triggers.disk_critical_free_gb == 2.0
    # Unspecified fields keep their defaults
    assert cfg.triggers.disk_warning_free_gb == 10.0
    assert cfg.notification_policy.critical_repeat_seconds == 30
    assert cfg.notification_policy.delivery_retry_seconds == [5, 10]


def test_telegram_secrets_loaded_from_env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "notifications": {"telegram": {"enabled": True}}
    }))
    monkeypatch.setenv("SENTINEL_TELEGRAM_BOT_TOKEN", "abc:123")
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100")

    cfg = load_config(str(path))
    assert cfg.notifications.telegram.enabled is True
    assert cfg.notifications.telegram.bot_token == "abc:123"
    assert cfg.notifications.telegram.chat_id == "-100"


def test_webhook_urls_merged_from_env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "actions": {"webhook_urls": ["https://hook.example/a"]}
    }))
    monkeypatch.setenv(
        "SENTINEL_WEBHOOK_URLS",
        "https://hook.example/b, https://hook.example/c",
    )

    cfg = load_config(str(path))
    assert cfg.actions.webhook_urls == [
        "https://hook.example/a",
        "https://hook.example/b",
        "https://hook.example/c",
    ]


def test_sentinel_config_env_var_is_respected(tmp_path, monkeypatch):
    path = tmp_path / "alt.json"
    path.write_text(json.dumps({"poll_interval_seconds": 99}))
    monkeypatch.setenv("SENTINEL_CONFIG", str(path))

    cfg = load_config()  # no explicit path
    assert cfg.poll_interval_seconds == 99
