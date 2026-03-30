"""Sentinel daemon – main monitoring loop.

Runs continuously, collecting snapshots, evaluating triggers, and
executing actions when thresholds are exceeded.
"""

import signal
import sys
import time

from sentinel.actions import CooldownTracker, handle_alerts, setup_logger
from sentinel.config import SentinelConfig
from sentinel.monitor import SystemMonitor
from sentinel.triggers import evaluate


class SentinelDaemon:
    """Main daemon loop for Sentinel."""

    def __init__(self, config: SentinelConfig):
        self._config = config
        self._running = False
        self._monitor = SystemMonitor(
            disk_paths=config.disk_paths,
            top_n=config.top_process_count,
        )
        self._cooldown = CooldownTracker(config.cooldown)
        self._logger = setup_logger(config.log)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        self._logger.info("Received %s – shutting down gracefully", sig_name)
        self._running = False

    def run(self) -> None:
        """Start the monitoring loop. Blocks until interrupted."""
        self._running = True

        # Graceful shutdown on SIGINT / SIGTERM
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._logger.info(
            "Sentinel started (poll every %ds)", self._config.poll_interval_seconds
        )

        while self._running:
            try:
                snapshot = self._monitor.snapshot()
                alerts = evaluate(snapshot, self._config.triggers)

                if alerts:
                    handle_alerts(
                        alerts,
                        self._config.actions,
                        self._cooldown,
                        self._logger,
                    )
                else:
                    self._logger.debug("All metrics within thresholds")

            except Exception:
                self._logger.exception("Error during monitoring cycle")

            # Sleep in small increments so we can respond to signals promptly
            waited = 0.0
            while self._running and waited < self._config.poll_interval_seconds:
                time.sleep(min(1.0, self._config.poll_interval_seconds - waited))
                waited += 1.0

        self._logger.info("Sentinel stopped")
        sys.exit(0)
