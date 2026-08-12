"""Trace event schema and the append-only TraceWriter.

Every run produces a JSONL trace. Events share a common envelope (``run_id``,
``harness_name``, ``harness_version_hash``, ``timestamp``, ``seq``, ``type``)
plus a type-specific ``payload``. The spec's ``redact`` patterns are applied
before anything is persisted.

Event types: ``run_started``, ``model_call``, ``model_response``, ``tool_call``,
``tool_update``, ``tool_result``, ``guardrail_triggered``, ``hook_triggered``,
``hook_error``, ``context_compaction``, ``verification_result``,
``run_finished``.
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.spec.loader import dump_spec
from hiveloom.spec.schema import HarnessSpec


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
    hasher.update(dump_spec(spec).encode("utf-8"))
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

    for refs in (spec.tools, spec.guardrails, spec.verify.validators, spec.hooks):
        for ref in refs:
            code = getattr(ref, "code", None)
            if isinstance(code, str):
                add_file(code.rsplit(":", 1)[0])
            params = getattr(ref, "params", lambda: {})()
            schema_file = params.get("schema_file")
            if isinstance(schema_file, str):
                add_file(schema_file)

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
    harness_version_hash: str
    seq: int
    timestamp: str
    type: str
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TraceWriter:
    """Writes trace events to ``<trace_dir>/<run_id>.jsonl`` (redacted).

    With a ``session_id``, traces group under ``<trace_dir>/<session_id>/`` —
    one directory per conversation/session — and every event carries the id,
    so related runs read together instead of as an undifferentiated pile.
    """

    def __init__(
        self,
        trace_dir: str | Path,
        run_id: str,
        harness_name: str,
        version_hash: str,
        redact_patterns: list[str] | None = None,
        level: str = "full",
        on_event=None,
        session_id: str | None = None,
    ):
        self._dir = Path(trace_dir) if session_id is None else Path(trace_dir) / session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{run_id}.jsonl"
        self._session_id = session_id
        self._run_id = run_id
        self._name = harness_name
        self._version = version_hash
        self._seq = 0
        self._patterns = [re.compile(p, re.IGNORECASE) for p in (redact_patterns or [])]
        self._level = level
        self._on_event = on_event
        # Parallel tool execution emits tool_update/tool events from worker
        # threads; seq assignment and the file append must stay atomic.
        self._lock = threading.Lock()
        self.events: list[TraceEvent] = []

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event_type: str, **payload: Any) -> TraceEvent:
        """Record an event: assign seq/timestamp, redact, append to JSONL.

        Thread-safe: parallel tool execution calls this from worker threads.
        """
        with self._lock:
            event = TraceEvent(
                run_id=self._run_id,
                harness_name=self._name,
                harness_version_hash=self._version,
                seq=self._seq,
                timestamp=datetime.now(UTC).isoformat(),
                type=event_type,
                session_id=self._session_id,
                payload=self._redact(payload),
            )
            self._seq += 1
            if self._level == "tool_calls_only" and event_type not in {
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
            self.events.append(event)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
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
