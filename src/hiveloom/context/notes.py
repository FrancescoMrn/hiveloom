"""Run-scoped notes: what the model chooses to keep, kept out of context.

Spilling (:mod:`hiveloom.context.spill`) gives a run somewhere to *put* what a
tool produced. It gives the model nowhere to put what the model itself worked
out. Everything it concludes lives in the conversation, and the conversation is
the one thing compaction is allowed to throw away — so a finding from turn 3
either occupies budget for the whole run or is gone by turn 20, with nothing in
between.

``notes`` is that in-between: a small, named, run-private store the model writes
to and reads back by name. A note survives compaction by construction, because
it was never a message. What the conversation carries is not the note but the
*index* of notes — name, size, first line — rendered into the system prompt, so
the model always knows what it has written down and pays a line for each rather
than a body.

The boundary is the same one spilled results live behind, for the same reasons:

* notes live under the run's own directory beside the spill objects (0600 files
  in a 0700 directory), inside the trace directory that ``file_read``/
  ``file_write`` refuse and :mod:`hiveloom.confine` masks from spawned
  processes;
* a name is not a capability. Resolution goes through a per-run authorized map
  filled by writing the note here, or by an explicit hash-bound grant that
  ``hiveloom fork`` carried over from the parent's *verified* journal. A
  resumed fork that merely mentions a name gains nothing;
* ``logging.redact`` is applied before the write, so a note carries exactly
  what the journal would have carried;
* every write and delete is journaled (``note_written`` / ``note_deleted``,
  content included), so ``trace --verify`` and ``fork`` see the same store the
  model saw.

Counts and sizes are bounded by the spec, not trimmed at run time as a
surprise: a write past ``max_notes`` or ``max_note_bytes`` is a tool error
naming the limit, which the model can act on, rather than a silent drop.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hiveloom.context.spill import (
    DEFAULT_READ_BYTES,
    _as_int,
    _decode,
    _matches_manifest,
    _private_dir,
    _write_private,
)
from hiveloom.errors import HiveloomError
from hiveloom.tools.registry import Tool, ToolError

#: The tool's name, and the sub-directory it keeps notes in under the run's
#: own spill directory.
NOTES_TOOL = "notes"
NOTES_DIR = "notes"

#: Where a fork's inherited notes land, beside the per-run directories — the
#: same arrangement spilled objects use (``spill/inherited``).
NOTES_INHERITED_DIR = "notes-inherited"

#: Note names are filenames. Lowercase, no separators, no dots: a name can then
#: be joined to the store directory directly without a traversal check being
#: the thing standing between the model and the rest of the disk.
NOTE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

#: Spec defaults and the hard ceilings that bound them. The count ceiling keeps
#: the prompt index bounded (it is rendered on every turn); the byte ceiling
#: keeps one note from becoming an unbounded file written a line at a time.
DEFAULT_MAX_NOTES = 32
MAX_NOTES_CAP = 256
MAX_NOTE_BYTES_CAP = 1024 * 1024
#: Used when the harness has spilling switched off, so there is no
#: ``max_inline_bytes`` to take the note-size default from.
FALLBACK_MAX_NOTE_BYTES = 16 * 1024

#: Characters of a note's first line shown in the system-prompt index. The
#: index is a table of contents, not a second copy of the notes.
_INDEX_LINE_CHARS = 100


class NotesError(HiveloomError):
    """Raised when a note cannot be written, read, or resolved for this run."""


@dataclass(frozen=True)
class NoteRecord:
    """One stored note: what was written, and what the journal records of it."""

    name: str
    content: str
    total_bytes: int
    sha256: str
    path: Path
    replaced: bool


class NotesStore:
    """Run-scoped named storage for what the model decides to remember.

    ``root`` is the run's spill root (``<trace_dir>/spill``); notes go to
    ``<root>/<run_id>/notes/<name>.txt``. Authority is the ``_authorized`` map,
    never the filesystem: a name resolves only if this run wrote it or a fork
    record explicitly granted it.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str,
        max_notes: int = DEFAULT_MAX_NOTES,
        max_note_bytes: int = FALLBACK_MAX_NOTE_BYTES,
        max_read_bytes: int = FALLBACK_MAX_NOTE_BYTES,
        redact: Callable[[str], str] | None = None,
        journal: Callable[..., Any] | None = None,
    ) -> None:
        self._root = Path(root)
        self._dir = self._root / run_id / NOTES_DIR
        self._run_id = run_id
        self._max_notes = max(1, min(int(max_notes), MAX_NOTES_CAP))
        self._max_note_bytes = max(1, min(int(max_note_bytes), MAX_NOTE_BYTES_CAP))
        self._max_read_bytes = max(1, int(max_read_bytes))
        self._redact = redact
        self._journal = journal
        # Tool calls in one turn may run in parallel (`loop.tool_execution`),
        # and two writes are a check-then-act against the same capacity, so
        # the check and the slot it claims are taken under this lock together.
        self._lock = threading.Lock()
        # name -> (file, sha256, bytes). Hash-bound like a spill handle, so a
        # note that changed underneath the run is refused rather than served.
        self._authorized: dict[str, tuple[Path, str, int]] = {}

    @property
    def inherited_dir(self) -> Path:
        """Where ``hiveloom fork`` puts the notes a fork may read back."""
        return self._root / NOTES_INHERITED_DIR

    @property
    def names(self) -> list[str]:
        """The note names this run may resolve, in index order."""
        with self._lock:
            return sorted(self._authorized)

    @property
    def capacity(self) -> int:
        """How many notes this run may hold at once."""
        return self._max_notes

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def write(self, name: str, content: str) -> NoteRecord:
        """Store (or replace) a note and journal what was stored."""
        name = _check_name(name)
        if content is None:
            raise NotesError("a note needs content to write")
        stored = self._redact(content) if self._redact is not None else content
        data = stored.encode("utf-8")
        if len(data) > self._max_note_bytes:
            raise NotesError(
                f"note '{name}' is {len(data)} bytes; this harness allows "
                f"{self._max_note_bytes} per note. Write a shorter note, or split it."
            )
        target = self._dir / f"{name}.txt"
        with self._lock:
            replaced = name in self._authorized
            if not replaced and len(self._authorized) >= self._max_notes:
                in_use = sorted(self._authorized)
                raise NotesError(
                    f"this harness allows {self._max_notes} notes and they are all in "
                    f"use ({', '.join(in_use)}). Replace one by name, or delete one."
                )
            # The capacity check and the name's claim on a slot are one step,
            # under one lock: parallel tool calls past the last free slot must
            # not all find it free. The placeholder holds the slot while the
            # file is written and is exchanged for the real record below; its
            # empty digest keeps the half-written note unreadable meanwhile.
            previous = self._authorized.get(name)
            reservation = (target, "", len(data))
            self._authorized[name] = reservation
        try:
            _private_dir(self._root)
            _private_dir(self._root / self._run_id)
            _private_dir(self._dir)
            # Written aside and moved into place, never an in-place truncate
            # nor an unlink then create: the note is never half-written, its
            # mode is set at creation rather than after, and two writes of one
            # name in a parallel turn cannot race for the filename.
            staged = self._dir / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                _write_private(staged, data)
                os.replace(staged, target)
            finally:
                with suppress(OSError):
                    staged.unlink()
        except OSError as exc:
            with self._lock:
                # Give the slot back — but only if it is still ours to give.
                if self._authorized.get(name) == reservation:
                    if previous is None:
                        self._authorized.pop(name, None)
                    else:
                        self._authorized[name] = previous
            raise NotesError(f"could not store note '{name}': {exc}") from exc
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._authorized[name] = (target, digest, len(data))
        self._emit(
            "note_written",
            name=name,
            bytes=len(data),
            sha256=digest,
            replaced=replaced,
            content=stored,
        )
        return NoteRecord(
            name=name,
            content=stored,
            total_bytes=len(data),
            sha256=digest,
            path=target,
            replaced=replaced,
        )

    def delete(self, name: str) -> bool:
        """Drop a note. Returns False when this run has no such note."""
        name = _check_name(name)
        with self._lock:
            record = self._authorized.pop(name, None)
        if record is None:
            return False
        # Best effort: authority is the map, so a file that cannot be removed
        # is already unreachable. Losing the bytes on disk is a cleanup
        # failure, not a correctness one.
        with suppress(OSError):
            record[0].unlink()
        self._emit("note_deleted", name=name)
        return True

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def _resolve(self, name: str) -> Path:
        """Map a name to the file this run may read for it, or refuse."""
        with self._lock:
            record = self._authorized.get(name)
        if record is None:
            known = ", ".join(self.names) or "none"
            raise NotesError(
                f"no note named '{name}' in this run (notes: {known}). A note is "
                "readable only in the run that wrote it, or in a fork that "
                "explicitly inherited it."
            )
        path, digest, expected_bytes = record
        if not _matches_manifest(path, digest, expected_bytes):
            raise NotesError(
                f"the stored note '{name}' no longer matches what was written for it"
            )
        return path

    def read(self, name: str, offset: int = 0, limit: int | None = None) -> str:
        """Return a byte range of a note, capped like ``read_tool_result``.

        A note may be written up to ``max_note_bytes``, which can exceed what
        one result may put into context, so reads are ranged for the same
        reason spilled results are: the store must not become a way to
        reintroduce more than the inline budget in a single call.
        """
        name = _check_name(name)
        path = self._resolve(name)
        total = path.stat().st_size
        offset = max(0, int(offset))
        limit = DEFAULT_READ_BYTES if limit is None else int(limit)
        limit = max(1, min(limit, self._max_read_bytes))
        if offset and offset >= total:
            return (
                f"[note {name}] offset {offset} is past the end of the note "
                f"({total} bytes)."
            )
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(limit)
        end = offset + len(chunk)
        footer = (
            f"\n[note {name}] {total - end} bytes remain; continue at offset {end}."
            if end < total
            else ""
        )
        return f"[note {name}] bytes {offset}-{end} of {total}\n{_decode(chunk)}{footer}"

    # ------------------------------------------------------------------ #
    # The system-prompt index
    # ------------------------------------------------------------------ #
    def index(self) -> list[tuple[str, int, str]]:
        """``(name, bytes, first line)`` per note, sorted by name.

        Sorted rather than write-ordered: the index is re-rendered on every
        turn, and a section that reorders itself as the run proceeds would
        invalidate the prompt cache for no gain.
        """
        with self._lock:
            held = sorted(self._authorized.items())
        return [(name, size, _first_line(path)) for name, (path, _digest, size) in held]

    def index_text(self) -> str | None:
        """The ``# Notes`` section, or None when nothing has been written."""
        rows = self.index()
        if not rows:
            return None
        lines = [
            "# Notes",
            "Your own notes for this run. They are not part of the conversation, "
            "so they survive when older turns are compacted away. Read one in "
            f'full with {NOTES_TOOL}(action="read", name=…); replace it with '
            'action="write"; remove it with action="delete".',
        ]
        lines += [f"- {name} ({size} bytes): {head}" for name, size, head in rows]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Admission
    # ------------------------------------------------------------------ #
    def inherit(self, manifests: Any, source: str | Path) -> list[str]:
        """Authorize hash-bound notes a fork explicitly carried over.

        The list comes from ``fork.yaml``, written from the parent's verified
        journal — never from the resumed transcript, which is model-visible
        text.
        """
        directory = Path(source)
        granted: list[str] = []
        for manifest in manifests or []:
            if not isinstance(manifest, dict):
                continue
            name = str(manifest.get("name", ""))
            digest = str(manifest.get("sha256", ""))
            expected_bytes = manifest.get("bytes")
            if not NOTE_NAME_RE.fullmatch(name):
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            if not isinstance(expected_bytes, int) or expected_bytes < 0:
                continue
            body = directory / f"{name}.txt"
            if not _matches_manifest(body, digest, expected_bytes):
                continue
            with self._lock:
                if len(self._authorized) >= self._max_notes:
                    break
                self._authorized[name] = (body, digest, expected_bytes)
            granted.append(name)
        return granted

    def _emit(self, event: str, **payload: Any) -> None:
        if self._journal is None:
            return
        # Journalling must never be what fails a stored note.
        with suppress(Exception):  # noqa: BLE001 - best effort by design
            self._journal(event, **payload)


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not NOTE_NAME_RE.fullmatch(name):
        raise NotesError(
            f"'{name}' is not a usable note name. Use 1-64 lowercase letters, "
            "digits, '-' or '_', starting with a letter or digit."
        )
    return name


