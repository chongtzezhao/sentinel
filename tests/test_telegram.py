"""Tests for the Telegram notification hook."""

import io
import json
import logging
import urllib.error

import pytest

import hooks.telegram as tg_mod
from sentinel.triggers import Alert, AlertLevel


@pytest.fixture
def logger():
    log = logging.getLogger("sentinel-test-telegram")
    log.addHandler(logging.NullHandler())
    return log


def _alert():
    return Alert(
        metric="cpu",
        level=AlertLevel.CRITICAL,
        message="CPU at 95%",
        current_value=95.0,
        threshold=90.0,
    )


def test_send_posts_json_to_correct_url(monkeypatch, logger):
    captured = {}

    class _Resp:
        status = 200
        def read(self):
            return json.dumps({
                "ok": True,
                "result": {"message_id": 42},
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        return _Resp()

    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", fake_urlopen)
    result = tg_mod.send(
        _alert(), bot_token="TOKEN", chat_id="CHAT", logger=logger
    )

    assert result.success is True
    assert result.message_id == 42
    assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert captured["method"] == "POST"
    assert captured["headers"].get("Content-type") == "application/json"

    body = json.loads(captured["data"])
    assert body["chat_id"] == "CHAT"
    assert "parse_mode" not in body
    assert "cpu" in body["text"]
    assert "CPU at 95%" in body["text"]


def test_send_swallows_url_errors(monkeypatch, logger):
    def boom(*_a, **_kw):
        raise urllib.error.URLError("dns blew up")
    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", boom)

    result = tg_mod.send(_alert(), "TOKEN", "CHAT", logger)
    assert result.success is False
    assert "dns blew up" in result.error


def test_send_rejects_ok_false_response(monkeypatch, logger):
    class _Resp:
        status = 200
        def read(self):
            return json.dumps({"ok": False, "description": "bad destination"}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", lambda *_a, **_kw: _Resp())
    result = tg_mod.send(_alert(), "TOKEN", "CHAT", logger)
    assert result.success is False
    assert result.error == "bad destination"


def test_recovery_reports_the_clearing_value(monkeypatch, logger):
    captured = {}

    class _Resp:
        status = 200
        def read(self):
            return json.dumps({
                "ok": True,
                "result": {"message_id": 43},
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", fake_urlopen)
    alert = Alert(
        metric="cpu",
        level=AlertLevel.CRITICAL,
        message="CPU usage at 95.0% (threshold: 90.0%)",
        current_value=74.2,
        threshold=90.0,
        unit="% usage",
    )

    tg_mod.send(
        alert, "TOKEN", "CHAT", logger,
        kind="recovery", duration_seconds=12.0,
    )

    body = json.loads(captured["data"])
    assert "Now: 74.2%" in body["text"]
    assert "Previous condition: CPU usage at 95.0%" in body["text"]


def test_recovery_before_delivery_uses_a_combined_message(monkeypatch, logger):
    captured = {}

    class _Resp:
        status = 200
        def read(self):
            return json.dumps({
                "ok": True,
                "result": {"message_id": 44},
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", fake_urlopen)
    alert = Alert(
        metric="cpu",
        level=AlertLevel.CRITICAL,
        message="CPU usage at 95.0% (threshold: 90.0%)",
        current_value=74.2,
        threshold=90.0,
        unit="% usage",
    )

    tg_mod.send(
        alert, "TOKEN", "CHAT", logger,
        kind="recovery", duration_seconds=12.0, undelivered_alert=True,
    )

    body = json.loads(captured["data"])
    assert "recovered before delivery" in body["text"]
    assert "Condition: CPU usage at 95.0%" in body["text"]
    assert "Now: 74.2%" in body["text"]


def test_send_honors_429_retry_after(monkeypatch, logger):
    body = json.dumps({
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry later",
        "parameters": {"retry_after": 17},
    }).encode()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", fake_urlopen)
    result = tg_mod.send(_alert(), "TOKEN", "CHAT", logger)

    assert result.success is False
    assert result.retry_after == 17
    assert result.status == 429


def test_send_redacts_bot_token_from_errors(monkeypatch, logger, caplog):
    def boom(*_a, **_kw):
        raise urllib.error.URLError(
            "https://api.telegram.org/botTOKEN/sendMessage failed"
        )

    monkeypatch.setattr(tg_mod.urllib.request, "urlopen", boom)

    with caplog.at_level(logging.ERROR, logger="sentinel-test-telegram"):
        result = tg_mod.send(_alert(), "TOKEN", "CHAT", logger)

    assert "TOKEN" not in caplog.text
    assert "TOKEN" not in (result.error or "")
