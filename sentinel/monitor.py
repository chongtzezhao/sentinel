"""System metrics collection for Sentinel.

Gathers CPU, memory, disk usage, disk growth rate, and top processes
using the psutil library.
"""

import time
from dataclasses import dataclass, field

import psutil


@dataclass
class CpuMetrics:
    percent: float  # Overall CPU usage percentage


@dataclass
class MemoryMetrics:
    percent: float       # RAM usage percentage
    total_mb: float
    used_mb: float
    available_mb: float


@dataclass
class DiskMetrics:
    path: str
    percent: float       # Disk usage percentage
    total_gb: float
    used_gb: float
    free_gb: float
    growth_mb_per_sec: float  # Estimated write rate


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    io_read_bytes: int
    io_write_bytes: int


@dataclass
class SystemSnapshot:
    """A point-in-time snapshot of all monitored metrics."""
    timestamp: float
    cpu: CpuMetrics
    memory: MemoryMetrics
    disks: list[DiskMetrics] = field(default_factory=list)
    top_cpu: list[ProcessInfo] = field(default_factory=list)
    top_memory: list[ProcessInfo] = field(default_factory=list)
    top_io: list[ProcessInfo] = field(default_factory=list)


class SystemMonitor:
    """Collects system metrics with disk-growth tracking between snapshots."""

    def __init__(self, disk_paths: list[str], top_n: int = 5):
        self._disk_paths = disk_paths
        self._top_n = top_n
        # Previous disk usage for growth-rate calculation: {path: (timestamp, used_bytes)}
        self._prev_disk: dict[str, tuple[float, int]] = {}

    # ---- CPU ----

    @staticmethod
    def _collect_cpu() -> CpuMetrics:
        return CpuMetrics(percent=psutil.cpu_percent(interval=1))

    # ---- Memory ----

    @staticmethod
    def _collect_memory() -> MemoryMetrics:
        vm = psutil.virtual_memory()
        return MemoryMetrics(
            percent=vm.percent,
            total_mb=vm.total / (1024 ** 2),
            used_mb=vm.used / (1024 ** 2),
            available_mb=vm.available / (1024 ** 2),
        )

    # ---- Disk ----

    def _collect_disks(self) -> list[DiskMetrics]:
        now = time.time()
        results: list[DiskMetrics] = []
        for path in self._disk_paths:
            try:
                usage = psutil.disk_usage(path)
            except OSError:
                continue

            # Calculate growth rate
            growth = 0.0
            prev = self._prev_disk.get(path)
            if prev is not None:
                elapsed = now - prev[0]
                if elapsed > 0:
                    delta_bytes = usage.used - prev[1]
                    growth = (delta_bytes / (1024 ** 2)) / elapsed  # MB/s

            self._prev_disk[path] = (now, usage.used)

            results.append(DiskMetrics(
                path=path,
                percent=usage.percent,
                total_gb=usage.total / (1024 ** 3),
                used_gb=usage.used / (1024 ** 3),
                free_gb=usage.free / (1024 ** 3),
                growth_mb_per_sec=round(growth, 3),
            ))
        return results

    # ---- Processes ----

    def _collect_processes(self) -> tuple[list[ProcessInfo], list[ProcessInfo], list[ProcessInfo]]:
        """Return (top_cpu, top_memory, top_io) process lists."""
        procs: list[ProcessInfo] = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = p.info
                try:
                    io = p.io_counters()
                    io_r, io_w = io.read_bytes, io.write_bytes
                except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                    io_r, io_w = 0, 0

                procs.append(ProcessInfo(
                    pid=info["pid"],
                    name=info["name"] or "",
                    cpu_percent=info["cpu_percent"] or 0.0,
                    memory_percent=info["memory_percent"] or 0.0,
                    io_read_bytes=io_r,
                    io_write_bytes=io_w,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        n = self._top_n
        top_cpu = sorted(procs, key=lambda p: p.cpu_percent, reverse=True)[:n]
        top_mem = sorted(procs, key=lambda p: p.memory_percent, reverse=True)[:n]
        top_io = sorted(procs, key=lambda p: p.io_read_bytes + p.io_write_bytes, reverse=True)[:n]
        return top_cpu, top_mem, top_io

    # ---- Full Snapshot ----

    def snapshot(self) -> SystemSnapshot:
        """Collect a full system snapshot."""
        cpu = self._collect_cpu()
        mem = self._collect_memory()
        disks = self._collect_disks()
        top_cpu, top_mem, top_io = self._collect_processes()

        return SystemSnapshot(
            timestamp=time.time(),
            cpu=cpu,
            memory=mem,
            disks=disks,
            top_cpu=top_cpu,
            top_memory=top_mem,
            top_io=top_io,
        )
