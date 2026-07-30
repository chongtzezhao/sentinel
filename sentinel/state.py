"""Persistent alert lifecycle and notification scheduling."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
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
        )

    def as_alert(self) -> Alert:
        return Alert(
            metric=self.metric,
            level=AlertLevel(self.level),
            message=self.message,
            current_value=self.current_value,
            threshold=self.threshold,
            unit=self.unit,
        )


@dataclass(frozen=True)
class NotificationEvent:
    metric: str
    kind: str  # "alert" or "recovery"
    alert: Alert
    first_seen: float
    event_time: float
    reason: str

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
        self._load()

    @property
    def states(self) -> dict[str, AlertState]:
        return dict(self._states)

    def sync(self, alerts: list[Alert]) -> list[NotificationEvent]:
        """Update active conditions and return notifications currently due."""
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

            if not was_active:
                state.first_seen = now
                state.last_attempt = None
                state.last_success = None
                state.successful_notifications = 0
                state.consecutive_failures = 0
                state.force_due = True
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
                events.append(
                    NotificationEvent(
                        metric=state.metric,
                        kind=kind,
                        alert=state.as_alert(),
                        first_seen=state.first_seen,
                        event_time=now,
                        reason=reason,
                    )
                )

        if changed:
            self._save()
        return events

    def record_result(self, event: NotificationEvent, success: bool) -> None:
        """Record a delivery result; failed attempts never count as delivery."""
        state = self._states.get(event.metric)
        if state is None:
            return

        now = self._now()
        state.last_attempt = now
        state.force_due = False
        if success:
            state.last_success = now
            state.successful_notifications += 1
            state.consecutive_failures = 0
            if event.kind == "recovery":
                del self._states[event.metric]
        else:
            state.consecutive_failures += 1
        self._save()

    def reset_for_test(self, metric: str) -> None:
        """Remove a synthetic metric so each manual test runs immediately."""
        if self._states.pop(metric, None) is not None:
            self._save()

    def _is_due(self, state: AlertState, now: float) -> bool:
        if state.force_due:
            return True

        if state.consecutive_failures > 0 and state.last_attempt is not None:
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
