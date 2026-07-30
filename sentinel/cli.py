"""Command-line interface for Sentinel.

Provides:
  sentinel status  – one-shot display of current system metrics
  sentinel run     – start the monitoring daemon
"""

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime

from sentinel import __version__
from sentinel.config import load_config
from sentinel.monitor import SystemMonitor, SystemSnapshot


def _format_status(snap: SystemSnapshot) -> str:
    """Render a human-readable status report."""
    ts = (
        datetime.fromtimestamp(snap.timestamp, UTC)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    lines = [
        f"=== Sentinel System Status ({ts}) ===",
        "",
        f"  CPU Usage:    {snap.cpu.percent:.1f}%",
        "",
        (
            f"  Memory:       {snap.memory.percent:.1f}%  "
            f"({snap.memory.used_mb:.0f} / {snap.memory.total_mb:.0f} MiB, "
            f"{snap.memory.available_mb:.0f} MiB available)"
        ),
    ]

    lines.append("")
    for d in snap.disks:
        lines.append(
            f"  Disk {d.path}:  {d.percent:.1f}%  "
            f"({d.used_gb:.1f} / {d.total_gb:.1f} GiB, "
            f"{d.free_gb:.2f} GiB available)  "
            f"growth: {d.growth_mb_per_sec:.2f} MB/s"
        )

    for label, procs in [
        ("Top CPU Processes", snap.top_cpu),
        ("Top Memory Processes", snap.top_memory),
        ("Top I/O Processes", snap.top_io),
    ]:
        lines.append("")
        lines.append(f"  {label}:")
        for p in procs:
            lines.append(
                f"    PID {p.pid:>7}  {p.name:<20}  "
                f"CPU {p.cpu_percent:5.1f}%  MEM {p.memory_percent:5.1f}%  "
                f"IO R/W {p.io_read_bytes}/{p.io_write_bytes}"
            )

    lines.append("")
    return "\n".join(lines)


def _snapshot_to_dict(snap: SystemSnapshot) -> dict:
    """Convert a snapshot to a JSON-serialisable dict."""
    return {
        "timestamp": snap.timestamp,
        "cpu": {"percent": snap.cpu.percent},
        "memory": {
            "percent": snap.memory.percent,
            "total_mb": round(snap.memory.total_mb, 1),
            "used_mb": round(snap.memory.used_mb, 1),
            "available_mb": round(snap.memory.available_mb, 1),
        },
        "disks": [
            {
                "path": d.path,
                "percent": d.percent,
                "total_gb": round(d.total_gb, 1),
                "used_gb": round(d.used_gb, 1),
                "free_gb": round(d.free_gb, 1),
                "growth_mb_per_sec": d.growth_mb_per_sec,
            }
            for d in snap.disks
        ],
        "top_cpu": [_proc_dict(p) for p in snap.top_cpu],
        "top_memory": [_proc_dict(p) for p in snap.top_memory],
        "top_io": [_proc_dict(p) for p in snap.top_io],
    }


def _proc_dict(p) -> dict:
    return {
        "pid": p.pid,
        "name": p.name,
        "cpu_percent": p.cpu_percent,
        "memory_percent": round(p.memory_percent, 2),
        "io_read_bytes": p.io_read_bytes,
        "io_write_bytes": p.io_write_bytes,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Sentinel – Lightweight System Monitoring Tool",
    )
    parser.add_argument(
        "--version", action="version", version=f"sentinel {__version__}"
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config file (default: sentinel_config.json or SENTINEL_CONFIG env var)",
    )

    sub = parser.add_subparsers(dest="command")

    # sentinel status
    status_p = sub.add_parser("status", help="Show current system metrics")
    status_p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output in JSON format",
    )

    # sentinel run
    sub.add_parser("run", help="Start the monitoring daemon")

    # sentinel test-notification
    sub.add_parser(
        "test-notification",
        help="Send a Telegram test and verify Telegram's response",
    )

    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "status":
        monitor = SystemMonitor(
            disk_paths=config.disk_paths,
            top_n=config.top_process_count,
        )
        snap = monitor.snapshot()
        if args.as_json:
            print(json.dumps(_snapshot_to_dict(snap), indent=2))
        else:
            print(_format_status(snap))

    elif args.command == "run":
        from sentinel.daemon import SentinelDaemon
        daemon = SentinelDaemon(config)
        daemon.run()

    elif args.command == "test-notification":
        from hooks.telegram import send as telegram_send
        from sentinel.triggers import Alert, AlertLevel

        tg = config.notifications.telegram
        if not tg.enabled or not tg.bot_token or not tg.chat_id:
            print(
                "Telegram is disabled or missing its bot token/chat ID.",
                file=sys.stderr,
            )
            sys.exit(2)

        logger = logging.getLogger("sentinel-test-notification")
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler())
        alert = Alert(
            metric="sentinel:test-notification",
            level=AlertLevel.WARNING,
            message="Manual Sentinel notification test",
            current_value=0.0,
            threshold=0.0,
        )
        now = time.time()
        result = telegram_send(
            alert,
            tg.bot_token,
            tg.chat_id,
            logger,
            first_seen=now,
        )
        if not result.success:
            print(f"Telegram test failed: {result.error}", file=sys.stderr)
            sys.exit(1)
        print(
            f"Telegram test delivered successfully "
            f"(status={result.status}, message_id={result.message_id})."
        )

    else:
        parser.print_help()
        sys.exit(1)
