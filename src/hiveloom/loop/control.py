"""Run control — a thread-safe channel from the outside into a running loop.

A served harness runs each loop on a worker thread; the operator meanwhile may
want to stop the run, say something the agent should take into account
("actually, exclude segment X"), or move it onto a different model ("this one
is going in circles — finish it on Opus"). All of these arrive here and are
consumed by the loop at the next turn boundary — never mid-model-call and
never mid-tool — so the run always stops or adjusts at a point where its state
is coherent.

Stopping is cooperative and graceful: the run finishes with status
``"stopped"``, its trace intact, rather than being killed.
"""

from __future__ import annotations

import threading
from typing import Any


class RunControl:
    """Stop flag + a small steering-message inbox, safe across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_reason = ""
        self._messages: list[str] = []
        self._model_switches: list[dict[str, str | None]] = []

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

    def switch_model(
        self, model: str | None = None, *, provider: str | None = None, reason: str = ""
    ) -> None:
        """Queue a model change for the loop's next turn boundary.

        Either field may be omitted: ``model`` alone moves to another model on
        the same provider, ``provider`` alone re-serves the same model id
        elsewhere. The run keeps its conversation; blocks only the previous
        model can validate are stripped at the boundary.

        The switch is recorded in the run's model path, and a run that changed
        models is reported as such rather than counted as a clean sample of the
        harness at its declared model.
        """
        if model is None and provider is None:
            return
        with self._lock:
            self._model_switches.append(
                {"model": model, "provider": provider, "reason": reason}
            )

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

    def drain_model_switches(self) -> list[dict[str, Any]]:
        with self._lock:
            drained, self._model_switches = self._model_switches, []
            return drained
