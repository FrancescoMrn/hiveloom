"""Run control — a thread-safe channel from the outside into a running loop.

A served harness runs each loop on a worker thread; the operator meanwhile may
want to stop the run, say something the agent should take into account
("actually, exclude segment X"), move it onto a different model ("this one is
going in circles — finish it on Opus"), or put it into a different playbook.
All of these arrive here and are consumed by the loop at the next turn
boundary — never mid-model-call and never mid-tool — so the run always stops or
adjusts at a point where its state is coherent.

Queued steering messages are **addressable**: each carries an id, and an
operator UI can list, edit, or withdraw one before the loop reaches its next
boundary. A queue you can only append to cannot be shown or corrected, and the
window between typing a message and the loop consuming it is exactly where a
person notices they got it wrong.

Stopping is cooperative and graceful: the run finishes with status
``"stopped"``, its trace intact, rather than being killed.
"""

from __future__ import annotations

import itertools
import threading
from datetime import UTC, datetime
from typing import Any


class RunControl:
    """Stop flag + a small steering-message inbox, safe across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_reason = ""
        self._messages: list[dict[str, str]] = []
        self._model_switches: list[dict[str, str | None]] = []
        self._playbook_switches: list[dict[str, str]] = []
        self._ids = itertools.count(1)

    # -- producer side (HTTP handler, embedding caller) -------------------- #

    def request_stop(self, reason: str = "") -> None:
        with self._lock:
            self._stop_reason = reason or "stopped by operator"
        self._stop.set()

    def send_message(self, content: str) -> str | None:
        """Queue a steering message; the loop injects it before its next model call.

        Returns the queued message's id, which :meth:`edit_message` and
        :meth:`remove_message` address until the loop drains it. ``None`` means
        nothing was queued (empty content).
        """
        if not content:
            return None
        with self._lock:
            message_id = f"msg_{next(self._ids)}"
            self._messages.append(
                {
                    "id": message_id,
                    "content": content,
                    "queued_at": datetime.now(UTC).isoformat(),
                }
            )
            return message_id

    def pending_messages(self) -> list[dict[str, str]]:
        """Steering messages queued but not yet consumed by the loop."""
        with self._lock:
            return [dict(message) for message in self._messages]

    def edit_message(self, message_id: str, content: str) -> bool:
        """Rewrite a queued message. False if it is gone (already delivered).

        Deliberately not a delete-then-append: that would move the message to
        the back of the queue, silently reordering what the agent is told.
        """
        if not content:
            return False
        with self._lock:
            for message in self._messages:
                if message["id"] == message_id:
                    message["content"] = content
                    return True
            return False

    def remove_message(self, message_id: str) -> bool:
        """Withdraw a queued message. False if the loop already took it."""
        with self._lock:
            for index, message in enumerate(self._messages):
                if message["id"] == message_id:
                    del self._messages[index]
                    return True
            return False

    def switch_playbook(self, name: str, *, reason: str = "") -> None:
        """Queue a playbook change for the loop's next turn boundary.

        The mode's own ``on_enter``/``on_exit`` gates still run and may refuse:
        an operator switch is a request through the same door the model uses,
        not a way around a gate that exists to stop exactly this.
        """
        if not name:
            return
        with self._lock:
            self._playbook_switches.append({"name": name, "reason": reason})

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
            return [message["content"] for message in drained]

    def drain_playbook_switches(self) -> list[dict[str, str]]:
        with self._lock:
            drained, self._playbook_switches = self._playbook_switches, []
            return drained

    def drain_model_switches(self) -> list[dict[str, Any]]:
        with self._lock:
            drained, self._model_switches = self._model_switches, []
            return drained
