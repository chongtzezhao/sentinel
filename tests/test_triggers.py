"""Tests for triggers.evaluate."""

import pytest

from sentinel.config import TriggerConfig
from sentinel.monitor import (
    CpuMetrics,
    DiskMetrics,
    MemoryMetrics,
    MemoryPressureMetrics,
    SwapMetrics,
    SystemSnapshot,
)
from sentinel.triggers import (
    Alert,
    AlertLevel,
    TriggerEvaluator,
    evaluate,
    metric_values,
    snapshot_values,
)


def _snap(
    *,
    cpu=0.0,
    mem_available=8192.0,
    swap_percent=0.0,
    psi_some_avg60=0.0,
    psi_available=True,
    disks=None,
) -> SystemSnapshot:
    return SystemSnapshot(
        timestamp=0.0,
        cpu=CpuMetrics(percent=cpu),
        memory=MemoryMetrics(
            percent=0, total_mb=0, used_mb=0, available_mb=mem_available
        ),
        swap=SwapMetrics(percent=swap_percent),
        memory_pressure=MemoryPressureMetrics(
            some_avg60=psi_some_avg60,
            available=psi_available,
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
        memory_pressure_warning=10.0,
        memory_pressure_critical=25.0,
        breach_samples=3,
        clear_samples=2,
        hysteresis_percent=5.0,
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


def test_memory_pressure_warning_and_critical(thresholds):
    warning = evaluate(_snap(psi_some_avg60=15.0), thresholds)
    assert len(warning) == 1
    assert warning[0].metric == "memory_pressure"
    assert warning[0].level == AlertLevel.WARNING
    assert warning[0].current_value == 15.0

    critical = evaluate(_snap(psi_some_avg60=30.0), thresholds)
    assert len(critical) == 1
    assert critical[0].level == AlertLevel.CRITICAL


def test_memory_pressure_unavailable_is_silent(thresholds):
    snap = _snap(psi_some_avg60=99.0, psi_available=False)
    assert evaluate(snap, thresholds) == []


def test_swap_is_diagnostic_not_a_trigger(thresholds):
    snap = _snap(swap_percent=99.0)
    assert evaluate(snap, thresholds) == []
    assert "swap" not in metric_values(snap)
    assert snapshot_values(snap)["swap"] == 99.0


def _cpu_alert() -> Alert:
    return Alert(
        metric="cpu",
        level=AlertLevel.CRITICAL,
        message="CPU usage at 95.0% (threshold: 90.0%)",
        current_value=95.0,
        threshold=90.0,
        unit="% usage",
    )


def test_evaluator_requires_three_consecutive_breaches(thresholds):
    evaluator = TriggerEvaluator(thresholds)

    assert evaluator.evaluate(_snap(cpu=95)) == []
    assert evaluator.evaluate(_snap(cpu=95)) == []
    alerts = evaluator.evaluate(_snap(cpu=95))
    assert [alert.metric for alert in alerts] == ["cpu"]


def test_evaluator_resets_a_pending_breach(thresholds):
    evaluator = TriggerEvaluator(thresholds)

    evaluator.evaluate(_snap(cpu=95))
    evaluator.evaluate(_snap(cpu=95))
    assert evaluator.evaluate(_snap(cpu=10)) == []

    # The count restarts, so two more samples are not enough.
    assert evaluator.evaluate(_snap(cpu=95)) == []
    assert evaluator.evaluate(_snap(cpu=95)) == []
    assert [a.metric for a in evaluator.evaluate(_snap(cpu=95))] == ["cpu"]


def test_evaluator_hysteresis_keeps_alert_until_lower_bound(thresholds):
    evaluator = TriggerEvaluator(thresholds)
    for _ in range(3):
        alerts = evaluator.evaluate(_snap(cpu=95))
    assert [a.metric for a in alerts] == ["cpu"]
    assert alerts[0].breaching is True

    # Below the 90% trigger but above the 85.5% clear bound: still active.
    alerts = evaluator.evaluate(_snap(cpu=88))
    assert [a.metric for a in alerts] == ["cpu"]
    assert alerts[0].current_value == 88.0
    assert alerts[0].breaching is False
    assert "88.0%" in alerts[0].message

    # First sample below the clear bound: still active.
    alerts = evaluator.evaluate(_snap(cpu=80))
    assert [a.metric for a in alerts] == ["cpu"]

    # Second consecutive sample below the clear bound: cleared.
    assert evaluator.evaluate(_snap(cpu=80)) == []


def test_evaluator_debounces_disk_growth(thresholds):
    evaluator = TriggerEvaluator(thresholds)
    disk = _disk(growth=75.0)

    assert evaluator.evaluate(_snap(disks=[disk])) == []
    assert evaluator.evaluate(_snap(disks=[disk])) == []
    alerts = evaluator.evaluate(_snap(disks=[disk]))
    assert [a.metric for a in alerts] == ["disk_growth:/"]


def test_evaluator_restore_keeps_persisted_alert_active(thresholds):
    evaluator = TriggerEvaluator(thresholds)
    evaluator.restore([_cpu_alert()])

    alerts = evaluator.evaluate(_snap(cpu=88))
    assert [a.metric for a in alerts] == ["cpu"]
