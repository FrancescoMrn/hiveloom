"""Retrievable tool-output spilling: keep large results out of context, intact.

A tool result that does not fit the inline budget is written whole to private,
run-scoped storage; what the model sees instead is a bounded head/tail preview,
the number of omitted bytes, and an opaque **handle**. Two auto-exposed tools
read the omitted part back — :class:`ReadToolResultTool` by byte range and
:class:`SearchToolResultTool` by substring — so nothing the tool produced is
lost for the rest of the run.

This replaces plain truncation, where the tail of a large result (often the
part that carries the answer, the error, or the summary line) simply ceased to
exist for the model: the full text went to the journal, and the model's file
tools deliberately cannot read the journal.

**A handle is the model's only way in.** Objects live under the configured
trace directory, which ``file_read``/``file_write`` refuse like the rest of
``.hiveloom`` (see :func:`hiveloom.package.is_sensitive_path`). Authority is a
recorded fact, never inferred from a name: a handle resolves only through a
per-run map, filled either by minting the object (:meth:`SpillStore.spill`) or
by an explicit grant a fork carried over from its parent's *verified* journal
(:meth:`SpillStore.inherit`). Inventing, guessing or quoting a handle grants
nothing — including in a resumed fork, whose transcript is model-visible text.
Each run writes into its own 0700 directory, and objects are created
exclusively at 0600.

That is a boundary on the *tools*, not on the filesystem. A harness that also
grants ``shell`` a file-reading command with arbitrary arguments is granting a
second path to the same bytes — and to the journal beside them, which holds
every tool result in full. Two things narrow it:
:mod:`hiveloom.confine` masks ``.hiveloom`` and the trace directory from every
spawned process wherever an OS sandbox exists, and the shell tool refuses
arguments that name those paths on every platform. A recursive walk that never
names the directory is only stopped by the sandbox, which is what
``confinement.mode: require`` is for. Files are written 0600 in a 0700
directory, so on a shared machine the boundary holds against other users
regardless.

Redaction applies before the write, not after, so a spill object carries
exactly what the journal would have carried — ``logging.redact`` cannot be
sidestepped by being large.

Spilling is best effort: if the object cannot be written, the original result
goes to the model unchanged. Losing context economy is a far smaller failure
than losing the result.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveloom.errors import HiveloomError
from hiveloom.tools.registry import Tool, ToolError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.spec.schema import ToolResultsConfig

#: The two retrieval tools. Registered inactive and activated by the loop on
#: the first spill, so a harness that never spills never pays for them in its
#: tool payload.
READ_TOOL = "read_tool_result"
SEARCH_TOOL = "search_tool_result"
TOOL_NAMES = (READ_TOOL, SEARCH_TOOL)

#: Retrieval results are never themselves spilled — a bounded read that spills
#: would hand back another handle, and the model would loop.
EXEMPT_TOOLS = frozenset(TOOL_NAMES)

#: Where a fork's inherited objects live, beside the per-run directories.
INHERITED_DIR = "inherited"

#: Handle shape. Opaque and unguessable so it cannot be forged from a run id,
#: and free of path separators so it can name a file directly.
HANDLE_RE = re.compile(r"tr_[0-9a-f]{16}")

#: Bytes returned by ``read_tool_result`` when the model names no limit.
DEFAULT_READ_BYTES = 4096
#: Cap on one ``search_tool_result`` report, whatever the match count.
SEARCH_REPORT_BYTES = 4000
#: Bytes of surrounding text shown on each side of a search match.
SEARCH_CONTEXT_BYTES = 120
#: Matches reported before the rest are only counted.
SEARCH_MAX_MATCHES = 20
#: Bytes held in memory at once while searching. A spilled object is large by
#: definition, so it is scanned in windows rather than read whole.
_SEARCH_CHUNK_BYTES = 1024 * 1024
#: Matches counted before the scan gives up and reports "N+". A query matching
#: every byte of a huge object should not build a huge list of offsets.
_SEARCH_SCAN_LIMIT = 10_000


class SpillError(HiveloomError):
    """Raised when a handle cannot be resolved for this run."""


@dataclass(frozen=True)
class SpillRecord:
    """One spilled result: where it went and what the model sees in its place."""

    handle: str
    preview: str
    total_bytes: int
    omitted_bytes: int
    path: Path


def _private_dir(path: Path) -> None:
    """Create a directory only this user can enter (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError, NotImplementedError):  # pragma: no cover - Windows
        path.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    """Create a file exclusively at 0600 and write it.

    ``O_EXCL`` rather than a plain open: the file is named by a fresh random
    handle, so an existing one means either a collision or something else
    placing a file there, and neither is a thing to overwrite. The mode is set
    at creation rather than after, so the content is never briefly readable by
    other users on a shared machine.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    with suppress(OSError, NotImplementedError):  # pragma: no cover - Windows
        path.chmod(0o600)


def _decode(data: bytes) -> str:
    """Lossy decode — a byte slice may cut a multi-byte character in half."""
    return data.decode("utf-8", errors="replace")


class SpillStore:
    """Run-scoped storage for oversized tool results.

    ``root`` is a directory under the run's trace directory. Each object is
    ``<handle>.txt`` (the bytes as persisted) beside ``<handle>.json`` (the
    producing tool, run id, size) — enough for a fork to carry an inherited
    object with it, and for ``hiveloom trace`` to explain one.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str,
        config: ToolResultsConfig,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self._root = Path(root)
        # One directory per run. A handle is then not merely unguessable but
        # unreachable across runs by construction: resolution consults a
        # per-run map, and nothing in another run's directory is in it.
        self._run_dir = self._root / run_id
        self._run_id = run_id
        self._config = config
        self._redact = redact
        self._lock = threading.Lock()
        # handle -> the file this run is authorized to read for it. Authority
        # is a recorded fact, never inferred from a name.
        self._authorized: dict[str, Path] = {}

    @property
    def inherited_dir(self) -> Path:
        """Where ``hiveloom fork`` puts the objects a fork may read back."""
        return self._root / INHERITED_DIR

    @property
    def handles(self) -> list[str]:
        """The handles this run may resolve."""
        with self._lock:
            return list(self._authorized)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def exceeds_budget(self, content: str) -> bool:
        return len(content.encode("utf-8")) > self._config.max_inline_bytes

    def spill(self, *, tool: str, content: str) -> SpillRecord | None:
        """Persist ``content`` and return its preview, or None to keep it inline.

        None means either that the result fits the budget or that the write
        failed; the caller passes the original result through in both cases.
        """
        if not self.exceeds_budget(content):
            return None
        stored = self._redact(content) if self._redact is not None else content
        data = stored.encode("utf-8")
        handle = f"tr_{secrets.token_hex(8)}"
        body = self._run_dir / f"{handle}.txt"
        try:
            _private_dir(self._root)
            _private_dir(self._run_dir)
            _write_private(body, data)
            _write_private(
                self._run_dir / f"{handle}.json",
                json.dumps(
                    {
                        "handle": handle,
                        "run_id": self._run_id,
                        "tool": tool,
                        "bytes": len(data),
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                ).encode("utf-8"),
            )
        except OSError:
            # Best effort by design: the caller keeps the full result inline.
            return None
        with self._lock:
            self._authorized[handle] = body
        preview, omitted = self._preview(handle, data)
        return SpillRecord(
            handle=handle,
            preview=preview,
            total_bytes=len(data),
            omitted_bytes=omitted,
            path=body,
        )

    def _preview(self, handle: str, data: bytes) -> tuple[str, int]:
        """Head, an instruction marker naming the handle, then tail."""
        total = len(data)
        head_n = min(self._config.preview_head_bytes, total)
        tail_n = min(self._config.preview_tail_bytes, total - head_n)
        head = _decode(data[:head_n])
        tail = _decode(data[total - tail_n :]) if tail_n else ""
        omitted = total - head_n - tail_n
        marker = (
            f"\n\n[hiveloom spill] handle={handle} — this result was "
            f"{total} bytes; {omitted} are omitted here and stored whole.\n"
            f"Shown above: bytes 0-{head_n}. "
            + (f"Shown below: bytes {total - tail_n}-{total}.\n" if tail_n else "\n")
            + f'Read the omitted part with {READ_TOOL}(handle="{handle}", '
            f"offset={head_n}, limit={DEFAULT_READ_BYTES}), or locate it first with "
            f'{SEARCH_TOOL}(handle="{handle}", query="..."). '
            "Do not guess at the omitted content — read it.\n"
            "[/hiveloom spill]\n\n"
        )
        return f"{head}{marker}{tail}", omitted

    # ------------------------------------------------------------------ #
    # Admission & resolution
    # ------------------------------------------------------------------ #
    def inherit(self, handles: Sequence[str], source: str | Path) -> list[str]:
        """Authorize handles a fork explicitly carried over, from ``source``.

        The list comes from the fork record, which ``hiveloom fork`` writes
        from the parent's *verified* journal — not from the seeded messages.
        A conversation is model-visible text: a resumed run whose transcript
        mentions ``tr_1234…`` must not thereby gain the authority to read it,
        or a model that once saw a handle could recover the object in any
        later fork by quoting it back.
        """
        directory = Path(source)
        granted: list[str] = []
        for handle in handles:
            if not HANDLE_RE.fullmatch(str(handle)):
                continue
            body = directory / f"{handle}.txt"
            if not body.is_file():
                continue
            with self._lock:
                self._authorized[handle] = body
            granted.append(handle)
        return granted

    def _resolve(self, handle: str) -> Path:
        """Map a handle to the file this run may read for it, or refuse.

        The map is the authority. A handle that was never minted here and never
        explicitly inherited resolves to nothing — whether it was invented,
        guessed, or read off another run's directory listing.
        """
        handle = (handle or "").strip()
        with self._lock:
            path = self._authorized.get(handle)
        if path is None:
            raise SpillError(
                f"unknown handle '{handle}'. A handle grants no access on its own: "
                "it is readable only in the run that produced it, or in a fork that "
                "explicitly inherited it."
            )
        if not path.is_file():
            raise SpillError(f"the stored result for '{handle}' is no longer on disk")
        return path

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def read(self, handle: str, offset: int = 0, limit: int | None = None) -> str:
        """Return a byte range of a stored result, with a range header.

        The range is capped at ``max_inline_bytes``: a read may never put more
        into context than the budget that caused the spill in the first place.
        """
        path = self._resolve(handle)
        total = path.stat().st_size
        offset = max(0, int(offset))
        limit = DEFAULT_READ_BYTES if limit is None else int(limit)
        limit = max(1, min(limit, self._config.max_inline_bytes))
        if offset >= total:
            return (
                f"[{handle}] offset {offset} is past the end of the stored result "
                f"({total} bytes). Read from an offset below {total}."
            )
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(limit)
        end = offset + len(chunk)
        header = f"[{handle}] bytes {offset}-{end} of {total}"
        footer = (
            f"\n[{handle}] {total - end} bytes remain; continue at offset {end}."
            if end < total
            else f"\n[{handle}] end of result."
        )
        return f"{header}\n{_decode(chunk)}{footer}"

    def search(self, handle: str, query: str, *, max_matches: int = SEARCH_MAX_MATCHES) -> str:
        """Report case-insensitive substring matches with byte offsets.

        Byte offsets, not line numbers, because they are what :meth:`read`
        takes: a match is meant to be followed by a read around it.

        Scanned in overlapping windows rather than by loading the object. A
        spilled result is large by definition — that is why it was spilled —
        and reading it whole (plus a lowercased copy of it) would make the
        cheap operation the expensive one.
        """
        path = self._resolve(handle)
        if not query:
            raise SpillError("search_tool_result needs a non-empty query")
        needle = query.encode("utf-8", errors="ignore").lower()
        total = path.stat().st_size

        offsets: list[int] = []
        excerpts: dict[int, str] = {}
        truncated = False
        with path.open("rb") as handle_file:
            for window, start in _windows(handle_file, len(needle)):
                lowered = window.lower()
                cursor = 0
                while True:
                    found = lowered.find(needle, cursor)
                    if found < 0:
                        break
                    position = start + found
                    cursor = found + max(1, len(needle))
                    # An overlap region is scanned twice by construction.
                    if offsets and position <= offsets[-1]:
                        continue
                    offsets.append(position)
                    if len(excerpts) < max_matches:
                        left = max(0, found - SEARCH_CONTEXT_BYTES)
                        right = min(len(window), found + len(needle) + SEARCH_CONTEXT_BYTES)
                        excerpts[position] = _decode(window[left:right]).replace("\n", " ⏎ ")
                if len(offsets) > _SEARCH_SCAN_LIMIT:
                    truncated = True
                    break

        if not offsets:
            return (
                f"[{handle}] no match for '{query}' in {total} bytes. "
                f'Try another term, or read a range with {READ_TOOL}(handle="{handle}", '
                "offset=0)."
            )

        shown = offsets[:max_matches]
        counted = f"{len(offsets)}+" if truncated else str(len(offsets))
        lines = [
            f"[{handle}] {counted} match(es) for '{query}' in {total} bytes"
            + (f" (first {len(shown)} shown)" if len(shown) < len(offsets) else "")
        ]
        for position in shown:
            lines.append(f"- byte {position}: …{excerpts.get(position, '')}…")
            if sum(len(line) for line in lines) > SEARCH_REPORT_BYTES:
                lines.append("- (report truncated; narrow the query)")
                break
        lines.append(
            f'Read around a match with {READ_TOOL}(handle="{handle}", offset=<byte>).'
        )
        return "\n".join(lines)


def _windows(stream: Any, needle_length: int):
    """Yield ``(chunk, start_offset)`` windows that overlap by ``needle-1`` bytes.

    The overlap is what keeps a match that straddles a chunk boundary from
    being missed; the cost is that such a region is scanned twice, which the
    caller de-duplicates by offset.
    """
    overlap = max(0, needle_length - 1)
    start = 0
    carry = b""
    while True:
        block = stream.read(_SEARCH_CHUNK_BYTES)
        if not block:
            return
        window = carry + block
        yield window, start
        if overlap:
            carry = window[-overlap:]
            start += len(window) - len(carry)
        else:  # pragma: no cover - an empty needle is refused above
            carry = b""
            start += len(window)


def _handles_in(value: Any) -> list[str]:
    """Every handle quoted anywhere in a message tree, in first-seen order."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for match in HANDLE_RE.findall(node):
                if match not in seen:
                    seen.add(match)
                    found.append(match)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def handles_in_messages(messages: list[dict[str, Any]]) -> list[str]:
    """Public form of :func:`_handles_in`, used by ``hiveloom fork``."""
    return _handles_in(messages)


# --------------------------------------------------------------------- #
# The retrieval tools
# --------------------------------------------------------------------- #
_GUIDELINES = (
    "When a tool result is too large to inline, the runtime replaces its middle "
    f"with a [hiveloom spill] marker naming a handle. Use {SEARCH_TOOL} to find "
    f"the relevant part and {READ_TOOL} to read it; never assume the omitted "
    "bytes said nothing."
)


class _SpillTool(Tool):
    """Shared binding for the two retrieval tools.

    Registered by :func:`hiveloom.tools.registry.build_registry` so they show
    up in ``run --dry-run`` like any other tool, then bound by the agent loop,
    which owns the run's store. Unbound, they fail as a tool error rather than
    crashing the loop.
    """

    tags = ["meta", "context"]
    guidelines = ""
    supports_updates = False
    wants_run_context = False

    def __init__(self) -> None:
        self._store: SpillStore | None = None

    def bind(self, store: SpillStore) -> None:
        self._store = store

    @property
    def store(self) -> SpillStore:
        if self._store is None:
            raise ToolError(f"{self.name} is not available in this context")
        return self._store


def _as_int(field: str, value: Any, default: int | None = None) -> int | None:
    """Numbers arrive as whatever the model emitted; name the field when it is junk."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{field} must be a number (got {value!r})") from exc


