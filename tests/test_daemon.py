"""Tests for daemon wiring that does not require a live service."""

import sentinel.daemon as daemon_mod
from sentinel.config import SentinelConfig


def test_manual_test_alert_is_queued_not_dispatched(monkeypatch, tmp_path):
    cfg = SentinelConfig()
    cfg.notification_policy.state_file = str(tmp_path / "state.json")
    cfg.log.log_to_file = False
    daemon = daemon_mod.SentinelDaemon(cfg)

    queued = []
    monkeypatch.setattr(
        daemon._notifier, "submit",
        lambda event: queued.append(event) or True,
    )

    def fail_dispatch(*args, **kwargs):
        raise AssertionError("manual test alert must not dispatch inline")

    monkeypatch.setattr(
        daemon_mod, "dispatch_notification", fail_dispatch
    )

    daemon._dispatch_test_alert()

    assert len(queued) == 1
    assert queued[0].metric == daemon_mod.TEST_ALERT_METRIC
    assert queued[0].reason == "manual-test"
