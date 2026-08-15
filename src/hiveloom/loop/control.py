"""Run control — a thread-safe channel from the outside into a running loop.

A served harness runs each loop on a worker thread; the operator meanwhile may
want to stop the run, or say something the agent should take into account
("actually, exclude segment X"). Both arrive here and are consumed by the loop
at the next turn boundary — never mid-model-call and never mid-tool — so the
run always stops or adjusts at a point where its state is coherent.

Stopping is cooperative and graceful: the run finishes with status
``"stopped"``, its trace intact, rather than being killed.
"""

from __future__ import annotations

import threading


class RunControl:
    """Stop flag + a small steering-message inbox, safe across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_reason = ""
        self._messages: list[str] = []

    # -- producer side (HTTP handler, embedding caller) -------------------- #

    def request_stop(self, reason: str = "") -> None:
        with self._lock:
            self._stop_reason = reason or "stopped by operator"
        self._stop.set()

    def send_message(self, content: str) -> None:
        """Queue a steering message; the loop injects it before its next model call."""
        if not content:
            return
        with self._lock:
            self._messages.append(content)

    # -- consumer side (the loop, at turn boundaries) ---------------------- #

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def stop_reason(self) -> str:
        with self._lock:
            return self._stop_reason

    def drain_messages(self) -> list[str]:
        with self._lock:
            drained, self._messages = self._messages, []
            return drained
