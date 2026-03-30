"""Trigger evaluation for Sentinel.

Compares a SystemSnapshot against configured thresholds and returns
a list of fired alerts.
"""

from dataclasses import dataclass
from enum import Enum

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
        ))

    # --- Memory ---
    if snapshot.memory.percent >= thresholds.memory_percent:
        alerts.append(Alert(
            metric="memory",
            level=AlertLevel.CRITICAL,
            message=f"Memory usage at {snapshot.memory.percent:.1f}% (threshold: {thresholds.memory_percent}%)",
            current_value=snapshot.memory.percent,
            threshold=thresholds.memory_percent,
        ))

    # --- Disk usage and growth ---
    for disk in snapshot.disks:
        if disk.percent >= thresholds.disk_percent:
            alerts.append(Alert(
                metric=f"disk:{disk.path}",
                level=AlertLevel.CRITICAL,
                message=(
                    f"Disk {disk.path} at {disk.percent:.1f}% "
                    f"(threshold: {thresholds.disk_percent}%)"
                ),
                current_value=disk.percent,
                threshold=thresholds.disk_percent,
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
            ))

    return alerts
