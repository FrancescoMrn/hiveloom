"""Trace event schema and the append-only TraceWriter.

Every run produces a JSONL trace. Events share a common envelope (``run_id``,
``harness_name``, ``harness_id``, ``harness_version_hash``, ``timestamp``,
``seq``, ``type``)
plus a type-specific ``payload``. The spec's ``redact`` patterns are applied
before anything is persisted.

Events are **chained**: each carries ``prev``, the sha256 of the preceding
line as written. Editing or removing a line therefore breaks the chain at that
point, which ``hiveloom trace --verify`` reports. Append-only becomes a
checkable property rather than an intention.

The conversation is recorded **progressively**: each message is appended once
as a ``context_append`` event, and ``model_call`` references the folded state
rather than re-snapshotting it. :mod:`hiveloom.logging.journal` is the fold
that reads it back.

Event types: ``run_started``, ``context_append``, ``context_system``,
``context_tools``, ``model_call``, ``model_response``, ``tool_call``,
``tool_update``, ``tool_result``, ``guardrail_triggered``, ``hook_triggered``,
``hook_error``, ``context_compaction``, ``verification_result``,
``run_finished``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.logging.journal import CONTEXT_EVENTS
from hiveloom.spec.loader import dump_spec, dump_spec_for_behavior_hash
from hiveloom.spec.schema import HarnessSpec


def payload_hash(value: Any) -> str:
    """Stable short hash of a JSON-serialisable payload."""
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def spec_version_hash(spec: HarnessSpec, base_dir: str | Path | None = None) -> str:
    """Return the stable harness version key used for evolution buckets.

    The YAML spec is always included. When ``base_dir`` is supplied (the
    runtime/package path), referenced code hooks, declared local extensions,
    skill files, and output schemas are included too. A validator edit is a
    behavioral harness change and must never share a fitness bucket or an
    artifact name with the previous validator.

    ``base_dir=None`` preserves the spec-only behavior for callers that only
    have an in-memory spec.
    """
    hasher = hashlib.sha256()
    hasher.update(dump_spec_for_behavior_hash(spec).encode("utf-8"))
    if base_dir is not None:
        base = Path(base_dir).resolve()
        for relative in _versioned_files(spec, base):
            hasher.update(relative.as_posix().encode("utf-8"))
            hasher.update(b"\0")
            try:
                hasher.update((base / relative).read_bytes())
            except OSError:
                # Full validation produces the actionable error; the sentinel
                # keeps a missing declared dependency distinct from an empty one.
                hasher.update(b"<missing>")
            hasher.update(b"\0")
    return hasher.hexdigest()[:12]


#: Cap on inlined file bodies when ``logging.snapshot_files`` is on. A journal
#: that carries its own harness stays a journal; one that carries a vendored
#: dependency tree is a tarball with extra steps.
SNAPSHOT_BYTE_BUDGET = 256 * 1024


def harness_snapshot(
    spec: HarnessSpec, base: str | Path, *, include_files: bool = False
) -> dict[str, Any]:
    """A self-describing record of the harness that produced a run.

    The dumped spec plus a ``path -> sha256`` manifest of every local
    behavioural file — exactly the set :func:`_versioned_files` already
    fingerprints for the version hash, so the snapshot and the hash can never
    disagree about what "this harness" means.

    ``include_files`` inlines the bodies too (``logging.snapshot_files``),
    making the journal portable at the cost of size; it stops at
    :data:`SNAPSHOT_BYTE_BUDGET` and reports what it skipped rather than
    silently truncating.
    """
    root = Path(base).resolve()
    manifest: dict[str, str] = {}
    contents: dict[str, str] = {}
    skipped: list[str] = []
    spent = 0

    for relative in _versioned_files(spec, root):
        key = relative.as_posix()
        try:
            raw = (root / relative).read_bytes()
        except OSError:
            manifest[key] = "<missing>"
            continue
        manifest[key] = hashlib.sha256(raw).hexdigest()
        if not include_files:
            continue
        if spent + len(raw) > SNAPSHOT_BYTE_BUDGET:
            skipped.append(key)
            continue
        try:
            contents[key] = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(key)
            continue
        spent += len(raw)

    snapshot: dict[str, Any] = {
        "spec": dump_spec(spec),
        "version_hash": spec_version_hash(spec, root),
        "files": manifest,
    }
    if include_files:
        snapshot["contents"] = contents
        if skipped:
            snapshot["skipped"] = skipped
    return snapshot


def _versioned_files(spec: HarnessSpec, base: Path) -> list[Path]:
    """Local behavioral files that belong in a harness version fingerprint."""
    paths: set[Path] = set()

    def add_file(value: str) -> None:
        candidate = Path(value)
        if candidate.is_absolute():
            return
        resolved = (base / candidate).resolve()
        if resolved.is_file() and base in resolved.parents:
            paths.add(resolved.relative_to(base))

    def add_ref(ref: Any) -> None:
        code = getattr(ref, "code", None)
        if isinstance(code, str):
            add_file(code.rsplit(":", 1)[0])
        params = getattr(ref, "params", lambda: {})()
        schema_file = params.get("schema_file")
        if isinstance(schema_file, str):
            add_file(schema_file)

    for refs in (spec.tools, spec.guardrails, spec.verify.validators, spec.hooks):
        for ref in refs:
            add_ref(ref)

    # A playbook is runtime behavior, not just YAML metadata: its prompt,
    # boundary hooks, and additive validators all affect what a run does. Keep
    # them in the same manifest used for version hashing and fork recovery.
    for playbook in spec.playbooks:
        if playbook.prompt:
            add_file(playbook.prompt)
        for hook in (playbook.on_enter, playbook.on_exit):
            if hook:
                add_file(hook.rsplit(":", 1)[0])
        for validator in playbook.validators:
            add_ref(validator)

    for extension in spec.extensions:
        if extension.endswith(".py"):
            add_file(extension)

    for skill in spec.skills:
        skill_dir = (base / "skills" / skill).resolve()
        if skill_dir.is_dir() and base in skill_dir.parents:
            paths.update(
                path.relative_to(base)
                for path in skill_dir.rglob("*")
                if path.is_file()
            )
    return sorted(paths)


class TraceEvent(BaseModel):
    """One trace event with a common envelope and a typed payload dict."""

    run_id: str
    harness_name: str
    # The spec's stable `id`, "" for a pre-1.0 harness that never adopted one.
    # Envelope material like the name: the Hive keys evidence on it.
    harness_id: str = ""
    harness_version_hash: str
    seq: int
    timestamp: str
    type: str
    # sha256 of the preceding *written* line; "" for the first one. Assigned at
    # write time, so events dropped by the logging level never enter the chain.
    prev: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class TraceWriter:
    """Writes trace events to ``<trace_dir>/<run_id>.jsonl`` (redacted).
    """

    def __init__(
        self,
        trace_dir: str | Path,
        run_id: str,
        harness_name: str,
        version_hash: str,
        redact_patterns: list[str] | None = None,
        level: str = "journal",
        on_event=None,
        harness_id: str = "",
    ):
        self._dir = Path(trace_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{run_id}.jsonl"
        self._run_id = run_id
        self._name = harness_name
        self._harness_id = harness_id
        self._version = version_hash
        self._seq = 0
        self._patterns = [re.compile(p, re.IGNORECASE) for p in (redact_patterns or [])]
        # Normalise the 0.x names here as well as in the spec: TraceWriter is
        # public, and an embedding caller may pass either.
        self._level = {"full": "journal", "tool_calls_only": "summary"}.get(level, level)
        self._on_event = on_event
        # Parallel tool execution emits tool_update/tool events from worker
        # threads; seq assignment and the file append must stay atomic.
        self._lock = threading.Lock()
        # The seq of the most recent context-mutating event. `model_call`
        # records it so the journal states which folded context it consumed,
        # instead of leaving that to an ordering convention.
        self._context_head = -1
        # Last emitted system prompt / tool payload, so each is journalled
        # only when it actually changes. Both move during a run (playbook
        # switches, a pinned plan), and both are wasteful to repeat per turn.
        self._system_hash: str | None = None
        self._tools_hash: str | None = None
        # Rolling chain head: sha256 of the last line written to this journal.
        self._prev_hash = ""
        self.events: list[TraceEvent] = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def context_head(self) -> int:
        """Seq of the last context-mutating event, or -1 before any."""
        with self._lock:
            return self._context_head

    def emit_context_system(self, system: str) -> str:
        """Journal the system prompt if it changed. Returns its hash either way."""
        digest = payload_hash(system)
        with self._lock:
            unchanged = digest == self._system_hash
            self._system_hash = digest
        if not unchanged:
            self.emit("context_system", system=system, hash=digest)
        return digest

    def emit_context_tools(self, tools: list[dict[str, Any]]) -> str:
        """Journal the tool payload if it changed. Returns its hash either way."""
        digest = payload_hash(tools)
        with self._lock:
            unchanged = digest == self._tools_hash
            self._tools_hash = digest
        if not unchanged:
            self.emit("context_tools", tools=tools, hash=digest)
        return digest

    def emit(self, event_type: str, **payload: Any) -> TraceEvent:
        """Record an event: assign seq/timestamp, redact, append to JSONL.

        Thread-safe: parallel tool execution calls this from worker threads.
        """
        with self._lock:
            event = TraceEvent(
                run_id=self._run_id,
                harness_name=self._name,
                harness_id=self._harness_id,
                harness_version_hash=self._version,
                seq=self._seq,
                timestamp=datetime.now(UTC).isoformat(),
                type=event_type,
                payload=self._redact(payload),
            )
            self._seq += 1
            if event_type in CONTEXT_EVENTS:
                self._context_head = event.seq
            if self._level == "summary" and event_type not in {
                "run_started",
                "run_finished",
                "tool_call",
                "tool_update",
                "tool_retry",
                "tool_result",
                "guardrail_triggered",
                "verification_result",
            }:
                return event
            # Chain over written lines only, and assign it inside the lock so
            # concurrent tool threads cannot interleave a hash with a write.
            event.prev = self._prev_hash
            line = event.model_dump_json()
            self.events.append(event)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001, S110 - a stream consumer must not kill the run
                pass
        return event

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            redacted = value
            for pattern in self._patterns:
                redacted = pattern.sub("[REDACTED]", redacted)
            return redacted
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value
