"""Bounded concurrency for harness runs: a small worker pool with a hard cap.

Runs invoke a model and can take a while; the control plane must never let an
unbounded number of them pile up in memory waiting for a thread. A
:class:`ThreadPoolExecutor` already queues internally once its workers are
busy, but that queue has no ceiling of its own — so ``RunSlots`` tracks
in-flight-plus-queued work itself and rejects outright once it's full, rather
than growing an unbounded backlog.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from hiveloom.errors import HiveloomError

DEFAULT_MAX_CONCURRENT_RUNS = 1
DEFAULT_MAX_QUEUED_RUNS = 4


class RunQueueFullError(HiveloomError):
    """Raised when in-flight-plus-queued runs already meet the configured cap.

    The app maps this to HTTP 503 with a ``Retry-After`` header.
    """

    def __init__(self, retry_after_seconds: int = 2):
        super().__init__("too many runs in flight or queued; try again shortly")
        self.retry_after_seconds = retry_after_seconds


class RunSlots:
    """Runs one at a time by default (``max_concurrent_runs=1``), queueing up
    to ``max_queued_runs`` more before rejecting further submissions.
    """

    def __init__(
        self,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
        max_queued_runs: int = DEFAULT_MAX_QUEUED_RUNS,
    ):
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_runs, thread_name_prefix="hiveloom-run"
        )
        self._cap = max_concurrent_runs + max_queued_runs
        self._lock = threading.Lock()
        self._count = 0

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """Submit ``fn`` to run in the background, or raise :class:`RunQueueFullError`."""
        with self._lock:
            if self._count >= self._cap:
                raise RunQueueFullError()
            self._count += 1
        future = self._executor.submit(fn, *args, **kwargs)
        future.add_done_callback(self._release)
        return future

    def _release(self, _future: Future) -> None:
        with self._lock:
            self._count -= 1

    def shutdown(self) -> None:
        """Clean shutdown on app teardown. Lets in-flight runs finish."""
        self._executor.shutdown(wait=True, cancel_futures=False)
