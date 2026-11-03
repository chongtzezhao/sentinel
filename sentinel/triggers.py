"""Trigger evaluation for Sentinel.

Compares a SystemSnapshot against configured thresholds and returns fired
alerts. ``TriggerEvaluator`` adds debouncing and value hysteresis so a single
10-second spike does not produce an alert/recovery pair.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sentinel.config import TriggerConfig
from sentinel.monitor import SystemSnapshot


class AlertLevel(Enum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Represents a single fired trigger."""
    metric: str          # e.g. "cpu", "memory", "disk:/", "disk_growth:/"
    level: AlertLevel
    message: str
    current_value: float
    threshold: float
    unit: str = ""
    # False while an active condition sits in the hysteresis band. The alert
    # is still active, but process actions should only run on a real breach.
    breaching: bool = True


def evaluate(snapshot: SystemSnapshot, thresholds: TriggerConfig) -> list[Alert]:
    """Evaluate a snapshot against thresholds, returning any fired alerts."""
    alerts: list[Alert] = []

    # --- CPU ---
    if snapshot.cpu.percent >= thresholds.cpu_percent:
        alerts.append(Alert(
            metric="cpu",
            level=AlertLevel.CRITICAL,
            message=f"CPU usage at {snapshot.cpu.percent:.1f}% (threshold: {thresholds.cpu_percent}%)",
            current_value=snapshot.cpu.percent,
            threshold=thresholds.cpu_percent,
            unit="% usage",
        ))

    # --- Memory: absolute available capacity ---
    if snapshot.memory.available_mb <= thresholds.memory_critical_available_mb:
        alerts.append(Alert(
            metric="memory",
            level=AlertLevel.CRITICAL,
            message=(
                f"Memory critically low: {snapshot.memory.available_mb:.0f} MiB "
                f"available (critical: ≤{thresholds.memory_critical_available_mb:.0f} MiB)"
            ),
            current_value=snapshot.memory.available_mb,
            threshold=thresholds.memory_critical_available_mb,
            unit="MiB available",
        ))
    elif snapshot.memory.available_mb <= thresholds.memory_warning_available_mb:
        alerts.append(Alert(
            metric="memory",
            level=AlertLevel.WARNING,
            message=(
                f"Memory low: {snapshot.memory.available_mb:.0f} MiB available "
                f"(warning: ≤{thresholds.memory_warning_available_mb:.0f} MiB)"
            ),
            current_value=snapshot.memory.available_mb,
            threshold=thresholds.memory_warning_available_mb,
            unit="MiB available",
        ))

    # --- Swap: percentage of swap in use ---
    if snapshot.swap.percent >= thresholds.swap_critical_percent:
        alerts.append(Alert(
            metric="swap",
            level=AlertLevel.CRITICAL,
            message=(
                f"Swap usage critical: {snapshot.swap.percent:.1f}% used "
                f"(critical: ≥{thresholds.swap_critical_percent:.0f}%)"
            ),
            current_value=snapshot.swap.percent,
            threshold=thresholds.swap_critical_percent,
            unit="% used",
        ))
    elif snapshot.swap.percent >= thresholds.swap_warning_percent:
        alerts.append(Alert(
            metric="swap",
            level=AlertLevel.WARNING,
            message=(
                f"Swap usage high: {snapshot.swap.percent:.1f}% used "
                f"(warning: ≥{thresholds.swap_warning_percent:.0f}%)"
            ),
            current_value=snapshot.swap.percent,
            threshold=thresholds.swap_warning_percent,
            unit="% used",
        ))

    # --- Disk free capacity and growth ---
    for disk in snapshot.disks:
        if disk.free_gb <= thresholds.disk_critical_free_gb:
            alerts.append(Alert(
                metric=f"disk:{disk.path}",
                level=AlertLevel.CRITICAL,
                message=(
                    f"Disk {disk.path} critically low: {disk.free_gb:.2f} GiB "
                    f"available (critical: ≤{thresholds.disk_critical_free_gb:.2f} GiB)"
                ),
                current_value=disk.free_gb,
                threshold=thresholds.disk_critical_free_gb,
                unit="GiB available",
            ))
        elif disk.free_gb <= thresholds.disk_warning_free_gb:
            alerts.append(Alert(
                metric=f"disk:{disk.path}",
                level=AlertLevel.WARNING,
                message=(
                    f"Disk {disk.path} low: {disk.free_gb:.2f} GiB available "
                    f"(warning: ≤{thresholds.disk_warning_free_gb:.2f} GiB)"
                ),
                current_value=disk.free_gb,
                threshold=thresholds.disk_warning_free_gb,
                unit="GiB available",
            ))

        if disk.growth_mb_per_sec >= thresholds.disk_growth_mb_per_sec:
            alerts.append(Alert(
                metric=f"disk_growth:{disk.path}",
                level=AlertLevel.WARNING,
                message=(
                    f"Disk {disk.path} growing at {disk.growth_mb_per_sec:.2f} MB/s "
                    f"(threshold: {thresholds.disk_growth_mb_per_sec} MB/s)"
                ),
                current_value=disk.growth_mb_per_sec,
                threshold=thresholds.disk_growth_mb_per_sec,
                unit="MiB/s growth",
            ))

    return alerts


def metric_values(snapshot: SystemSnapshot) -> dict[str, float]:
    """Return the current value for every metric that can trigger."""
    values = {
        "cpu": snapshot.cpu.percent,
        "memory": snapshot.memory.available_mb,
        "swap": snapshot.swap.percent,
    }
    for disk in snapshot.disks:
        values[f"disk:{disk.path}"] = disk.free_gb
        values[f"disk_growth:{disk.path}"] = disk.growth_mb_per_sec
    return values


