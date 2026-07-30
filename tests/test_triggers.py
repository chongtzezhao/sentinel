"""Tests for triggers.evaluate."""

import pytest

from sentinel.config import TriggerConfig
from sentinel.monitor import CpuMetrics, DiskMetrics, MemoryMetrics, SystemSnapshot
from sentinel.triggers import AlertLevel, evaluate


def _snap(*, cpu=0.0, mem_available=8192.0, disks=None) -> SystemSnapshot:
    return SystemSnapshot(
        timestamp=0.0,
        cpu=CpuMetrics(percent=cpu),
        memory=MemoryMetrics(
            percent=0, total_mb=0, used_mb=0, available_mb=mem_available
        ),
        disks=disks or [],
    )


def _disk(path="/", free=100.0, growth=0.0) -> DiskMetrics:
    return DiskMetrics(
        path=path, percent=0, total_gb=0, used_gb=0, free_gb=free,
        growth_mb_per_sec=growth,
    )


@pytest.fixture
def thresholds():
    return TriggerConfig(
        cpu_percent=90.0,
        memory_warning_available_mb=1024.0,
        memory_critical_available_mb=50.0,
        disk_warning_free_gb=10.0,
        disk_critical_free_gb=1.0,
        disk_growth_mb_per_sec=50.0,
    )


def test_no_alerts_when_below_all_thresholds(thresholds):
    snap = _snap(cpu=10, mem_available=4096, disks=[_disk(free=50, growth=1.0)])
    assert evaluate(snap, thresholds) == []


def test_cpu_alert_at_threshold(thresholds):
    snap = _snap(cpu=90.0)
    alerts = evaluate(snap, thresholds)
    assert len(alerts) == 1
    assert alerts[0].metric == "cpu"
    assert alerts[0].level == AlertLevel.CRITICAL
    assert alerts[0].current_value == 90.0


def test_memory_warning_uses_available_mb(thresholds):
    snap = _snap(mem_available=1000.0)
    alerts = evaluate(snap, thresholds)
    assert len(alerts) == 1
    assert alerts[0].metric == "memory"
    assert alerts[0].level == AlertLevel.WARNING
    assert alerts[0].current_value == 1000.0


def test_memory_critical_takes_precedence_over_warning(thresholds):
    alerts = evaluate(_snap(mem_available=50.0), thresholds)
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.CRITICAL


def test_disk_warning_includes_path(thresholds):
    snap = _snap(disks=[_disk(path="/var", free=9.5)])
    alerts = evaluate(snap, thresholds)
    assert len(alerts) == 1
    assert alerts[0].metric == "disk:/var"
    assert alerts[0].level == AlertLevel.WARNING


def test_disk_critical_takes_precedence_over_warning(thresholds):
    alerts = evaluate(_snap(disks=[_disk(free=1.0)]), thresholds)
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.CRITICAL


def test_disk_growth_alert_is_warning(thresholds):
    snap = _snap(disks=[_disk(path="/", free=50.0, growth=75.0)])
    alerts = evaluate(snap, thresholds)
    assert len(alerts) == 1
    assert alerts[0].metric == "disk_growth:/"
    assert alerts[0].level == AlertLevel.WARNING


def test_disk_can_emit_usage_and_growth_simultaneously(thresholds):
    snap = _snap(disks=[_disk(path="/", free=0.5, growth=75.0)])
    metrics = {a.metric for a in evaluate(snap, thresholds)}
    assert metrics == {"disk:/", "disk_growth:/"}


def test_multiple_metrics_alert_together(thresholds):
    snap = _snap(cpu=95, mem_available=40, disks=[_disk(free=0.5)])
    metrics = {a.metric for a in evaluate(snap, thresholds)}
    assert metrics == {"cpu", "memory", "disk:/"}


def test_disk_growth_just_below_threshold_silent(thresholds):
    snap = _snap(disks=[_disk(growth=49.999)])
    assert evaluate(snap, thresholds) == []
