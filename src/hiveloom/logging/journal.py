"""Reading a run journal back: the fold from events to context state.

A journal records the conversation *progressively* — each message is appended
once, as a ``context_append`` event — rather than re-snapshotting the whole
message list on every model call. The state that went to the model at any
point is therefore not stored anywhere; it is **folded** out of the events
that precede it.

This module is that fold, and it is the single implementation shared by
``hiveloom trace --materialize``, ``hiveloom fork``, and the dev UI. It also
holds :func:`verify_chain`, which checks the journal's append-only claim.

The context-mutating events are:

``context_append``      one message appended to the conversation
``context_compaction``  history rewritten; the payload carries the result
``context_system``      the assembled system prompt changed
``context_tools``       the active tool payload changed

Pre-1.0 traces re-recorded ``system``/``messages``/``tools`` on every
``model_call``; :func:`state_at` treats such a payload as a wholesale replace,
so an old trace still folds correctly (see :func:`fold_events`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Event types that mutate the folded context state.
CONTEXT_EVENTS: frozenset[str] = frozenset(
    {"context_append", "context_compaction", "context_system", "context_tools"}
)


@dataclass
class ContextState:
    """The (system, messages, tools) triple that went to the model."""

    system: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)

    def as_request(self) -> dict[str, Any]:
        """The shape a provider is called with — what ``--materialize`` prints."""
        return {"system": self.system, "messages": self.messages, "tools": self.tools}


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL journal into a list of event dicts, skipping unparseable lines."""
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def fold_events(events: list[dict[str, Any]]) -> ContextState:
    """Fold an ordered event list into the context state it produces."""
    state = ContextState()
    for event in events:
        payload = event.get("payload") or {}
        etype = event.get("type")
        if etype == "context_append":
            message = payload.get("message")
            if isinstance(message, dict):
                state.messages.append(message)
        elif etype == "context_compaction":
            replacement = payload.get("messages")
            if isinstance(replacement, list):
                state.messages = list(replacement)
        elif etype == "context_system":
            text = payload.get("system")
            if isinstance(text, str):
                state.system = text
        elif etype == "context_tools":
            tools = payload.get("tools")
            if isinstance(tools, list):
                state.tools = list(tools)
        elif etype == "model_call":
            if payload.get("inline") or "context_head" in payload:
                # `inline` marks an out-of-band request that is not part of the
                # conversation (the compaction summarisation turn), and so must
                # not disturb the fold. `context_head` marks a 1.0 journal,
                # where the context comes from the context_* events alone.
                continue
            # Pre-1.0 compatibility: a snapshot-shaped model_call carries the
            # whole request, so it replaces whatever was folded so far.
            if isinstance(payload.get("messages"), list):
                state.messages = list(payload["messages"])
            if isinstance(payload.get("system"), str):
                state.system = payload["system"]
            if isinstance(payload.get("tools"), list):
                state.tools = list(payload["tools"])
    return state


def state_at(
    events: list[dict[str, Any]], seq: int | None = None, *, inclusive: bool = True
) -> ContextState:
    """The context state as of ``seq``.

    ``seq=None`` folds the whole journal. ``inclusive=False`` folds everything
    strictly before ``seq`` — which is what materialising a ``model_call``
    wants, since its own event is emitted *after* the context events it
    consumed.
    """
    if seq is None:
        return fold_events(events)
    ordered = sorted(events, key=lambda e: e.get("seq", 0))
    if inclusive:
        return fold_events([e for e in ordered if e.get("seq", 0) <= seq])
    return fold_events([e for e in ordered if e.get("seq", 0) < seq])


def state_at_model_call(events: list[dict[str, Any]], seq: int) -> ContextState:
    """The exact request a ``model_call`` at ``seq`` was built from."""
    return state_at(events, seq, inclusive=False)


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #
@dataclass
class ChainResult:
    """The outcome of checking a journal's hash chain."""

    ok: bool
    checked: int
    chained: bool
    broken_at: int | None = None
    reason: str = ""

    def summary(self) -> str:
        if not self.chained:
            return (
                f"unchained: {self.checked} events carry no `prev` "
                "(written before hiveloom 1.0)"
            )
        if self.ok:
            return f"intact: {self.checked} events, chain unbroken"
        return f"BROKEN at line {self.broken_at}: {self.reason}"


def verify_chain(path: str | Path) -> ChainResult:
    """Check that every line commits to the sha256 of the line before it.

    A journal is append-only by construction; this is what makes it *checkable*.
    Editing, reordering, or removing a line breaks the chain at that point.

    Pre-1.0 traces carry no ``prev`` at all. That is reported as ``chained =
    False`` rather than as a break — "we cannot tell" is a different answer
    from "it was tampered with", and conflating them would make the check
    useless for exactly the traces most likely to be old.
    """
    lines = [
        line
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        return ChainResult(ok=True, checked=0, chained=True)

    try:
        first = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        return ChainResult(
            ok=False, checked=0, chained=True, broken_at=0, reason=f"unparseable line: {exc}"
        )
    if "prev" not in first:
        return ChainResult(ok=True, checked=len(lines), chained=False)

    expected = ""
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return ChainResult(
                ok=False,
                checked=index,
                chained=True,
                broken_at=index,
                reason=f"unparseable line: {exc}",
            )
        actual = event.get("prev", "")
        if actual != expected:
            want = expected[:12] or "<genesis>"
            got = actual[:12] or "<empty>"
            return ChainResult(
                ok=False,
                checked=index,
                chained=True,
                broken_at=index,
                reason=(
                    f"event seq {event.get('seq')} expected prev {want}, found {got}"
                ),
            )
        expected = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return ChainResult(ok=True, checked=len(lines), chained=True)
