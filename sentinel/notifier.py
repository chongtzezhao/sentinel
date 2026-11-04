"""Background delivery for Sentinel notifications.

The monitoring loop must never block on Telegram or webhook I/O. One worker
thread drains a bounded FIFO queue. If the queue is full, the event is left
due in AlertStateStore and retried on a later cycle. On shutdown the worker
stops after its current send; queued events stay due and retry after restart.
"""

from __future__ import annotations

import queue
import threading

from sentinel.state import NotificationEvent


class NotificationWorker:
    """Send due notifications off the sampling thread."""

    def __init__(
        self,
        dispatch,
        record_result,
        logger,
        maxsize: int = 64,
        is_current=None,
    ):
        self._dispatch = dispatch
        self._record_result = record_result
        self._logger = logger
        self._is_current = is_current or (lambda event: True)
        self._queue: queue.Queue[NotificationEvent] = queue.Queue(maxsize=maxsize)
        self._inflight: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="sentinel-notifier",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, event: NotificationEvent) -> bool:
        """Queue an event. False means it is still due and will be retried."""
        key = (event.metric, event.kind)
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._inflight.discard(key)
            self._logger.warning(
                "Notification queue full; leaving %s %s due for retry",
                event.kind,
                event.metric,
            )
            return False
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """Stop after the in-flight send; unprocessed events remain due."""
        self._stop.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._logger.warning(
                "Notification worker still busy after %.1fs; pending events "
                "will retry after restart",
                timeout,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            key = (event.metric, event.kind)
            try:
                if not self._is_current(event):
                    self._logger.info(
                        "Skipping stale %s notification for %s",
                        event.kind,
                        event.metric,
                    )
                else:
                    result = self._dispatch(event)
                    self._safe_record(
                        event,
                        result.success,
                        retry_after=result.retry_after,
                        delivered=result.delivered,
                    )
            except Exception:
                self._logger.exception(
                    "Error dispatching %s notification for %s",
                    event.kind,
                    event.metric,
                )
                self._safe_record(
                    event,
                    False,
                    delivered=frozenset(event.delivered_destinations),
                )
            finally:
                with self._lock:
                    self._inflight.discard(key)

    def _safe_record(self, event, success, **kwargs) -> None:
        """Keep a state-file failure from killing the worker thread."""
        try:
            self._record_result(event, success, **kwargs)
        except Exception:
            self._logger.exception(
                "Error recording delivery result for %s %s",
                event.kind,
                event.metric,
            )
