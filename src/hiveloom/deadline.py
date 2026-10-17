"""One time budget for a run: what model calls, retries and backoff share.

``max_wall_clock_seconds`` is a guardrail, and guardrails are consulted before
a model call. A call that has already started is not theirs to bound, so one
provider timeout plus its retries and backoff could outlive the budget many
times over. The runner therefore publishes the run's deadline here, in a
:class:`~contextvars.ContextVar` (no signals, so it works off the main thread),
and providers draw each attempt's timeout and each backoff wait from it.

The same slot optionally carries the run's stop event, so a cooperative
``RunControl.request_stop`` ends a retry sequence at the next attempt boundary
instead of after the last attempt. An HTTP request already in flight is not
interrupted: a host that needs a hard bound must still supervise the process.

Provider authors use the module through :mod:`hiveloom.ext`:
``remaining_seconds`` for per-attempt timeouts, ``check_run_bounds`` before an
attempt, ``wait_backoff`` instead of ``time.sleep``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from hiveloom.errors import HiveloomError


class DeadlineExceeded(HiveloomError):
    """The run's time budget is spent. The loop ends the run as a guardrail halt."""


class RunStopped(HiveloomError):
    """A cooperative stop was requested. The loop ends the run as ``stopped``."""


@dataclass(frozen=True)
class RunBounds:
    """What the active run allows: a monotonic deadline and its stop events."""

    deadline: float | None = None
    # The run's own stop event plus every enclosing run's: a delegated child
    # stops when any run above it is asked to.
    stops: tuple[threading.Event, ...] = ()
    # Names the budget in the halt reason, e.g. "wall clock exceeded 30s".
    reason: str = "wall clock deadline exceeded"

    def stop_requested(self) -> bool:
        return any(stop.is_set() for stop in self.stops)


_ACTIVE: ContextVar[RunBounds | None] = ContextVar("hiveloom_run_bounds", default=None)
# How often a backoff under more than one stop event looks at the outer ones.
_STOP_POLL_SECONDS = 0.1


@contextmanager
def run_bounds(
    *,
    deadline: float | None = None,
    stop: threading.Event | None = None,
    reason: str = "wall clock deadline exceeded",
) -> Iterator[RunBounds]:
    """Activate bounds for the current run; nested runs only ever tighten them.

    A delegated child runs inside its parent's call stack, so it inherits the
    parent's deadline and stop events: the tighter deadline wins, and a stop
    of any enclosing run stops the child too.
    """
    stops: tuple[threading.Event, ...] = (stop,) if stop is not None else ()
    outer = _ACTIVE.get()
    if outer is not None:
        if outer.deadline is not None and (deadline is None or outer.deadline < deadline):
            deadline, reason = outer.deadline, outer.reason
        stops = (*stops, *outer.stops)
    bounds = RunBounds(deadline=deadline, stops=stops, reason=reason)
    token = _ACTIVE.set(bounds)
    try:
        yield bounds
    finally:
        _ACTIVE.reset(token)


def has_deadline() -> bool:
    """Whether the current run has a deadline."""
    bounds = _ACTIVE.get()
    return bounds is not None and bounds.deadline is not None


def check_run_bounds() -> None:
    """Raise if the active run is out of time or has been asked to stop."""
    bounds = _ACTIVE.get()
    if bounds is None:
        return
    if bounds.stop_requested():
        raise RunStopped("stop requested")
    if bounds.deadline is not None and time.monotonic() >= bounds.deadline:
        raise DeadlineExceeded(bounds.reason)


def remaining_seconds(default: float) -> float:
    """``min(default, time left)``; just ``default`` when no deadline is active.

    Raises :class:`DeadlineExceeded` once the budget is spent, so a provider
    never starts an attempt it has no time for.
    """
    bounds = _ACTIVE.get()
    if bounds is None or bounds.deadline is None:
        return default
    left = bounds.deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExceeded(bounds.reason)
    return min(default, left)


def wait_backoff(seconds: float, sleep: Callable[[float], None] = time.sleep) -> None:
    """Sleep between attempts without outliving the run or ignoring a stop.

    Capped to the time left. With the default ``sleep`` and a stop event
    active, the wait ends as soon as a stop is requested (raising
    :class:`RunStopped`); an injected ``sleep`` (a test double) is always
    honoured, and the bounds are checked when it returns.
    """
    bounds = _ACTIVE.get()
    if bounds is None:
        sleep(seconds)
        return
    seconds = remaining_seconds(seconds)
    if bounds.stops and sleep is time.sleep:
        end = time.monotonic() + seconds
        # The innermost stop wakes the wait at once; outer ones are polled.
        poll = _STOP_POLL_SECONDS if len(bounds.stops) > 1 else seconds
        while (left := end - time.monotonic()) > 0 and not bounds.stop_requested():
            bounds.stops[0].wait(min(left, poll))
    else:
        sleep(seconds)
    check_run_bounds()
