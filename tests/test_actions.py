"""Tests for sentinel.actions orchestration and helpers."""

import logging
from types import SimpleNamespace

import pytest

import sentinel.actions as actions_mod
from sentinel.actions import (
    dispatch_notification,
    kill_processes,
    send_webhooks,
)
from sentinel.config import (
    ActionConfig,
    NotificationsConfig,
    TelegramConfig,
)
from sentinel.state import NotificationEvent
from sentinel.triggers import Alert, AlertLevel


@pytest.fixture
def logger():
    log = logging.getLogger("sentinel-test")
    log.addHandler(logging.NullHandler())
    return log


def _alert(metric="cpu", value=99.0):
    return Alert(
        metric=metric,
        level=AlertLevel.CRITICAL,
        message=f"{metric} hot",
        current_value=value,
        threshold=90.0,
    )


def _event(kind="alert"):
    return NotificationEvent(
        metric="cpu",
        kind=kind,
        alert=_alert(),
        first_seen=100.0,
        event_time=200.0,
        reason="initial",
    )


class _FakeProc:
    def __init__(self, pid, name):
        self.pid = pid
        self.info = {"pid": pid, "name": name}


def test_kill_processes_skips_whitelisted(monkeypatch, logger):
    procs = [_FakeProc(1, "systemd"), _FakeProc(42, "rogue")]
    monkeypatch.setattr(actions_mod.psutil, "process_iter", lambda *_: iter(procs))

    killed = []
    monkeypatch.setattr(actions_mod.os, "kill", lambda pid, sig: killed.append(pid))

    kill_processes(["systemd", "rogue"], whitelist=["systemd"], logger=logger)
    assert killed == [42]


def test_kill_processes_is_case_insensitive(monkeypatch, logger):
    procs = [_FakeProc(7, "MyApp")]
    monkeypatch.setattr(actions_mod.psutil, "process_iter", lambda *_: iter(procs))
    killed = []
    monkeypatch.setattr(actions_mod.os, "kill", lambda pid, sig: killed.append(pid))

    kill_processes(["myapp"], whitelist=[], logger=logger)
    assert killed == [7]


def test_send_webhooks_encodes_metric_and_value(monkeypatch, logger):
    captured = {}

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(actions_mod.urllib.request, "urlopen", fake_urlopen)
    assert send_webhooks(
        ["https://hook.example/x"], _alert(metric="cpu", value=97.5), logger
    )

    assert "metric=cpu" in captured["url"]
    assert "value=97.5" in captured["url"]
    assert "level=critical" in captured["url"]


def test_send_webhooks_logs_and_continues_on_error(monkeypatch, logger):
    def boom(*_a, **_kw):
        raise OSError("connection refused")
    monkeypatch.setattr(actions_mod.urllib.request, "urlopen", boom)

    assert not send_webhooks(["https://hook.example/x"], _alert(), logger)


def test_recovery_does_not_run_process_actions(monkeypatch, logger):
    called = []
    monkeypatch.setattr(actions_mod, "kill_processes",
                        lambda *a, **k: called.append("kill"))
    action_cfg = ActionConfig(kill_processes=["foo"])
    assert dispatch_notification(
        _event(kind="recovery"), action_cfg, NotificationsConfig(), logger
    )
    assert called == []


def test_dispatch_notification_returns_telegram_result(monkeypatch, logger):
    sent = []
    import hooks.telegram as tg_mod

    def fake_send(alert, token, chat, log, **kwargs):
        sent.append((token, chat, alert.metric, kwargs["kind"]))
        return SimpleNamespace(success=True)

    monkeypatch.setattr(tg_mod, "send", fake_send)

    notifs = NotificationsConfig(
        telegram=TelegramConfig(enabled=True, bot_token="t", chat_id="c"),
    )
    assert dispatch_notification(_event(), ActionConfig(), notifs, logger)

    assert sent == [("t", "c", "cpu", "alert")]


def test_dispatch_notification_fails_when_telegram_unconfigured(logger, caplog):
    notifs = NotificationsConfig(
        telegram=TelegramConfig(enabled=True, bot_token="", chat_id=""),
    )

    with caplog.at_level(logging.ERROR, logger="sentinel-test"):
        result = dispatch_notification(_event(), ActionConfig(), notifs, logger)

    assert result is False
    messages = [r.message for r in caplog.records]
    assert any("Telegram enabled but missing" in m for m in messages)
