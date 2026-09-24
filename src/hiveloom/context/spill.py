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
by an explicit size/hash-bound grant a fork carried over from its parent's
*verified, chained* journal (:meth:`SpillStore.inherit`). Inventing, guessing
or quoting a handle grants
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
names the directory is stopped by the sandbox; without one, the portable shell
policy refuses model-controlled arguments for file-reading commands. Files are
written 0600 in a 0700
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

import hashlib
import json
import os
import re
import secrets
import stat
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
#: The third of the set: reshape a stored object *without* paging it through
#: context. Registered and activated with the readers, for the same reason —
#: there is nothing to transform until something has been stored.
TRANSFORM_TOOL = "transform_result"
TOOL_NAMES = (READ_TOOL, SEARCH_TOOL, TRANSFORM_TOOL)

#: Retrieval results are never themselves spilled — a bounded read that spills
#: would hand back another handle, and the model would loop. ``transform_result``
#: is exempt for the opposite reason: it decides for itself whether its output
#: is inlined or stored as a derived object, and a second pass would re-store
#: what it just stored. ``notes`` too: a note read is already ranged.
EXEMPT_TOOLS = frozenset((*TOOL_NAMES, "notes"))

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

#: Ceilings on one ``transform_result`` call. Both ends are bounded: an op
#: never *examines* more than the scan ceiling (a stored object can be far
#: larger than memory) and never *produces* more than the output ceiling before
#: the inline-or-store decision is even reached.
TRANSFORM_MAX_SCAN_BYTES = 32 * 1024 * 1024
TRANSFORM_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
#: Lines returned by ``lines`` when the model names no count, and the most it
#: may name.
TRANSFORM_DEFAULT_LINES = 200
TRANSFORM_MAX_LINES = 10_000
#: Bytes returned by ``head``/``tail`` when the model names no size.
TRANSFORM_DEFAULT_BYTES = 4096
#: ``grep`` bounds: pattern length, matches reported, and context lines per
#: match. A pattern is applied to one line at a time, never to the whole
#: buffer, so the input to any single match attempt is bounded too.
TRANSFORM_MAX_PATTERN_CHARS = 256
TRANSFORM_MAX_MATCHES = 200
TRANSFORM_DEFAULT_MATCHES = 20
TRANSFORM_MAX_CONTEXT_LINES = 5
#: Objects one ``concat`` may join.
TRANSFORM_MAX_CONCAT = 8
#: A "line" is bounded as well: a stored object need not contain newlines at
#: all, and a regex applied to a 500 MB single line is not a bounded operation.
#: Anything longer is split at this bound (and the split counts as a line).
_LINE_MAX_BYTES = 1024 * 1024
#: Patterns that nest an unbounded quantifier inside a repeated group — the
#: classic catastrophic-backtracking shape. A heuristic, not a decision
#: procedure: `re` has no step limit, so the real bound is that a pattern only
#: ever meets one bounded line at a time.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+][^()]*\)\s*[*+{]")


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
    sha256: str


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