def format_value(alert: Alert) -> str:
    """Format an alert's current value for logs and notifications."""
    if alert.unit in ("% usage", "% used"):
        return f"{alert.current_value:.1f}%"
    if alert.unit == "MiB available":
        return f"{alert.current_value:.0f} MiB available"
    if alert.unit == "GiB available":
        return f"{alert.current_value:.2f} GiB available"
    if alert.unit == "MiB/s growth":
        return f"{alert.current_value:.2f} MB/s growth"
    value = f"{alert.current_value:.2f}"
    return f"{value} {alert.unit}".strip()


def _is_low_is_bad(alert: Alert) -> bool:
    """True when a lower value is worse (available memory, free disk)."""
    return alert.metric == "memory" or (
        alert.metric.startswith("disk:")
        and not alert.metric.startswith("disk_growth:")
    )


def _clear_bound(alert: Alert, hysteresis_percent: float) -> float:
    """Return the value at which *alert* is considered cleared."""
    # Clamp so a bad config cannot create a bound that is impossible to clear.
    margin = max(0.0, min(99.0, float(hysteresis_percent))) / 100.0
    if _is_low_is_bad(alert):
        return alert.threshold * (1.0 + margin)
    return alert.threshold * (1.0 - margin)


def _is_cleared(alert: Alert, value: float, hysteresis_percent: float) -> bool:
    """Return True when *value* is past the recovery bound for *alert*.

    Low-is-bad metrics (memory available, disk free) recover above their
    threshold. High-is-bad metrics (CPU, swap, disk growth) recover below it.
    """
    bound = _clear_bound(alert, hysteresis_percent)
    if _is_low_is_bad(alert):
        return value >= bound
    return value <= bound


def _refresh_alert(
    alert: Alert, value: float, hysteresis_percent: float
) -> Alert:
    """Return an active alert updated with the current value.

    Used while a condition is in the hysteresis band so reminders and
    webhooks carry the real value rather than the last breaching value.
    """
    bound = _clear_bound(alert, hysteresis_percent)
    if alert.metric == "cpu":
        message = (
            f"CPU usage at {value:.1f}% "
            f"(still active; clears below {bound:.1f}%)"
        )
    elif alert.metric == "memory":
        message = (
            f"Memory available at {value:.0f} MiB "
            f"(still active; clears above {bound:.0f} MiB)"
        )
    elif alert.metric == "swap":
        message = (
            f"Swap usage at {value:.1f}% "
            f"(still active; clears below {bound:.1f}%)"
        )
    elif alert.metric.startswith("disk_growth:"):
        path = alert.metric.split(":", 1)[1]
        message = (
            f"Disk {path} growing at {value:.2f} MB/s "
            f"(still active; clears below {bound:.2f} MB/s)"
        )
    elif alert.metric.startswith("disk:"):
        path = alert.metric.split(":", 1)[1]
        message = (
            f"Disk {path} free space at {value:.2f} GiB "
            f"(still active; clears above {bound:.2f} GiB)"
        )
    else:
        message = f"{alert.metric} at {value:.2f} {alert.unit} (still active)"

    return Alert(
        metric=alert.metric,
        level=alert.level,
        message=message,
        current_value=value,
        threshold=alert.threshold,
        unit=alert.unit,
        breaching=False,
    )


@dataclass
class _MetricState:
    alert: Alert | None = None
    breach_count: int = 0
    clear_count: int = 0


class TriggerEvaluator:
    """Debounce raw triggers and apply value hysteresis.

    A new condition must breach its trigger for ``breach_samples`` consecutive
    snapshots before it is reported. An active condition stays active until
    its value is past the hysteresis bound for ``clear_samples`` consecutive
    snapshots. This suppresses single-poll spikes while keeping CPU alerts
    sensitive to genuinely sustained saturation.
    """

    def __init__(self, config: TriggerConfig):
        self._config = config
        self._states: dict[str, _MetricState] = {}

    def restore(self, alerts: Iterable[Alert]) -> None:
        """Seed active conditions, e.g. from persisted state after a restart."""
        for alert in alerts:
            self._states[alert.metric] = _MetricState(alert=alert)

    def evaluate(self, snapshot: SystemSnapshot) -> list[Alert]:
        raw = {alert.metric: alert for alert in evaluate(snapshot, self._config)}
        values = metric_values(snapshot)
        breach_samples = max(1, int(self._config.breach_samples))
        clear_samples = max(1, int(self._config.clear_samples))
        active: list[Alert] = []

        for metric, state in list(self._states.items()):
            current = values.get(metric)
            if current is None:
                # The metric disappeared, e.g. a configured disk was removed.
                del self._states[metric]
                continue

            if state.alert is None:
                # Still waiting for enough consecutive breaches.
                if metric in raw:
                    state.breach_count += 1
                    if state.breach_count >= breach_samples:
                        state.alert = raw[metric]
                        state.breach_count = 0
                        active.append(state.alert)
                else:
                    del self._states[metric]
                continue

            if metric in raw:
                state.alert = raw[metric]
                state.breach_count = 0
                state.clear_count = 0
                active.append(state.alert)
                continue

            if _is_cleared(
                state.alert, current, self._config.hysteresis_percent
            ):
                state.clear_count += 1
                if state.clear_count >= clear_samples:
                    del self._states[metric]
                    continue
            else:
                state.clear_count = 0
                state.alert = _refresh_alert(
                    state.alert,
                    current,
                    self._config.hysteresis_percent,
                )
            active.append(state.alert)

        for metric, alert in raw.items():
            if metric in self._states:
                continue
            state = _MetricState(breach_count=1)
            self._states[metric] = state
            if state.breach_count >= breach_samples:
                state.alert = alert
                state.breach_count = 0
                active.append(alert)

        return active