def _first_line(path: Path) -> str:
    """The note's first line, bounded, for the prompt index."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(_INDEX_LINE_CHARS * 4)
    except OSError:  # pragma: no cover - the map is verified before this runs
        return ""
    line = _decode(raw).splitlines()[0] if raw else ""
    line = line.strip()
    if len(line) > _INDEX_LINE_CHARS:
        line = line[: _INDEX_LINE_CHARS - 1].rstrip() + "…"
    return line


class NotesTool(Tool):
    """The model's handle on its own run-scoped store.

    Registered from the catalog like any opt-in builtin, then bound by the
    agent loop, which owns the run's directory, its redaction and its journal.
    Unbound (``run --dry-run``, an SDK caller with no run) it fails as a tool
    error rather than crashing the loop.
    """

    name = NOTES_TOOL
    tags = ["write", "memory"]
    supports_updates = False
    wants_run_context = False
    guidelines = (
        f"Use {NOTES_TOOL} to keep findings you will need later — they survive "
        "context compaction, the conversation does not. The system prompt lists "
        "what you have written; read one back by name before relying on it."
    )

    def __init__(self, *, max_notes: int = DEFAULT_MAX_NOTES, max_note_bytes: int = 0) -> None:
        self._store: NotesStore | None = None
        self.max_notes = max(1, min(int(max_notes), MAX_NOTES_CAP))
        # 0 means "take the harness's inline result budget", resolved by the
        # loop, which is where the context config lives.
        self.max_note_bytes = min(int(max_note_bytes or 0), MAX_NOTE_BYTES_CAP)
        self.description = (
            "Keep run-scoped notes: write a named note, read one back, or delete "
            "one. Notes are private to this run and are not part of the "
            "conversation, so they survive when older turns are dropped. The "
            f"system prompt lists the notes you have written (up to {self.max_notes})."
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write", "read", "delete"],
                    "description": "write stores or replaces a note; read returns one; "
                    "delete removes one.",
                },
                "name": {
                    "type": "string",
                    "description": "Note name: 1-64 lowercase letters, digits, '-' or "
                    "'_'. Writing an existing name replaces that note.",
                },
                "content": {
                    "type": "string",
                    "description": "The note's full text (write only).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Byte offset to read from (read only, 0 = the start).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": f"Bytes to return (read only, default {DEFAULT_READ_BYTES}).",
                },
            },
            "required": ["action", "name"],
        }

    def bind(self, store: NotesStore) -> None:
        self._store = store

    @property
    def store(self) -> NotesStore:
        if self._store is None:
            raise ToolError(f"{self.name} is not available in this context")
        return self._store

    def run(
        self,
        action: str = "",
        name: str = "",
        content: Any = None,
        offset: Any = 0,
        limit: Any = None,
        **_: Any,
    ) -> str:
        store = self.store
        action = (action or "").strip().lower()
        try:
            if action == "write":
                if content is None:
                    raise NotesError('write needs "content"')
                record = store.write(name, content if isinstance(content, str) else str(content))
                verb = "replaced" if record.replaced else "wrote"
                return (
                    f"{verb} note '{record.name}' ({record.total_bytes} bytes). "
                    f"{len(store.names)} of {store.capacity} notes in use."
                )
            if action == "read":
                return store.read(
                    name,
                    offset=_as_int("offset", offset, 0) or 0,
                    limit=_as_int("limit", limit),
                )
            if action == "delete":
                if store.delete(name):
                    return f"deleted note '{name}'."
                return f"no note named '{name}' to delete."
        except NotesError as exc:
            raise ToolError(str(exc)) from exc
        raise ToolError(
            f"unknown action '{action}'. Use write, read, or delete."
        )