def _matches_manifest(path: Path, digest: str, expected_bytes: int) -> bool:
    """Verify a regular, non-symlink spill object against its authorization."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_bytes:
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == digest
    except OSError:
        return False


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
        self._authorized: dict[str, tuple[Path, str, int]] = {}
        # Called when ``transform_result`` mints a *derived* object. The store
        # cannot journal (it has no trace writer), and the loop cannot see
        # inside a tool call, so the one place that knows a new object exists
        # tells the one place that can record it — without which a fork would
        # not count the derived object as minted and could not carry it.
        self._on_derived: Callable[[SpillRecord, str, str], None] | None = None

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

    def spill(
        self,
        *,
        tool: str,
        content: str,
        derived_from: str | None = None,
        op: str | None = None,
    ) -> SpillRecord | None:
        """Persist ``content`` and return its preview, or None to keep it inline.

        None means either that the result fits the budget or that the write
        failed; the caller passes the original result through in both cases.

        ``derived_from``/``op`` record a :meth:`transform` output's provenance
        in the sidecar, so a derived object explains where it came from rather
        than appearing in the directory as an orphan of unknown origin.
        """
        if not self.exceeds_budget(content):
            return None
        stored = self._redact(content) if self._redact is not None else content
        data = stored.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
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
                        "sha256": digest,
                        "created_at": datetime.now(UTC).isoformat(),
                        **({"derived_from": derived_from} if derived_from else {}),
                        **({"op": op} if op else {}),
                    },
                    indent=2,
                ).encode("utf-8"),
            )
        except OSError:
            # Best effort by design: the caller keeps the full result inline.
            return None
        with self._lock:
            self._authorized[handle] = (body, digest, len(data))
        preview, omitted = self._preview(handle, data)
        return SpillRecord(
            handle=handle,
            preview=preview,
            total_bytes=len(data),
            omitted_bytes=omitted,
            path=body,
            sha256=digest,
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
    def inherit(self, manifests: Sequence[Any], source: str | Path) -> list[str]:
        """Authorize hash-bound objects a fork explicitly carried over.

        The list comes from the fork record, which ``hiveloom fork`` writes
        from the parent's *verified* journal — not from the seeded messages.
        A conversation is model-visible text: a resumed run whose transcript
        mentions ``tr_1234…`` must not thereby gain the authority to read it,
        or a model that once saw a handle could recover the object in any
        later fork by quoting it back.
        """
        directory = Path(source)
        granted: list[str] = []
        for manifest in manifests:
            if isinstance(manifest, str) and HANDLE_RE.fullmatch(manifest):
                # SDK callers may still pass the compact handle list. It is
                # accepted only when the copied sidecar supplies the same
                # hash-bound manifest; an old handle-only object stays denied.
                try:
                    candidate = json.loads(
                        (directory / f"{manifest}.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if candidate.get("handle") != manifest:
                    continue
                manifest = candidate
            if not isinstance(manifest, dict):
                continue
            handle = str(manifest.get("handle", ""))
            digest = str(manifest.get("sha256", ""))
            expected_bytes = manifest.get("bytes")
            if not HANDLE_RE.fullmatch(str(handle)):
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            if not isinstance(expected_bytes, int) or expected_bytes < 0:
                continue
            body = directory / f"{handle}.txt"
            if not _matches_manifest(body, digest, expected_bytes):
                continue
            with self._lock:
                self._authorized[handle] = (body, digest, expected_bytes)
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
            record = self._authorized.get(handle)
        if record is None:
            raise SpillError(
                f"unknown handle '{handle}'. A handle grants no access on its own: "
                "it is readable only in the run that produced it, or in a fork that "
                "explicitly inherited it."
            )
        path, digest, expected_bytes = record
        if not _matches_manifest(path, digest, expected_bytes):
            raise SpillError(
                f"the stored result for '{handle}' no longer matches its authorization"
            )
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


    # ------------------------------------------------------------------ #
    # Transforming
    # ------------------------------------------------------------------ #
    def set_on_derived(self, callback: Callable[[SpillRecord, str, str], None]) -> None:
        """Register the journal callback for derived objects (loop-owned)."""
        self._on_derived = callback

    def resolve_text(self, handle: str, max_bytes: int) -> str:
        """The whole object as text, for a handle-typed tool argument.

        Refuses above ``max_bytes`` rather than truncating: silently handing a
        tool half of what the model asked for would be worse than saying no.
        """
        path = self._resolve(handle)
        size = path.stat().st_size
        if size > max_bytes:
            # Only point at transform_result when this run has it: with
            # ``transforms`` off the registry never holds one, and advice to call
            # a tool the model cannot see is a dead end dressed as a way out.
            remedy = (
                f"Narrow it first with {TRANSFORM_TOOL}."
                if self._config.transforms
                else f"Pass a smaller part instead, read with {READ_TOOL}."
            )
            raise SpillError(
                f"the stored result for '{handle}' is {size} bytes; at most "
                f"{max_bytes} may be expanded into a tool argument. {remedy}"
            )
        return _decode(path.read_bytes())

    def transform(self, handle: str, op: str, args: dict[str, Any]) -> str:
        """Reshape a stored object without paging it through context.

        The output is returned inline when it fits ``max_inline_bytes``, and
        otherwise stored as a *derived* object under a new handle — so a
        narrowing that is still too large is one more transform away rather
        than a dead end.
        """
        operation = (op or "").strip().lower()
        runner = _TRANSFORM_OPS.get(operation)
        if runner is None:
            raise SpillError(
                f"unknown op '{op}'. Use one of: {', '.join(sorted(_TRANSFORM_OPS))}."
            )
        output = runner(self, handle, args)
        if not self.exceeds_budget(output):
            return output
        record = self.spill(
            tool=TRANSFORM_TOOL, content=output, derived_from=handle, op=operation
        )
        if record is None:  # pragma: no cover - best effort, same as any spill
            return output
        if self._on_derived is not None:
            with suppress(Exception):  # noqa: BLE001 - journalling never fails a result
                self._on_derived(record, handle, operation)
        return (
            f"[{TRANSFORM_TOOL}] {operation} of {handle} produced "
            f"{record.total_bytes} bytes, stored as {record.handle}.\n"
            f"{record.preview}"
        )


# --------------------------------------------------------------------- #
# Transform ops
#
# Every op is bounded at both ends: it examines at most
# ``TRANSFORM_MAX_SCAN_BYTES`` of the object and emits at most
# ``TRANSFORM_MAX_OUTPUT_BYTES``. None of them is Turing-complete, none takes
# a callable, and none can name a path — the only thing an op can reach is an
# object this run is already authorized to read.
# --------------------------------------------------------------------- #
def _arg_int(args: dict[str, Any], field: str, default: int, *, low: int, high: int) -> int:
    value = args.get(field)
    if value is None or value == "":
        value = default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SpillError(f"{field} must be a number (got {value!r})") from exc
    return max(low, min(number, high))


def _iter_lines(path: Path, max_bytes: int | None = None):
    """Yield ``(number, text)`` per line, streaming and bounded both ways.

    Chunked rather than ``for line in file``: a stored object need not contain
    a newline at all, and one readline on a 500 MB object is not a bounded
    read. A run longer than ``_LINE_MAX_BYTES`` is split at that bound, and
    each piece counts as a line — wherever the chunk boundaries fall.

    Lines end at ``\n`` only (a trailing ``\r`` is dropped), the same rule
    :func:`_split_lines` applies for the ops that read an object whole.

    At most ``max_bytes`` (default: the scan ceiling) are read. When that
    stops the scan short of the end, the line it cut is not yielded at all:
    half a line passed off as a whole one is a wrong answer, where a missing
    one is covered by the note :func:`_scan_note` adds.
    """
    limit = TRANSFORM_MAX_SCAN_BYTES if max_bytes is None else max_bytes
    line_max = _LINE_MAX_BYTES
    number = 0
    scanned = 0
    carry = b""
    exhausted = False
    with path.open("rb") as stream:
        while scanned < limit:
            block = stream.read(min(_SEARCH_CHUNK_BYTES, limit - scanned))
            if not block:
                exhausted = True
                break
            scanned += len(block)
            # ``carry`` never exceeds ``line_max`` here, so this copy is
            # bounded; the lines themselves are found by index, never by
            # re-splitting the remainder (which made a chunk quadratic).
            buffer = carry + block
            start = 0
            while True:
                end = buffer.find(b"\n", start)
                if end < 0:
                    break
                while end - start > line_max:
                    number += 1
                    yield number, _decode(buffer[start : start + line_max])
                    start += line_max
                number += 1
                yield number, _decode(buffer[start:end].rstrip(b"\r"))
                start = end + 1
            while len(buffer) - start > line_max:
                number += 1
                yield number, _decode(buffer[start : start + line_max])
                start += line_max
            carry = buffer[start:]
        if not exhausted and not stream.read(1):
            exhausted = True
    if carry and exhausted:
        yield number + 1, _decode(carry.rstrip(b"\r"))


def _scan_note(path: Path) -> str:
    """The partial-coverage marker for an object past the scan ceiling, or "".

    A streaming op over such an object answers for its first
    ``TRANSFORM_MAX_SCAN_BYTES`` only; saying so is what keeps "no match" or
    "N lines" from reading as a claim about the whole object.
    """
    size = path.stat().st_size
    if size <= TRANSFORM_MAX_SCAN_BYTES:
        return ""
    return (
        f"[{TRANSFORM_TOOL}] partial: scanned the first {TRANSFORM_MAX_SCAN_BYTES} "
        f"of {size} bytes; results cover only that prefix (a line cut by that "
        "bound is left out)."
    )


def _split_lines(text: str) -> list[str]:
    """Lines as :func:`_iter_lines` sees them: ``\n`` only, trailing ``\r`` dropped.

    Not ``str.splitlines``, which also breaks on form feeds, ``\x1c``-``\x1e``,
    U+2028 and more — so ``sort`` would count and number different lines than
    ``lines``/``grep``/``count`` for the same object.
    """
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [part[:-1] if part.endswith("\r") else part for part in parts]


def _cut_utf8(data: bytes, limit: int) -> bytes:
    """``data`` cut to at most ``limit`` bytes, never through a character."""
    if len(data) <= limit:
        return data
    end = max(0, limit)
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end]


def _bounded(lines: list[str]) -> str:
    """Join output lines, stopping at the output ceiling with a marker.

    An element that alone overruns what is left is cut rather than dropped:
    ``json_path`` emits its whole selection as one element and ``concat`` one
    per object, so dropping it would answer a narrowing with the marker and no
    data at all — a dead end, where a cut selection still flows on through the
    "too large, store it as a derived object" path.
    """
    kept: list[str] = []
    size = 0
    for index, line in enumerate(lines):
        encoded = line.encode("utf-8")
        width = len(encoded) + 1
        if size + width > TRANSFORM_MAX_OUTPUT_BYTES:
            # The first element is never preceded by a newline, so it has one
            # more byte of room than the ones after it.
            room = TRANSFORM_MAX_OUTPUT_BYTES - size - (0 if index == 0 else 1)
            head = _cut_utf8(encoded, room)
            if head:
                kept.append(_decode(head))
            kept.append(
                f"[{TRANSFORM_TOOL}] output stopped at {TRANSFORM_MAX_OUTPUT_BYTES} bytes."
            )
            break
        kept.append(line)
        size += width
    return "\n".join(kept)


def _read_whole(path: Path, handle: str, limit: int | None = None) -> bytes:
    """The whole object, refused above the scan ceiling rather than truncated.

    ``limit`` bounds what is brought into memory for a caller that already
    knows it cannot emit more than that. The refusal above the scan ceiling
    still applies first, so a bounded read is never a quiet half-answer to
    "read this object" — it is only the part of an accepted object the caller
    has budget for.
    """
    size = path.stat().st_size
    if size > TRANSFORM_MAX_SCAN_BYTES:
        raise SpillError(
            f"'{handle}' is {size} bytes; this op reads at most "
            f"{TRANSFORM_MAX_SCAN_BYTES}. Narrow it first with lines, head or grep."
        )
    if limit is None or size <= limit:
        return path.read_bytes()
    with path.open("rb") as stream:
        return stream.read(limit)


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a model-supplied regex under length and shape limits."""
    if not pattern:
        raise SpillError("this op needs a non-empty pattern")
    if len(pattern) > TRANSFORM_MAX_PATTERN_CHARS:
        raise SpillError(
            f"pattern is {len(pattern)} characters; at most "
            f"{TRANSFORM_MAX_PATTERN_CHARS} are accepted"
        )
    if _NESTED_QUANTIFIER.search(pattern):
        raise SpillError(
            "pattern repeats a group that already contains an unbounded "
            "quantifier, which can backtrack catastrophically. Rewrite it with "
            "a single quantifier."
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise SpillError(f"invalid regular expression: {exc}") from exc


def _op_lines(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    path = store._resolve(handle)
    start = _arg_int(args, "start", 1, low=1, high=2**31)
    count = _arg_int(args, "count", TRANSFORM_DEFAULT_LINES, low=1, high=TRANSFORM_MAX_LINES)
    end = start + count
    out = [f"[{handle}] lines {start}-{end - 1}"]
    note = _scan_note(path)
    if note:
        out.append(note)
    for number, text in _iter_lines(path):
        if number < start:
            continue
        if number >= end:
            break
        out.append(f"{number}: {text}")
    return _bounded(out)


def _op_head(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    path = store._resolve(handle)
    size = _arg_int(
        args, "bytes", TRANSFORM_DEFAULT_BYTES, low=1, high=TRANSFORM_MAX_OUTPUT_BYTES
    )
    total = path.stat().st_size
    with path.open("rb") as stream:
        chunk = stream.read(size)
    return f"[{handle}] bytes 0-{len(chunk)} of {total}\n{_decode(chunk)}"


def _op_tail(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    path = store._resolve(handle)
    size = _arg_int(
        args, "bytes", TRANSFORM_DEFAULT_BYTES, low=1, high=TRANSFORM_MAX_OUTPUT_BYTES
    )
    total = path.stat().st_size
    start = max(0, total - size)
    with path.open("rb") as stream:
        stream.seek(start)
        chunk = stream.read(size)
    return f"[{handle}] bytes {start}-{total} of {total}\n{_decode(chunk)}"


def _op_grep(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    path = store._resolve(handle)
    expression = _compile(str(args.get("pattern") or ""))
    # `raw` makes the result the data itself — bare lines, no header, no line
    # numbers — so a derived handle to it can be passed on (to `file_write`,
    # say) as exactly the matching lines. Data is only useful complete, so a
    # raw grep takes up to TRANSFORM_MAX_LINES matches by default and refuses,
    # rather than truncates, when it cannot return all of them.
    raw = args.get("raw") is True
    limit = _arg_int(
        args,
        "max_matches",
        TRANSFORM_MAX_LINES if raw else TRANSFORM_DEFAULT_MATCHES,
        low=1,
        high=TRANSFORM_MAX_LINES if raw else TRANSFORM_MAX_MATCHES,
    )
    around = _arg_int(args, "context_lines", 0, low=0, high=TRANSFORM_MAX_CONTEXT_LINES)
    out: list[str] = []
    matched = 0
    more = False
    before: list[tuple[int, str]] = []
    after = 0

    def mark(number: int, text: str, sep: str) -> str:
        return text if raw else f"{number}{sep} {text}"

    for number, text in _iter_lines(path):
        # The pattern meets one bounded line at a time, never the whole
        # buffer: a match attempt's input is bounded even when the object is
        # hundreds of megabytes.
        hit = expression.search(text) is not None
        if matched >= limit:
            # Past the limit the scan goes on only to learn whether "stopped"
            # is true: a further match must exist before it is claimed.
            more = more or hit
            if after:
                out.append(mark(number, text, "-"))
                after -= 1
            if more and not after:
                break
            continue
        if hit:
            matched += 1
            for earlier_number, earlier in before:
                out.append(mark(earlier_number, earlier, "-"))
            before = []
            out.append(mark(number, text, ":"))
            after = around
        elif after:
            out.append(mark(number, text, "-"))
            after -= 1
        elif around:
            before.append((number, text))
            before = before[-around:]
    note = _scan_note(path)
    if raw and out:
        return _raw_lines(out, handle, more=more, limit=limit, partial=bool(note))
    if more:
        out.append(f"[{TRANSFORM_TOOL}] stopped at {limit} matches; more lines match.")
    if not out:
        return f"[{handle}] no line matched {args.get('pattern')!r}." + (
            f"\n{note}" if note else ""
        )
    header = [f"[{handle}] {matched} matching line(s)", *([note] if note else [])]
    return _bounded([*header, *out])


def _raw_lines(out: list[str], handle: str, *, more: bool, limit: int, partial: bool) -> str:
    """A raw grep's result: every matching line, or a refusal naming why not."""
    if more:
        raise SpillError(
            f"raw grep on '{handle}' matched more than {limit} lines; raise "
            f"max_matches (up to {TRANSFORM_MAX_LINES}) or narrow the pattern"
        )
    if partial:
        raise SpillError(
            f"'{handle}' is larger than the {TRANSFORM_MAX_SCAN_BYTES}-byte scan "
            "ceiling, so a raw grep of it would be incomplete"
        )
    text = "\n".join(out)
    if len(text.encode("utf-8")) > TRANSFORM_MAX_OUTPUT_BYTES:
        raise SpillError(
            f"raw grep on '{handle}' would return more than "
            f"{TRANSFORM_MAX_OUTPUT_BYTES} bytes; narrow the pattern"
        )
    return text


def _op_json_path(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    from hiveloom.json_path import extract_json_path

    path = str(args.get("path") or "")
    if not path:
        raise SpillError('json_path needs a "path" such as $.items[*].id')
    raw = _read_whole(store._resolve(handle), handle)
    try:
        document = json.loads(_decode(raw))
    except json.JSONDecodeError as exc:
        raise SpillError(f"'{handle}' is not valid JSON: {exc}") from exc
    try:
        selected = extract_json_path(document, path)
    except ValueError as exc:
        raise SpillError(str(exc)) from exc
    if not selected:
        return f"[{handle}] {path} selected nothing."
    return _bounded([json.dumps(selected, indent=2, ensure_ascii=False)])


def _op_count(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    path = store._resolve(handle)
    pattern = str(args.get("pattern") or "")
    expression = _compile(pattern) if pattern else None
    lines = 0
    matches = 0
    for _number, text in _iter_lines(path):
        lines += 1
        if expression is not None and expression.search(text):
            matches += 1
    report = f"[{handle}] {lines} lines, {path.stat().st_size} bytes"
    if expression is not None:
        report += f", {matches} lines matching {pattern!r}"
    note = _scan_note(path)
    return f"{report}\n{note}" if note else report


def _op_sort(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    raw = _read_whole(store._resolve(handle), handle)
    lines = _split_lines(_decode(raw))
    unique = bool(args.get("unique"))
    ordered = sorted(set(lines)) if unique else sorted(lines)
    return _bounded([f"[{handle}] {len(ordered)} line(s) sorted", *ordered])


def _op_unique(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    raw = _read_whole(store._resolve(handle), handle)
    seen: set[str] = set()
    kept: list[str] = []
    for line in _split_lines(_decode(raw)):
        if line not in seen:
            seen.add(line)
            kept.append(line)
    return _bounded([f"[{handle}] {len(kept)} distinct line(s), first seen first", *kept])


def _op_concat(store: SpillStore, handle: str, args: dict[str, Any]) -> str:
    extra = args.get("handles") or []
    if isinstance(extra, str):
        extra = [extra]
    if not isinstance(extra, list):
        raise SpillError("concat needs a list of handles")
    ordered = [handle, *[str(item) for item in extra]]
    if len(ordered) > TRANSFORM_MAX_CONCAT:
        raise SpillError(
            f"concat joins at most {TRANSFORM_MAX_CONCAT} objects (got {len(ordered)})"
        )
    parts: list[str] = []
    # A running budget across the objects, not per object: eight reads of up
    # to the scan ceiling would be hundreds of megabytes resident to produce
    # output `_bounded` then cuts to a few. One byte past the ceiling is read
    # so that the marker is earned rather than guessed.
    budget = TRANSFORM_MAX_OUTPUT_BYTES + 1
    for item in ordered:
        # Each one resolves under this run's authority; concat is not a way to
        # reach an object the model could not already read.
        raw = _read_whole(store._resolve(item), item, limit=budget)
        parts.append(f"[{item}]")
        parts.append(_decode(raw))
        budget -= len(raw)
        if budget <= 0:
            break
    return _bounded(parts)


#: Declaration order is the order the tool lists them to the model.
_TRANSFORM_OPS: dict[str, Callable[[SpillStore, str, dict[str, Any]], str]] = {
    "lines": _op_lines,
    "head": _op_head,
    "tail": _op_tail,
    "grep": _op_grep,
    "json_path": _op_json_path,
    "count": _op_count,
    "sort": _op_sort,
    "unique": _op_unique,
    "concat": _op_concat,
}


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


class TransformResultTool(_SpillTool):
    """Reshapes a stored result in place, inside the run's storage.

    Reading a 40 MB log back 4 KB at a time to find twelve lines spends the
    whole context budget on the 39.9 MB that did not matter. Every op here
    runs against the *stored bytes* and returns only what it produced — and
    when even that is too large to inline, it becomes a new stored object with
    its own handle, so narrowing can be repeated rather than abandoned.

    Deliberately not a scripting surface: a fixed set of ops, each bounded in
    what it may scan and what it may emit, none able to name a path or reach
    an object this run was not already authorized to read.
    """

    name = TRANSFORM_TOOL
    description = (
        "Reshape an earlier tool result that was too large to show inline, by the "
        "handle printed in its [hiveloom spill] marker — without reading it into "
        "context first. Ops: lines (start, count), head/tail (bytes), grep "
        "(pattern, max_matches, context_lines), json_path (path), count "
        "(optional pattern), sort (unique), unique, concat (handles). The result "
        "comes back inline when it fits, otherwise as a new handle you can "
        "transform again."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The handle from the [hiveloom spill] marker, e.g. tr_1a2b….",
            },
            "op": {
                "type": "string",
                "enum": list(_TRANSFORM_OPS),
                "description": "The transformation to apply.",
            },
            "start": {
                "type": "integer",
                "minimum": 1,
                "description": "lines: first line number (1 = the first line).",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "description": f"lines: how many lines (default {TRANSFORM_DEFAULT_LINES}, "
                f"max {TRANSFORM_MAX_LINES}).",
            },
            "bytes": {
                "type": "integer",
                "minimum": 1,
                "description": f"head/tail: how many bytes (default {TRANSFORM_DEFAULT_BYTES}).",
            },
            "pattern": {
                "type": "string",
                "description": "grep/count: a regular expression, applied to one line at "
                f"a time (at most {TRANSFORM_MAX_PATTERN_CHARS} characters).",
            },
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "description": f"grep: matching lines to return (max {TRANSFORM_MAX_MATCHES}).",
            },
            "raw": {
                "type": "boolean",
                "description": "grep: return only the matching lines themselves — no "
                "header, no line numbers — so the result (or its handle) is the data. "
                f"Takes up to {TRANSFORM_MAX_LINES} matches and fails rather than "
                "return a partial set.",
            },
            "context_lines": {
                "type": "integer",
                "minimum": 0,
                "description": f"grep: lines of context around each match (max "
                f"{TRANSFORM_MAX_CONTEXT_LINES}).",
            },
            "path": {
                "type": "string",
                "description": "json_path: a path such as $.items[*].id.",
            },
            "unique": {
                "type": "boolean",
                "description": "sort: drop duplicate lines.",
            },
            "handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"concat: further handles to join after this one "
                f"(at most {TRANSFORM_MAX_CONCAT} objects in total).",
            },
        },
        "required": ["handle", "op"],
    }

    def run(self, handle: str = "", op: str = "", **args: Any) -> str:
        try:
            return self.store.transform(handle, op, args)
        except SpillError as exc:
            raise ToolError(str(exc)) from exc


def spill_tools(*, transforms: bool = True) -> list[Tool]:
    """The retrieval tools, unbound — one per registry.

    ``transforms`` follows ``context.tool_results.transforms``: off, the
    registry never holds a ``transform_result`` at all, so the loop's
    activation-by-name has nothing to switch on and the model never sees it.
    """
    tools: list[Tool] = [ReadToolResultTool(), SearchToolResultTool()]
    if transforms:
        tools.append(TransformResultTool())
    return tools
