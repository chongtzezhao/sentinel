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
    unit: str = ""


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
