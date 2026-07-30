"""Tests for persistent alert lifecycle and reminder scheduling."""

from sentinel.config import NotificationPolicyConfig
from sentinel.state import AlertStateStore
from sentinel.triggers import Alert, AlertLevel


def _alert(level=AlertLevel.CRITICAL, metric="disk:/") -> Alert:
    return Alert(
        metric=metric,
        level=level,
        message="disk low",
        current_value=0.5,
        threshold=1.0,
        unit="GiB available",
    )


def _store(tmp_path, now):
    config = NotificationPolicyConfig(
        critical_repeat_seconds=30,
        persistent_repeat_seconds=60,
        rapid_repeat_count=3,
        delivery_retry_seconds=[5, 10, 20],
        state_file=str(tmp_path / "state.json"),
    )
    return AlertStateStore(config, now_fn=lambda: now[0])


def test_new_alert_is_due_immediately(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    events = store.sync([_alert()])
    assert len(events) == 1
    assert events[0].kind == "alert"
    assert events[0].reason == "initial"


def test_failed_delivery_retries_without_counting_success(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, False)

    now[0] += 4
    assert store.sync([_alert()]) == []
    now[0] += 1
    retry = store.sync([_alert()])
    assert len(retry) == 1
    assert retry[0].reason == "delivery-retry"
    assert store.states["disk:/"].successful_notifications == 0


def test_critical_reminders_slow_after_rapid_repeats(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)

    for _ in range(3):
        event = store.sync([_alert()])[0]
        store.record_result(event, True)
        now[0] += 30

    assert store.sync([_alert()]) == []
    now[0] += 30
    reminder = store.sync([_alert()])
    assert len(reminder) == 1
    assert reminder[0].reason == "reminder"


def test_warning_escalation_is_due_immediately(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert(AlertLevel.WARNING)])[0]
    store.record_result(event, True)

    now[0] += 1
    escalated = store.sync([_alert(AlertLevel.CRITICAL)])
    assert len(escalated) == 1
    assert escalated[0].reason == "state-change"


def test_recovery_is_sent_once_then_state_is_removed(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)

    now[0] += 90
    recovery = store.sync([])
    assert len(recovery) == 1
    assert recovery[0].kind == "recovery"
    store.record_result(recovery[0], True)
    assert "disk:/" not in store.states
    assert store.sync([]) == []


def test_state_survives_restart(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)

    reloaded = _store(tmp_path, now)
    assert reloaded.sync([_alert()]) == []
    assert reloaded.states["disk:/"].successful_notifications == 1


def test_failed_recovery_is_retried(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)
    recovery = store.sync([])[0]
    store.record_result(recovery, False)

    now[0] += 5
    retry = store.sync([])
    assert len(retry) == 1
    assert retry[0].kind == "recovery"
    assert retry[0].reason == "delivery-retry"
