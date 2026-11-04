"""Tests for sentinel.actions orchestration and helpers."""

import logging
from types import SimpleNamespace

import pytest

import sentinel.actions as actions_mod
from sentinel.actions import (
    dispatch_notification,
    kill_processes,
    restart_processes,
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
    url = "https://hook.example/x?a=1&b=2"
    ok, delivered = send_webhooks(
        [url], _alert(metric="disk:/", value=97.5), logger
    )

    assert ok is True
    assert delivered == {url}
    assert captured["url"].count("?") == 1
    assert "a=1" in captured["url"]
    assert "b=2" in captured["url"]
    assert "metric=disk%3A%2F" in captured["url"]
    assert "value=97.5" in captured["url"]
    assert "level=critical" in captured["url"]


def test_send_webhooks_logs_and_continues_on_error(monkeypatch, logger):
    def boom(*_a, **_kw):
        raise OSError("connection refused")
    monkeypatch.setattr(actions_mod.urllib.request, "urlopen", boom)

    ok, delivered = send_webhooks(
        ["https://hook.example/x"], _alert(), logger
    )
    assert ok is False
    assert delivered == set()


def test_send_webhooks_masks_url_secrets(monkeypatch, logger, caplog):
    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        actions_mod.urllib.request, "urlopen",
        lambda *_a, **_kw: _FakeResp(),
    )
    url = "https://hooks.example/services/SECRET?token=abc"

    with caplog.at_level(logging.INFO, logger="sentinel-test"):
        ok, _ = send_webhooks([url], _alert(), logger)

    assert ok is True
    assert "SECRET" not in caplog.text
    assert "token=abc" not in caplog.text
    assert "hooks.example" in caplog.text


def test_send_webhooks_hides_secrets_in_error_text(
    monkeypatch, logger, caplog
):
    def boom(req, timeout=None):
        raise ValueError(f"bad url {req.full_url}")

    monkeypatch.setattr(actions_mod.urllib.request, "urlopen", boom)
    url = "https://hooks.example/services/SECRET?token=a%2fb"

    with caplog.at_level(logging.ERROR, logger="sentinel-test"):
        ok, _ = send_webhooks([url], _alert(), logger)

    assert ok is False
    assert "SECRET" not in caplog.text
    assert "token" not in caplog.text
    assert "a%2f" not in caplog.text.lower()


def test_send_webhooks_handles_malformed_url(logger):
    ok, delivered = send_webhooks(["not a url"], _alert(), logger)
    assert ok is False
    assert delivered == set()


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

    assert result.success is False
    messages = [r.message for r in caplog.records]
    assert any("Telegram enabled but missing" in m for m in messages)


def test_recovery_logs_the_clearing_value(logger, caplog):
    alert = Alert(
        metric="cpu",
        level=AlertLevel.CRITICAL,
        message="CPU usage at 95.0% (threshold: 90.0%)",
        current_value=74.2,
        threshold=90.0,
        unit="% usage",
    )
    event = NotificationEvent(
        metric="cpu",
        kind="recovery",
        alert=alert,
        first_seen=100.0,
        event_time=112.0,
        reason="state-change",
    )

    with caplog.at_level(logging.INFO, logger="sentinel-test"):
        assert dispatch_notification(
            event, ActionConfig(), NotificationsConfig(), logger
        )

    assert any("now 74.2%" in record.message for record in caplog.records)
    assert any("previous: CPU usage at 95.0%" in record.message for record in caplog.records)


def test_hysteresis_band_alert_does_not_run_process_actions(monkeypatch, logger):
    called = []
    monkeypatch.setattr(
        actions_mod, "kill_processes",
        lambda *a, **k: called.append("kill"),
    )
    alert = _alert()
    alert.breaching = False
    event = NotificationEvent(
        metric="cpu",
        kind="alert",
        alert=alert,
        first_seen=100.0,
        event_time=200.0,
        reason="reminder",
    )

    assert dispatch_notification(
        event, ActionConfig(kill_processes=["foo"]), NotificationsConfig(), logger
    )
    assert called == []


def test_dispatch_notification_propagates_retry_after(monkeypatch, logger):
    import hooks.telegram as tg_mod

    def fake_send(alert, token, chat, log, **kwargs):
        return SimpleNamespace(success=False, retry_after=17)

    monkeypatch.setattr(tg_mod, "send", fake_send)

    notifs = NotificationsConfig(
        telegram=TelegramConfig(enabled=True, bot_token="t", chat_id="c"),
    )
    result = dispatch_notification(_event(), ActionConfig(), notifs, logger)

    assert result.success is False
    assert result.retry_after == 17


