"""Tests for Sentinel metric collection helpers."""

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