class ReadToolResultTool(_SpillTool):
    """Reads a byte range out of a spilled tool result."""

    name = READ_TOOL
    # Carried by one of the pair only: the registry concatenates the
    # guidelines of every active tool, and this advice covers both.
    guidelines = _GUIDELINES
    description = (
        "Read the full text of an earlier tool result that was too large to show "
        "inline, by the handle printed in its [hiveloom spill] marker. Returns a "
        "byte range and says how much remains, so a large result can be walked in "
        "order. Use search_tool_result first when you do not know where to look."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The handle from the [hiveloom spill] marker, e.g. tr_1a2b….",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Byte offset to start at (0 = the beginning).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": f"Bytes to return (default {DEFAULT_READ_BYTES}).",
            },
        },
        "required": ["handle"],
    }

    def run(self, handle: str = "", offset: Any = 0, limit: Any = None, **_: Any) -> str:
        try:
            return self.store.read(
                handle, offset=_as_int("offset", offset, 0), limit=_as_int("limit", limit)
            )
        except SpillError as exc:
            raise ToolError(str(exc)) from exc


class SearchToolResultTool(_SpillTool):
    """Finds a substring inside a spilled tool result."""

    name = SEARCH_TOOL
    description = (
        "Search an earlier tool result that was too large to show inline, by the "
        "handle printed in its [hiveloom spill] marker. Case-insensitive substring "
        "match; returns each hit with its byte offset and surrounding text, which "
        "read_tool_result then takes as an offset."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The handle from the [hiveloom spill] marker, e.g. tr_1a2b….",
            },
            "query": {
                "type": "string",
                "description": "Text to look for (case-insensitive substring).",
            },
        },
        "required": ["handle", "query"],
    }

    def run(self, handle: str = "", query: str = "", **_: Any) -> str:
        try:
            return self.store.search(handle, query)
        except SpillError as exc:
            raise ToolError(str(exc)) from exc


def spill_tools() -> list[Tool]:
    """The retrieval tools, unbound — one per registry."""
    return [ReadToolResultTool(), SearchToolResultTool()]
