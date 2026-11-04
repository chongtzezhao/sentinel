"""Persistent alert lifecycle and notification scheduling."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sentinel.config import NotificationPolicyConfig
from sentinel.triggers import Alert, AlertLevel


@dataclass
class AlertState:
    metric: str
    level: str
    message: str
    current_value: float
    threshold: float
    unit: str
    active: bool
    first_seen: float
    last_seen: float
    recovered_at: float | None = None
    last_attempt: float | None = None
    last_success: float | None = None
    successful_notifications: int = 0
    consecutive_failures: int = 0
    force_due: bool = False
    breaching: bool = True
    next_attempt: float | None = None
    level_delivered: bool = True
    delivered_destinations: list[str] = field(default_factory=list)
    generation: int = 0
    attempts: int = 0

    @classmethod
    def from_alert(cls, alert: Alert, now: float) -> AlertState:
        return cls(
            metric=alert.metric,
            level=alert.level.value,
            message=alert.message,
            current_value=alert.current_value,
            threshold=alert.threshold,
            unit=alert.unit,
            active=True,
            first_seen=now,
            last_seen=now,
            breaching=alert.breaching,
            level_delivered=False,
            generation=1,
        )

    def as_alert(self) -> Alert:
        return Alert(
            metric=self.metric,
            level=AlertLevel(self.level),
            message=self.message,
            current_value=self.current_value,
            threshold=self.threshold,
            unit=self.unit,
            breaching=self.breaching,
        )


@dataclass(frozen=True)
class NotificationEvent:
    metric: str
    kind: str  # "alert" or "recovery"
    alert: Alert
    first_seen: float
    event_time: float
    reason: str
    undelivered_alert: bool = False
    delivered_destinations: tuple[str, ...] = ()
    generation: int = 0
    attempt: int = 0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.event_time - self.first_seen)


class AlertStateStore:
    """Track active alerts and schedule notifications across restarts."""

    def __init__(
        self,
        config: NotificationPolicyConfig,
        *,
        now_fn=time.time,
    ):
        self._config = config
        self._path = Path(config.state_file)
        self._now = now_fn
        self._states: dict[str, AlertState] = {}
        self._lock = threading.RLock()
        self._load()

    @property
    def states(self) -> dict[str, AlertState]:
        with self._lock:
            return dict(self._states)

    def is_current(self, event: NotificationEvent) -> bool:
        """Return True if *event* still matches the live state."""
        with self._lock:
            state = self._states.get(event.metric)
            if state is None:
                return False
            if event.generation != state.generation:
                return False
            if event.attempt != state.attempts:
                return False
            if event.first_seen != state.first_seen:
                return False
            if event.alert.level.value != state.level:
                return False
            if tuple(state.delivered_destinations) != event.delivered_destinations:
                return False
            if event.kind == "alert":
                return state.active
            if event.kind == "recovery":
                return not state.active
            return False

    def sync(
        self,
        alerts: list[Alert],
        current_values: dict[str, float] | None = None,
    ) -> list[NotificationEvent]:
        """Update active conditions and return notifications currently due.

        ``current_values`` is optional; when supplied it updates the value
        reported in recovery notifications so they show the level that
        actually cleared the condition rather than the last breaching value.
        """
        with self._lock:
            return self._sync_locked(alerts, current_values)

    def _sync_locked(
        self,
        alerts: list[Alert],
        current_values: dict[str, float] | None = None,
    ) -> list[NotificationEvent]:
        now = self._now()
        current = {alert.metric: alert for alert in alerts}
        changed = False

        for metric, alert in current.items():
            state = self._states.get(metric)
            if state is None:
                self._states[metric] = AlertState.from_alert(alert, now)
                changed = True
                continue

            was_active = state.active
            previous_level = state.level
            state.active = True
            state.recovered_at = None
            state.last_seen = now
            state.level = alert.level.value
            state.message = alert.message
            state.current_value = alert.current_value
            state.threshold = alert.threshold
            state.unit = alert.unit
            state.breaching = alert.breaching

            if previous_level != alert.level.value:
                state.level_delivered = False
                state.delivered_destinations = []
                state.generation += 1
                changed = True

            if not was_active:
                state.first_seen = now
                state.last_attempt = None
                state.last_success = None
                state.successful_notifications = 0
                state.consecutive_failures = 0
                state.force_due = True
                state.level_delivered = False
                state.delivered_destinations = []
                state.generation += 1
                changed = True
            elif (
                previous_level == AlertLevel.WARNING.value
                and alert.level == AlertLevel.CRITICAL
            ):
                state.force_due = True
                changed = True

        for metric, state in list(self._states.items()):
            if metric in current or not state.active:
                continue
            state.active = False
            state.recovered_at = now
            state.last_attempt = None
            state.consecutive_failures = 0
            state.force_due = self._config.send_recovery
            if current_values is not None and metric in current_values:
                state.current_value = current_values[metric]
            if not self._config.send_recovery:
                del self._states[metric]
            changed = True

        events: list[NotificationEvent] = []
        for state in self._states.values():
            if self._is_due(state, now):
                kind = "alert" if state.active else "recovery"
                if (
                    state.active
                    and state.successful_notifications == 0
                    and state.last_attempt is None
                ):
                    reason = "initial"
                elif state.force_due:
                    reason = "state-change"
                elif state.consecutive_failures:
                    reason = "delivery-retry"
                else:
                    reason = "reminder"
                undelivered_alert = not state.active and not state.level_delivered
                if reason != "delivery-retry" and state.delivered_destinations:
                    state.delivered_destinations = []
                    changed = True
                events.append(
                    NotificationEvent(
                        metric=state.metric,
                        kind=kind,
                        alert=state.as_alert(),
                        first_seen=state.first_seen,
                        event_time=now,
                        reason=reason,
                        undelivered_alert=undelivered_alert,
                        delivered_destinations=tuple(
                            state.delivered_destinations
                        ),
                        generation=state.generation,
                        attempt=state.attempts,
                    )
                )

        if changed:
            self._save()
        return events

    def record_result(
        self,
        event: NotificationEvent,
        success: bool,
        retry_after: int | None = None,
        delivered: frozenset[str] | None = None,
    ) -> None:
        """Record a delivery result; failed attempts never count as delivery."""
        with self._lock:
            self._record_result_locked(event, success, retry_after, delivered)

    def _record_result_locked(
        self,
        event: NotificationEvent,
        success: bool,
        retry_after: int | None = None,
        delivered: frozenset[str] | None = None,
    ) -> None:
        state = self._states.get(event.metric)
        if state is None:
            return
        # Delivery progress belongs to the event lifecycle: an alert result
        # must not be applied after recovery, and vice versa.
        if event.kind == "alert" and not state.active:
            return
        if event.kind == "recovery" and state.active:
            return
        # Ignore results for an event that a newer state has superseded.
        if event.generation != state.generation:
            return
        if event.attempt != state.attempts:
            return
        if event.first_seen != state.first_seen:
            return
        if event.alert.level.value != state.level:
            return
        if tuple(state.delivered_destinations) != event.delivered_destinations:
            return

        now = self._now()
        state.last_attempt = now
        state.force_due = False
        if success:
            state.last_success = now
            state.successful_notifications += 1
            state.consecutive_failures = 0
            state.next_attempt = None
            state.delivered_destinations = []
            if event.kind == "alert":
                state.level_delivered = True
            if event.kind == "recovery":
                del self._states[event.metric]
        else:
            state.consecutive_failures += 1
            state.delivered_destinations = sorted(delivered or ())
            if retry_after is not None:
                state.next_attempt = now + max(0.0, float(retry_after))
            else:
                state.next_attempt = None
        state.attempts += 1
        self._save()

    def reset_for_test(self, metric: str) -> None:
        """Remove a synthetic metric so each manual test runs immediately."""
        with self._lock:
            if self._states.pop(metric, None) is not None:
                self._save()

    def _is_due(self, state: AlertState, now: float) -> bool:
        if state.next_attempt is not None and now < state.next_attempt:
            return False

        if state.force_due:
            return True

        if state.consecutive_failures > 0 and state.last_attempt is not None:
            if state.next_attempt is not None:
                # A 429 retry_after is authoritative while it is set.
                return True
            delays = self._config.delivery_retry_seconds or [1800]
            index = min(state.consecutive_failures - 1, len(delays) - 1)
            return now - state.last_attempt >= delays[index]

        if state.last_success is None:
            return state.last_attempt is None

        if not state.active:
            return True

        if (
            state.level == AlertLevel.CRITICAL.value
            and state.successful_notifications < self._config.rapid_repeat_count
        ):
            interval = self._config.critical_repeat_seconds
        else:
            interval = self._config.persistent_repeat_seconds
        return now - state.last_success >= interval

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            # A malformed state file must not prevent monitoring from starting.
            return

        for item in raw.get("states", []):
            try:
                state = AlertState(**item)
                # Validate the persisted level before it can reach as_alert().
                AlertLevel(state.level)
                self._states[state.metric] = state
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "states": [asdict(state) for state in self._states.values()],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            dir=self._path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
