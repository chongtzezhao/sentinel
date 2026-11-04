"""Tests for Sentinel metric collection helpers."""

from types import SimpleNamespace

import sentinel.monitor as monitor_mod
from sentinel.monitor import SystemMonitor


def test_parse_memory_pressure_valid():
    text = (
        "some avg10=1.20 avg60=2.30 avg300=3.40 total=1000\n"
        "full avg10=0.10 avg60=0.20 avg300=0.30 total=100\n"
    )
    metrics = SystemMonitor._parse_memory_pressure(text)

    assert metrics.available is True
    assert metrics.some_avg10 == 1.20
    assert metrics.some_avg60 == 2.30
    assert metrics.some_avg300 == 3.40
    assert metrics.full_avg60 == 0.20


def test_parse_memory_pressure_missing_some_is_unavailable():
    metrics = SystemMonitor._parse_memory_pressure(
        "full avg10=0.10 avg60=0.20 avg300=0.30 total=100\n"
    )
    assert metrics.available is False


def test_parse_memory_pressure_missing_avg60_is_unavailable():
    metrics = SystemMonitor._parse_memory_pressure(
        "some avg10=1.20 avg300=3.40 total=1000\n"
    )
    assert metrics.available is False


def test_cpu_sampler_uses_non_blocking_interval(monkeypatch):
    calls = []

    def fake_cpu(interval=None):
        calls.append(interval)
        return 12.5

    monkeypatch.setattr(monitor_mod.psutil, "cpu_percent", fake_cpu)
    monkeypatch.setattr(
        monitor_mod.psutil, "process_iter", lambda attrs=None: iter(())
    )
    monitor = SystemMonitor(["/"])
    monitor._cpu_samples = 1

    sample = monitor._collect_cpu()

    assert sample.percent == 12.5
    assert calls[-1] is None
    assert all(interval is None for interval in calls)


def test_process_cpu_is_primed_at_startup(monkeypatch):
    calls = []

    def fake_process_iter(attrs=None):
        calls.append(attrs)
        return iter(())

    monkeypatch.setattr(monitor_mod.psutil, "process_iter", fake_process_iter)
    monkeypatch.setattr(
        monitor_mod.psutil, "cpu_percent", lambda interval=None: 0.0
    )

    monitor = SystemMonitor(["/"])
    assert calls  # startup prime
    monitor._collect_processes()
    assert len(calls) >= 2


def test_process_cpu_percent_is_used(monkeypatch):
    class _Proc:
        pid = 1
        info = {
            "pid": 1,
            "name": "busy",
            "cpu_percent": 42.0,
            "memory_percent": 1.0,
        }

        def io_counters(self):
            return SimpleNamespace(read_bytes=1, write_bytes=2)

    monkeypatch.setattr(
        monitor_mod.psutil, "process_iter", lambda attrs=None: iter([_Proc()])
    )
    monkeypatch.setattr(
        monitor_mod.psutil, "cpu_percent", lambda interval=None: 0.0
    )
    monitor = SystemMonitor(["/"], top_n=1)

    top_cpu, _, _ = monitor._collect_processes()

    assert top_cpu[0].cpu_percent == 42.0
