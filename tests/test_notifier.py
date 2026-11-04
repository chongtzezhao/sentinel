"""Tests for the background notification worker."""

import logging
import threading
import time

import pytest

from sentinel.actions import DeliveryResult
from sentinel.config import NotificationPolicyConfig
from sentinel.notifier import NotificationWorker
from sentinel.state import AlertStateStore
from sentinel.triggers import Alert, AlertLevel


@pytest.fixture
def logger():
    log = logging.getLogger("sentinel-test-notifier")
    log.addHandler(logging.NullHandler())
    return log


def _alert(metric: str = "disk:/") -> Alert:
    return Alert(
        metric=metric,
        level=AlertLevel.CRITICAL,
        message="disk low",
        current_value=0.5,
        threshold=1.0,
        unit="GiB available",
    )


def _store(tmp_path):
    return AlertStateStore(
        NotificationPolicyConfig(
            state_file=str(tmp_path / "state.json"),
            delivery_retry_seconds=[5, 10, 20],
        ),
        now_fn=lambda: 1000.0,
    )


def test_slow_dispatch_does_not_block_state_sync(tmp_path, logger):
    store = _store(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def dispatch(event):
        started.set()
        release.wait(2)
        return DeliveryResult(True)

    worker = NotificationWorker(dispatch, store.record_result, logger)
    event = store.sync([_alert()])[0]
    worker.start()
    try:
        assert worker.submit(event) is True
        assert started.wait(1)

        started_at = time.monotonic()
        store.sync([_alert()])
        assert time.monotonic() - started_at < 0.5
    finally:
        release.set()
        worker.stop()


def test_worker_deduplicates_an_inflight_event(tmp_path, logger):
    store = _store(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def dispatch(event):
        started.set()
        release.wait(2)
        return DeliveryResult(True)

    worker = NotificationWorker(dispatch, store.record_result, logger)
    event = store.sync([_alert()])[0]
    worker.start()
    try:
        assert worker.submit(event) is True
        assert started.wait(1)
        assert worker.submit(event) is False
    finally:
        release.set()
        worker.stop()


def test_worker_records_a_failed_dispatch(tmp_path, logger):
    store = _store(tmp_path)
    done = threading.Event()

    def record(event, success, retry_after=None, delivered=None):
        store.record_result(event, success, retry_after, delivered)
        done.set()

    worker = NotificationWorker(
        lambda event: DeliveryResult(False), record, logger
    )
    event = store.sync([_alert()])[0]
    worker.start()
    try:
        assert worker.submit(event) is True
        assert done.wait(1)
        assert store.states["disk:/"].consecutive_failures == 1
    finally:
        worker.stop()


def test_worker_survives_a_record_result_failure(tmp_path, logger):
    store = _store(tmp_path)
    first, second = store.sync([_alert(), _alert("cpu")])
    recorded = []
    done = threading.Event()

    def record(event, success, retry_after=None, delivered=None):
        recorded.append(event.metric)
        if len(recorded) == 1:
            raise OSError(28, "No space left on device")
        store.record_result(event, success, retry_after, delivered)
        done.set()

    worker = NotificationWorker(
        lambda event: DeliveryResult(True), record, logger
    )
    worker.start()
    try:
        assert worker.submit(first) is True
        assert worker.submit(second) is True
        assert done.wait(1)
        assert recorded == ["disk:/", "cpu"]
    finally:
        worker.stop()


def test_worker_skips_a_stale_event(tmp_path, logger):
    store = _store(tmp_path)
    stale_alert = store.sync([_alert()])[0]
    store.sync([])  # condition recovered before the worker starts
    sent = []
    checked = threading.Event()

    def is_current(event):
        result = store.is_current(event)
        checked.set()
        return result

    worker = NotificationWorker(
        lambda event: sent.append(event) or DeliveryResult(True),
        store.record_result,
        logger,
        is_current=is_current,
    )
    worker.start()
    try:
        assert worker.submit(stale_alert) is True
        assert checked.wait(1)
        assert sent == []
    finally:
        worker.stop()
