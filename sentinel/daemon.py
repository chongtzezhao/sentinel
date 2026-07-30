"""Sentinel daemon – main monitoring loop.

Runs continuously, collecting snapshots, evaluating triggers, and
executing actions when thresholds are exceeded.
"""

import os
import signal
import sys
import time

from sentinel.actions import dispatch_notification, setup_logger
from sentinel.config import SentinelConfig
from sentinel.monitor import SystemMonitor
from sentinel.state import AlertStateStore, NotificationEvent
from sentinel.triggers import Alert, AlertLevel, evaluate

TEST_ALERT_METRIC = "sentinel:test-alert"


class SentinelDaemon:
    """Main daemon loop for Sentinel."""

    def __init__(self, config: SentinelConfig):
        self._config = config
        self._running = False
        self._test_alert_pending = False
        self._monitor = SystemMonitor(
            disk_paths=config.disk_paths,
            top_n=config.top_process_count,
        )
        self._logger = setup_logger(config.log)
        self._alert_states = AlertStateStore(config.notification_policy)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        self._logger.info("Received %s – shutting down gracefully", sig_name)
        self._running = False

    def _handle_test_alert_signal(self, _signum: int, _frame: object) -> None:
        # Signal handlers must be minimal — defer real work to the main loop.
        self._test_alert_pending = True

    def _log_env_sanity_report(self) -> None:
        """Log a redacted view of the env/config the running service sees."""
        tg = self._config.notifications.telegram
        webhook_count = len(self._config.actions.webhook_urls)
        kill_count = len(self._config.actions.kill_processes)
        restart_count = len(self._config.actions.restart_processes)

        self._logger.info(
            "Test-alert env sanity: pid=%d uid=%d cwd=%s log_file=%s",
            os.getpid(), os.geteuid(), os.getcwd(), self._config.log.log_file,
        )
        self._logger.info(
            "Test-alert config: telegram.enabled=%s bot_token=%s chat_id=%s "
            "webhook_urls=%d kill_processes=%d restart_processes=%d",
            tg.enabled,
            "set" if tg.bot_token else "MISSING",
            "set" if tg.chat_id else "MISSING",
            webhook_count, kill_count, restart_count,
        )

    def _dispatch_test_alert(self) -> None:
        """Fire a synthetic alert through the full action pipeline.

        Triggered by SIGUSR1 so an operator can sanity-check that the
        running systemd service has the env vars / config it needs to
        actually deliver alerts (Telegram secrets, webhook URLs, etc.).
        """
        self._logger.info("Received SIGUSR1 – dispatching manual test alert")
        self._log_env_sanity_report()

        alert = Alert(
            metric=TEST_ALERT_METRIC,
            level=AlertLevel.WARNING,
            message="Manual test alert (SIGUSR1) – verifying systemd service context",
            current_value=0.0,
            threshold=0.0,
        )
        now = time.time()
        event = NotificationEvent(
            metric=TEST_ALERT_METRIC,
            kind="alert",
            alert=alert,
            first_seen=now,
            event_time=now,
            reason="manual-test",
        )
        success = dispatch_notification(
            event,
            self._config.actions,
            self._config.notifications,
            self._logger,
        )
        if success:
            self._logger.info("Manual test notification completed successfully")
        else:
            self._logger.error("Manual test notification failed")

    def run(self) -> None:
        """Start the monitoring loop. Blocks until interrupted."""
        self._running = True

        # Graceful shutdown on SIGINT / SIGTERM
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        # SIGUSR1 fires a manual test alert through the live service context
        signal.signal(signal.SIGUSR1, self._handle_test_alert_signal)

        self._logger.info(
            "Sentinel started: pid=%d poll=%ds telegram=%s destination=%s "
            "state_file=%s",
            os.getpid(),
            self._config.poll_interval_seconds,
            (
                "enabled"
                if self._config.notifications.telegram.enabled
                else "disabled"
            ),
            (
                "configured"
                if self._config.notifications.telegram.chat_id
                else "missing"
            ),
            self._config.notification_policy.state_file,
        )

        while self._running:
            if self._test_alert_pending:
                self._test_alert_pending = False
                try:
                    self._dispatch_test_alert()
                except Exception:
                    self._logger.exception("Error dispatching test alert")

            try:
                snapshot = self._monitor.snapshot()
                alerts = evaluate(snapshot, self._config.triggers)
                events = self._alert_states.sync(alerts)
                for event in events:
                    try:
                        success = dispatch_notification(
                            event,
                            self._config.actions,
                            self._config.notifications,
                            self._logger,
                        )
                    except Exception:
                        success = False
                        self._logger.exception(
                            "Error dispatching %s notification for %s",
                            event.kind,
                            event.metric,
                        )
                    self._alert_states.record_result(event, success)

            except Exception:
                self._logger.exception("Error during monitoring cycle")

            # Sleep in small increments so we can respond to signals promptly
            waited = 0.0
            while self._running and waited < self._config.poll_interval_seconds:
                if self._test_alert_pending:
                    break  # handle the test alert on the next outer iteration
                time.sleep(min(1.0, self._config.poll_interval_seconds - waited))
                waited += 1.0

        self._logger.info("Sentinel stopped")
        sys.exit(0)