def test_partial_delivery_tracks_successful_destination(monkeypatch, logger):
    import hooks.telegram as tg_mod

    monkeypatch.setattr(
        tg_mod, "send",
        lambda *a, **k: SimpleNamespace(success=True),
    )
    url = "https://hook.example/x"
    monkeypatch.setattr(
        actions_mod, "send_webhooks",
        lambda urls, alert, log, already=None: (False, set(already or ())),
    )
    notifs = NotificationsConfig(
        telegram=TelegramConfig(enabled=True, bot_token="t", chat_id="c"),
    )

    result = dispatch_notification(
        _event(), ActionConfig(webhook_urls=[url]), notifs, logger
    )

    assert result.success is False
    assert result.delivered == frozenset({"telegram"})


def test_delivered_destination_is_not_resent(monkeypatch, logger):
    import hooks.telegram as tg_mod

    sent = []
    monkeypatch.setattr(
        tg_mod, "send",
        lambda *a, **k: sent.append("telegram") or SimpleNamespace(success=True),
    )
    url = "https://hook.example/x"
    monkeypatch.setattr(
        actions_mod, "send_webhooks",
        lambda urls, alert, log, already=None: (
            True, set(already or ()) | set(urls)
        ),
    )
    event = NotificationEvent(
        metric="cpu",
        kind="alert",
        alert=_alert(),
        first_seen=100.0,
        event_time=200.0,
        reason="delivery-retry",
        delivered_destinations=("telegram",),
    )

    result = dispatch_notification(
        event,
        ActionConfig(webhook_urls=[url]),
        NotificationsConfig(
            telegram=TelegramConfig(enabled=True, bot_token="t", chat_id="c"),
        ),
        logger,
    )

    assert result.success is True
    assert sent == []


def test_restart_processes_prefers_systemd(monkeypatch, logger):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    restart_processes(["nginx"], [], logger)

    assert calls == [
        ["systemctl", "cat", "nginx.service"],
        ["systemctl", "restart", "nginx.service"],
    ]


def test_restart_processes_waits_then_relaunches(monkeypatch, logger):
    class _Proc:
        pid = 42
        info = {"pid": 42, "name": "foo"}

    monkeypatch.setattr(
        actions_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(
        actions_mod.psutil, "process_iter",
        lambda attrs: iter([_Proc()]),
    )
    killed = []
    monkeypatch.setattr(
        actions_mod.os, "kill",
        lambda pid, sig: killed.append(pid),
    )
    monkeypatch.setattr(actions_mod.psutil, "pid_exists", lambda pid: False)
    monkeypatch.setattr(actions_mod.shutil, "which", lambda name: "/usr/bin/foo")
    popen = []
    monkeypatch.setattr(
        actions_mod.subprocess, "Popen",
        lambda *a, **k: popen.append((a, k)),
    )

    restart_processes(["foo"], [], logger)

    assert killed == [42]
    assert popen
    assert popen[0][1]["start_new_session"] is True


def test_restart_processes_does_not_duplicate_a_live_process(monkeypatch, logger):
    class _Proc:
        pid = 42
        info = {"pid": 42, "name": "foo"}

    monkeypatch.setattr(
        actions_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(
        actions_mod.psutil, "process_iter",
        lambda attrs: iter([_Proc()]),
    )
    monkeypatch.setattr(actions_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(actions_mod.psutil, "pid_exists", lambda pid: True)
    times = iter([0, 1, 2, 3, 4, 5, 6])
    monkeypatch.setattr(
        actions_mod.time, "monotonic", lambda: next(times)
    )
    monkeypatch.setattr(actions_mod.time, "sleep", lambda seconds: None)
    popen = []
    monkeypatch.setattr(
        actions_mod.subprocess, "Popen",
        lambda *a, **k: popen.append((a, k)),
    )

    restart_processes(["foo"], [], logger)

    assert popen == []


def test_restart_processes_does_not_fallback_when_systemd_fails(
    monkeypatch, logger
):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1 if cmd[1] == "restart" else 0)

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    popen = []
    monkeypatch.setattr(
        actions_mod.subprocess, "Popen",
        lambda *a, **k: popen.append((a, k)),
    )

    restart_processes(["foo"], [], logger)

    assert calls == [
        ["systemctl", "cat", "foo.service"],
        ["systemctl", "restart", "foo.service"],
    ]
    assert popen == []
