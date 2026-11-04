"""Tests for persistent alert lifecycle and reminder scheduling."""

import json

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


def test_recovery_reports_the_value_that_cleared(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)

    now[0] += 90
    recovery = store.sync([], {"disk:/": 12.5})[0]
    assert recovery.alert.current_value == 12.5
    # The previous condition stays available for the "Previous condition" line.
    assert recovery.alert.message == "disk low"


def test_invalid_persisted_level_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "states": [{
            "metric": "cpu",
            "level": "CRITICAL",
            "message": "bad level",
            "current_value": 95.0,
            "threshold": 90.0,
            "unit": "% usage",
            "active": True,
            "first_seen": 1.0,
            "last_seen": 2.0,
        }],
    }))

    store = AlertStateStore(
        NotificationPolicyConfig(state_file=str(path)),
        now_fn=lambda: 3.0,
    )
    assert store.states == {}


def test_breaching_flag_survives_state_round_trip(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    alert = _alert()
    alert.breaching = False

    event = store.sync([alert])[0]
    assert event.alert.breaching is False
    store.record_result(event, True)

    # A reminder rebuilt from persisted state must stay non-breaching too.
    now[0] += 30
    reminder = store.sync([alert])[0]
    assert reminder.reason == "reminder"
    assert reminder.alert.breaching is False


def test_recovery_marks_an_undelivered_alert(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, False)

    now[0] += 90
    recovery = store.sync([], {"disk:/": 12.5})[0]
    assert recovery.kind == "recovery"
    assert recovery.undelivered_alert is True


def test_recovery_after_delivery_is_not_marked_undelivered(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)

    now[0] += 90
    recovery = store.sync([], {"disk:/": 12.5})[0]
    assert recovery.kind == "recovery"
    assert recovery.undelivered_alert is False


def test_recovery_marks_a_failed_critical_escalation(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    warning = store.sync([_alert(AlertLevel.WARNING)])[0]
    store.record_result(warning, True)

    now[0] += 1
    critical = store.sync([_alert(AlertLevel.CRITICAL)])[0]
    assert critical.reason == "state-change"
    store.record_result(critical, False)

    now[0] += 90
    recovery = store.sync([], {"disk:/": 12.5})[0]
    assert recovery.kind == "recovery"
    assert recovery.undelivered_alert is True


def test_retry_after_delays_the_next_attempt(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, False, retry_after=17)

    now[0] += 16
    assert store.sync([_alert()]) == []

    now[0] += 1
    retry = store.sync([_alert()])[0]
    assert retry.reason == "delivery-retry"


def test_partial_delivery_persists_successful_destinations(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(
        event, False, delivered=frozenset({"telegram"})
    )

    now[0] += 5
    retry = store.sync([_alert()])[0]
    assert retry.delivered_destinations == ("telegram",)

    store.record_result(retry, True)
    assert store.states["disk:/"].delivered_destinations == []


def test_reactivation_is_persisted(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)

    now[0] += 90
    recovery = store.sync([])[0]
    store.record_result(recovery, False)

    now[0] += 5
    store.sync([_alert()])

    reloaded = _store(tmp_path, now)
    assert reloaded.states["disk:/"].active is True


def test_level_change_is_persisted(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert(AlertLevel.CRITICAL)])[0]
    store.record_result(event, True)

    now[0] += 1
    store.sync([_alert(AlertLevel.WARNING)])

    reloaded = _store(tmp_path, now)
    assert reloaded.states["disk:/"].level == "warning"


def test_stale_recovery_does_not_delete_a_new_state(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    event = store.sync([_alert()])[0]
    store.record_result(event, True)

    recovery = store.sync([])[0]
    now[0] += 1
    new_event = store.sync([_alert()])[0]
    assert new_event.kind == "alert"

    store.record_result(recovery, True)
    assert store.states["disk:/"].active is True


def test_alert_result_after_recovery_is_ignored(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    alert = store.sync([_alert()])[0]

    now[0] += 90
    store.sync([])  # condition recovered while the alert was in flight
    store.record_result(
        alert, True, delivered=frozenset({"telegram"})
    )

    state = store.states["disk:/"]
    assert state.active is False
    assert state.delivered_destinations == []

    # The recovery must still deliver to every destination.
    recovery = store.sync([])[0]
    assert recovery.kind == "recovery"
    assert recovery.delivered_destinations == ()


def test_level_change_resets_destination_progress(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    critical = store.sync([_alert(AlertLevel.CRITICAL)])[0]
    store.record_result(
        critical, False, delivered=frozenset({"telegram"})
    )

    now[0] += 1
    assert store.sync([_alert(AlertLevel.WARNING)]) == []

    now[0] += 4
    retry = store.sync([_alert(AlertLevel.WARNING)])[0]
    assert retry.reason == "delivery-retry"
    assert retry.delivered_destinations == ()


def test_repeated_level_visit_rejects_old_delivery_progress(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    first = store.sync([_alert(AlertLevel.CRITICAL)])[0]
    store.record_result(first, True)

    now[0] += 1
    store.sync([_alert(AlertLevel.WARNING)])
    now[0] += 1
    current = store.sync([_alert(AlertLevel.CRITICAL)])[0]

    # The old critical event is from an earlier generation.
    store.record_result(
        first, False, delivered=frozenset({"telegram"})
    )
    assert store.states["disk:/"].delivered_destinations == []

    retry = store.sync([_alert(AlertLevel.CRITICAL)])[0]
    assert retry.generation == current.generation
    assert retry.delivered_destinations == ()


def test_snapshot_with_old_delivery_progress_is_stale(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    first = store.sync([_alert()])[0]
    stale = store.sync([_alert()])[0]
    assert stale.delivered_destinations == ()

    # The in-flight event records a partial success after the stale snapshot
    # was taken but before the stale snapshot is submitted.
    store.record_result(first, False, delivered=frozenset({"telegram"}))
    assert store.is_current(stale) is False

    # A stale result must not overwrite the newer progress.
    store.record_result(stale, True)
    assert store.states["disk:/"].delivered_destinations == ["telegram"]

    now[0] += 5
    fresh = store.sync([_alert()])[0]
    assert fresh.delivered_destinations == ("telegram",)


def test_snapshot_after_success_is_stale(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    first = store.sync([_alert()])[0]
    stale = store.sync([_alert()])[0]

    store.record_result(first, True)
    assert store.is_current(stale) is False

    store.record_result(stale, True)
    assert store.states["disk:/"].successful_notifications == 1
    assert store.sync([_alert()]) == []


def test_snapshot_after_retry_after_is_stale(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, now)
    first = store.sync([_alert()])[0]
    stale = store.sync([_alert()])[0]

    store.record_result(first, False, retry_after=17)
    assert store.is_current(stale) is False

    now[0] += 16
    assert store.sync([_alert()]) == []

    now[0] += 1
    retry = store.sync([_alert()])[0]
    assert retry.attempt == 1
