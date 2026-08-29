#!/usr/bin/env python3
"""The hiveloom workbench backend: one API across every harness you work on.

This ships as ``hiveloom-workbench``, a separate opt-in distribution. The
``hiveloom`` wheel deliberately does not carry it: the framework is what runs in
production and on a server, while the workbench is an inspector you install on
the machine where you are actually building harnesses. Keeping them apart is
what lets the framework stay small and dependency-light for everyone who never
opens a UI.

Why this exists rather than ``hiveloom serve``: that server is deliberately
*one* harness's deployment front door, scoped to a single directory and hardened
for a remote caller. The workbench is the opposite shape — a local tool spanning
the whole registry, where creating a harness, editing its spec, running it, and
reading its history are one continuous activity. So this composes hiveloom's own
public modules (``registry``, ``construct``, ``runner``, ``catalog``, ``Hive``,
``evolve``) rather than wrapping the HTTP server, and reimplements none of them.

Safety is not relaxed for being local. The trust gate still applies: a harness
runs only after it has been trusted, and the API answers an untrusted one with a
``trust_required`` error the UI turns into an explicit button. Running arbitrary
code from a directory you have not vouched for is exactly what that gate is for,
and a UI is not a reason to skip it.

Installed, it serves its own compiled frontend and needs no Node toolchain::

    hiveloom-workbench --port 8770

From a checkout, ``devtools/ui/dev.sh`` runs this alongside Vite instead, so
both halves reload.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import threading
import traceback
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from hiveloom import catalog as catalog_mod
from hiveloom import construct
from hiveloom import paths as paths_mod
from hiveloom import registry as registry_mod
from hiveloom import runner as runner_mod
from hiveloom import trust as trust_mod
from hiveloom.errors import HiveloomError
from hiveloom.evolve import analyzer as evolve_mod
from hiveloom.evolve import proposals as proposals_mod
from hiveloom.fork import (
    create_fork,
    fork_points,
    fork_target,
    harness_root,
    load_fork,
    load_fork_context,
    parent_version_hash,
)
from hiveloom.generate.llm import build_strong_model
from hiveloom.logging.hive import Hive
from hiveloom.logging.journal import read_events, state_at, verify_chain
from hiveloom.logging.trace import payload_hash, spec_version_hash
from hiveloom.loop.control import RunControl
from hiveloom.package import trace_dir_relative_to
from hiveloom.spec.loader import atomic_write_text, harness_path, load_spec, validate_harness
from hiveloom.tools.builtin import safe_path
from hiveloom.tools.registry import ToolError

_STREAM_DONE = object()

# Where an attachment lands inside the harness it was dropped on. A real
# directory rather than an in-memory blob, because a harness reads files with
# `file_read` — the tool it already has — and a path in the message is
# something the journal records and a later reader can still resolve.
_UPLOAD_SUBDIR = "uploads"

# Workbench metadata about a harness's versions, kept beside its traces. In
# `.hiveloom/` deliberately: `safe_path` refuses that directory, so a version
# label is something a person writes and a running harness cannot read or
# rewrite.
_TAGS_FILE = Path(".hiveloom") / "version_tags.json"

# An attachment is context for one turn, not a data transfer: past a megabyte
# or so it belongs in the workspace already, referenced by path.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# An attachment may be larger because a target harness can consume it itself,
# but the copilot's inspection tool should not pour a multi-megabyte file into
# one model turn. Larger files remain attached and can still be exercised by
# the target harness.
_MAX_COPILOT_FILE_BYTES = 256 * 1024

# The distribution's version, read from here by its build backend so there is
# one place to bump. Independent of ``hiveloom``'s: the workbench is released on
# its own cadence and only ever depends on the framework's public API.
__version__ = "0.1.0"

_PACKAGE_DIR = Path(__file__).resolve().parent


def _is_source_checkout() -> bool:
    """True when this module runs from ``devtools/ui`` rather than an install.

    ``vite.config.ts`` is the frontend's *build* configuration: it is needed to
    compile the UI and is deliberately excluded from the published package,
    which makes its presence the one unambiguous signal that this is a working
    copy. Note it cannot be ``package.json`` -- the npm package ships one, so
    every install would claim to be a checkout.

    The distinction decides where the workbench may write. A checkout keeps its
    state beside the source, where a developer expects to find and delete it; an
    installed copy lives under ``node_modules`` (or ``site-packages``), which is
    disposable, shared between projects, and must never be written to.
    """
    return (_PACKAGE_DIR / "vite.config.ts").is_file()


def _workbench_home() -> Path:
    """The directory holding state the workbench itself owns.

    Conversations, memory, and -- when installed -- the copilot's own working
    copy. Resolved on each call rather than at import so ``HIVELOOM_HOME`` still
    relocates it, which is what keeps tests and CI off the real one.
    """
    if _is_source_checkout():
        return _PACKAGE_DIR / ".hiveloom"
    return paths_mod.hiveloom_home() / "workbench"


def _copilot_dir() -> Path:
    """The bundled copilot harness, in a directory it is allowed to write to.

    A checkout uses the source directory directly, so editing the copilot's
    prompt or its tools takes effect on the next start. An installed copilot is
    materialized under the workbench home instead: running it journals into its
    own ``.hiveloom/traces``, which it cannot do from inside ``site-packages``.
    """
    bundled = _PACKAGE_DIR / "copilot"
    if _is_source_checkout():
        return bundled
    working = _workbench_home() / "copilot"
    _sync_bundled_copilot(bundled, working)
    return working


def _sync_bundled_copilot(bundled: Path, working: Path) -> None:
    """Refresh the working copilot from the installed one, keeping local state.

    Copy-if-different rather than replace-the-directory: an upgrade has to pick
    up a new system prompt or tool, but the working copy also accumulates things
    the distribution has no business deleting -- the journals under
    ``.hiveloom``, and a ``.env`` holding the key the copilot runs on. Only
    files the distribution actually ships are ever written.
    """
    for source in sorted(bundled.rglob("*")):
        if source.is_dir() or "__pycache__" in source.parts:
            continue
        target = working / source.relative_to(bundled)
        payload = source.read_bytes()
        if target.exists() and target.read_bytes() == payload:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _web_root() -> Path | None:
    """The built single-page app, when there is one on disk to serve.

    An installed workbench carries the compiled bundle as ``web/`` and serves it
    itself, so it is one process and one URL with no Node toolchain present. A
    checkout normally lets Vite own the browser origin and proxy ``/api`` here,
    but ``npm run build`` writes to that same ``web/`` and is then served the
    same way -- which is how the packaged behavior gets exercised before it is
    packaged.
    """
    root = _PACKAGE_DIR / "web"
    return root if (root / "index.html").is_file() else None


# What a browser gets when the API is up but no bundle was built. The two ways
# out differ by install, so both are named rather than guessed between.
_NO_WEB_BUILD = """The hiveloom workbench API is running, but no built interface was found.

From a checkout, run the development servers instead:

    devtools/ui/dev.sh

From an installed workbench, the bundle is missing from the distribution:

    pip install --force-reinstall hiveloom-workbench
"""


# The workbench's own expert. It is intentionally outside the scanned target
# tree: copilot reasoning is not a target harness and must never appear in the
# target rail or contaminate target fitness.

_MAX_CONVERSATION_BYTES = 4 * 1024 * 1024
_MAX_CONVERSATION_MESSAGES = 200
_MAX_MEMORY_CONTENT_BYTES = 4 * 1024
_MAX_MEMORIES = 500


# --------------------------------------------------------------------- #
# In-flight runs
# --------------------------------------------------------------------- #
class _LiveRuns:
    """The RunControl of every run currently executing in this process.

    A run is addressable the moment it starts, not when it finishes: the id is
    pre-allocated and announced on the stream's first frame, so the UI can wire
    up stop / steer / model / playbook buttons while the first model call is
    still in flight. Entries are removed when the run completes, which is what
    makes a control call on a finished run a clean 404 rather than a silent
    no-op.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._controls: dict[str, RunControl] = {}

    def register(self, run_id: str, control: RunControl) -> None:
        with self._lock:
            self._controls[run_id] = control

    def release(self, run_id: str) -> None:
        with self._lock:
            self._controls.pop(run_id, None)

    def get(self, run_id: str) -> RunControl | None:
        with self._lock:
            return self._controls.get(run_id)

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._controls)


_LIVE = _LiveRuns()


# --------------------------------------------------------------------- #
# Persisted workbench conversations
# --------------------------------------------------------------------- #
class _ConversationStore:
    """SQLite conversation journal, separate from harness run evidence.

    A copilot turn remains an ordinary Hiveloom run. This store only remembers
    the human-facing thread that groups those runs, plus the harness/run context
    the person had attached. The database lives in the developer UI folder so
    browser refreshes and API restarts see the same workbench.
    """

    def __init__(self, database: Path, legacy_directory: Path | None = None) -> None:
        self._database = database
        self._lock = threading.RLock()
        self._initialize()
        if legacy_directory is not None:
            self._migrate_legacy(legacy_directory)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _valid_id(conversation_id: str) -> bool:
        return bool(re.fullmatch(r"chat_[a-f0-9]{32}", conversation_id))

    def _check_id(self, conversation_id: str) -> None:
        if not self._valid_id(conversation_id):
            raise LookupError(f"unknown conversation {conversation_id!r}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as database, database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, selection_json TEXT NOT NULL, "
                "messages_json TEXT NOT NULL)"
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS conversations_updated "
                "ON conversations(updated_at DESC)"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "selection": json.loads(row["selection_json"]),
            "messages": json.loads(row["messages_json"]),
        }

    def _read(self, conversation_id: str) -> dict[str, Any]:
        self._check_id(conversation_id)
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown conversation {conversation_id!r}")
        return self._record(row)

    def _insert(self, record: dict[str, Any]) -> None:
        with closing(self._connect()) as database, database:
            database.execute(
                "INSERT OR IGNORE INTO conversations "
                "(id, title, created_at, updated_at, selection_json, messages_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    record["title"],
                    record["created_at"],
                    record["updated_at"],
                    json.dumps(record.get("selection") or {}, ensure_ascii=False),
                    json.dumps(record.get("messages") or [], ensure_ascii=False),
                ),
            )

    def _migrate_legacy(self, directory: Path) -> None:
        """Import the short-lived JSON format once, without deleting its files."""
        if not directory.is_dir():
            return
        for path in directory.glob("chat_*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(record, dict) and self._valid_id(str(record.get("id"))):
                    self._insert(record)
            except (OSError, json.JSONDecodeError, KeyError):
                continue

    def create(self) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            record = {
                "id": f"chat_{uuid.uuid4().hex}",
                "title": "New conversation",
                "created_at": now,
                "updated_at": now,
                "selection": {},
                "messages": [],
            }
            self._insert(record)
            return record

    def get(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            return self._read(conversation_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            with closing(self._connect()) as database:
                stored = database.execute(
                    "SELECT * FROM conversations ORDER BY updated_at DESC"
                ).fetchall()
            for row in stored:
                record = self._record(row)
                messages = record["messages"]
                preview = next(
                    (
                        str(message.get("content") or "")
                        for message in reversed(messages)
                        if isinstance(message, dict) and message.get("content")
                    ),
                    "",
                )
                rows.append(
                    {
                        "id": record["id"],
                        "title": str(record.get("title") or "New conversation"),
                        "created_at": str(record.get("created_at") or ""),
                        "updated_at": str(record.get("updated_at") or ""),
                        "selection": record.get("selection") or {},
                        "message_count": len(messages),
                        "preview": preview[:160],
                    }
                )
            return rows

    def save(self, conversation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(body) - {"messages", "selection", "title"})
        if unknown:
            raise ValueError(f"unknown fields: {unknown}")
        with self._lock:
            current = self._read(conversation_id)
            messages = body.get("messages", current.get("messages") or [])
            selection = body.get("selection", current.get("selection") or {})
            title = str(body.get("title", current.get("title") or "New conversation")).strip()
            if not isinstance(messages, list):
                raise ValueError("'messages' must be an array")
            if len(messages) > _MAX_CONVERSATION_MESSAGES:
                raise ValueError(
                    f"a conversation may contain at most {_MAX_CONVERSATION_MESSAGES} messages"
                )
            if not isinstance(selection, dict):
                raise ValueError("'selection' must be an object")
            clean_selection = {
                key: str(selection[key])
                for key in ("harness_id", "run_id")
                if selection.get(key)
            }
            if not title or title == "New conversation":
                title = next(
                    (
                        str(message.get("content") or "").strip()
                        for message in messages
                        if isinstance(message, dict)
                        and message.get("role") == "user"
                        and str(message.get("content") or "").strip()
                    ),
                    "New conversation",
                )[:80]
            record = {
                **current,
                "title": title[:80],
                "updated_at": self._now(),
                "selection": clean_selection,
                "messages": messages,
            }
            encoded = json.dumps(record, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > _MAX_CONVERSATION_BYTES:
                raise ValueError(
                    f"conversation exceeds the {_MAX_CONVERSATION_BYTES} byte storage ceiling"
                )
            with closing(self._connect()) as database, database:
                database.execute(
                    "UPDATE conversations SET title=?, updated_at=?, selection_json=?, "
                    "messages_json=? WHERE id=?",
                    (
                        record["title"],
                        record["updated_at"],
                        json.dumps(clean_selection, ensure_ascii=False),
                        json.dumps(messages, ensure_ascii=False),
                        conversation_id,
                    ),
                )
            return record

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            self._check_id(conversation_id)
            with closing(self._connect()) as database, database:
                cursor = database.execute(
                    "DELETE FROM conversations WHERE id=?", (conversation_id,)
                )
            if cursor.rowcount == 0:
                raise LookupError(f"unknown conversation {conversation_id!r}")


class _MemoryStore:
    """Small, inspectable cross-conversation memory in the workbench DB."""

    def __init__(self, database: Path) -> None:
        self._database = database
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as database, database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                "id TEXT PRIMARY KEY, harness_id TEXT NOT NULL, content TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS memories_scope_updated "
                "ON memories(harness_id, updated_at DESC)"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "scope": "harness" if row["harness_id"] else "global",
            "harness_id": row["harness_id"] or None,
            "content": row["content"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(
        self, *, harness_id: str = "", query: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as database:
            params: list[Any] = []
            if harness_id:
                where = "(harness_id='' OR harness_id=?)"
                params.append(harness_id)
            else:
                where = "harness_id=''"
            if query.strip():
                where += " AND lower(content) LIKE ?"
                params.append(f"%{query.strip().lower()}%")
            params.append(max(1, min(int(limit), 100)))
            rows = database.execute(
                f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ?",  # noqa: S608 - fixed clauses only
                params,
            ).fetchall()
        return [self._record(row) for row in rows]

    def remember(self, content: str, *, harness_id: str = "") -> dict[str, Any]:
        clean = content.strip()
        if not clean:
            raise ValueError("memory content must not be empty")
        if len(clean.encode("utf-8")) > _MAX_MEMORY_CONTENT_BYTES:
            raise ValueError(
                f"memory exceeds the {_MAX_MEMORY_CONTENT_BYTES} byte ceiling"
            )
        with self._lock, closing(self._connect()) as database, database:
            existing = database.execute(
                "SELECT * FROM memories WHERE harness_id=? AND content=?",
                (harness_id, clean),
            ).fetchone()
            now = datetime.now(UTC).isoformat()
            if existing is not None:
                database.execute(
                    "UPDATE memories SET updated_at=? WHERE id=?",
                    (now, existing["id"]),
                )
                row = database.execute(
                    "SELECT * FROM memories WHERE id=?", (existing["id"],)
                ).fetchone()
                return self._record(row)
            count = database.execute("SELECT count(*) FROM memories").fetchone()[0]
            if count >= _MAX_MEMORIES:
                raise ValueError(
                    f"workbench memory is full ({_MAX_MEMORIES} entries); forget one first"
                )
            memory_id = f"mem_{uuid.uuid4().hex}"
            database.execute(
                "INSERT INTO memories "
                "(id, harness_id, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (memory_id, harness_id, clean, now, now),
            )
            row = database.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
        return self._record(row)

    def forget(self, memory_id: str) -> None:
        if not re.fullmatch(r"mem_[a-f0-9]{32}", memory_id):
            raise LookupError(f"unknown memory {memory_id!r}")
        with self._lock, closing(self._connect()) as database, database:
            cursor = database.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if cursor.rowcount == 0:
            raise LookupError(f"unknown memory {memory_id!r}")


# --------------------------------------------------------------------- #
# Harness identity
# --------------------------------------------------------------------- #
# A harness is addressed by a slug of its spec name, not by its path: paths are
# long, ugly in a URL, and change when a checkout moves, while the name is what
# the user sees everywhere else in hiveloom (stats, traces, MCP tool names).
def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return cleaned or "harness"


def _scan_harnesses(scan_dirs: list[str]) -> list[str]:
    """Find harness roots below workbench scan directories, deepest included."""
    found: set[str] = set()
    for value in scan_dirs:
        root = Path(value).resolve()
        if (root / "harness.yaml").is_file():
            found.add(str(root))
        if not root.is_dir():
            continue
        try:
            manifests = root.rglob("harness.yaml")
            for manifest in manifests:
                if manifest.is_file():
                    found.add(str(manifest.parent.resolve()))
        except OSError:
            # An unreadable subtree does not hide the harnesses already found;
            # a broken harness is represented separately by `_catalog.read`.
            continue
    return sorted(found)


def _catalog(
    extra_dirs: list[str], scan_dirs: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Every harness the UI offers: registry, scans, and explicit ``--dir`` ones.

    Re-read per request. The registry is a small local YAML file, and the
    alternative — caching — means creating a harness in the UI does not show it
    until a restart, which is the opposite of what a workbench should do.

    Ids come from the harness name, except where a name is not unique: a fork
    copies its parent's name, so two folders sharing one is the normal case.
    Those rows fall back to their folder, which makes the id stable no matter
    what order the registry happens to list them in — and keeps the parent's
    plain id when its folder is named after it, as it usually is.
    """
    rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def read(path: str, *, explicit: bool) -> None:
        folder = Path(path)
        resolved = str(folder.resolve())
        if resolved in seen_paths:
            return
        seen_paths.add(resolved)
        try:
            spec = load_spec(harness_path(path))
        except Exception as exc:  # noqa: BLE001 - a broken harness is a row, not a 500
            rows.append(
                {
                    "path": resolved,
                    "folder": folder.name,
                    "name": folder.name,
                    "description": "",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "explicit": explicit,
                    # A fork whose spec no longer loads is still that harness's
                    # fork; containment is a fact about the path, so it survives
                    # the spec being broken and the row still nests correctly.
                    "root_path": str(harness_root(folder)),
                }
            )
            return
        rows.append(
            {
                "path": resolved,
                "folder": folder.name,
                "name": spec.name,
                # The Hive key: the spec's stable `id`, or the name for a
                # pre-identity spec. Stats/compare must query by this, never
                # by display name, or same-named harnesses share evidence.
                "key": spec.identity,
                "description": spec.description,
                "ok": True,
                "error": "",
                "explicit": explicit,
                # A fork knows where it came from; the folder name does not.
                # Without this the UI can only show a slug someone typed once.
                "fork": load_fork(folder),
                # Containment, which is a fact about the folder rather than
                # about the name inside it. A fork lives under its harness's
                # `.hiveloom/forks`, so the rail can nest it there even when
                # its spec has been renamed — where grouping by name would
                # scatter it, and grouping two same-named unrelated harnesses
                # would merge them.
                "root_path": str(harness_root(folder)),
                # The version this folder is at, so a picker can offer versions
                # rather than paths — two folders at one version are one
                # version, which is exactly what a fork of an unedited harness
                # is.
                "version_hash": spec_version_hash(spec, folder),
            }
        )

    for entry in registry_mod.registered():
        read(entry.path, explicit=False)
    for directory in _scan_harnesses(scan_dirs or []):
        read(directory, explicit=True)
    for directory in extra_dirs:
        read(directory, explicit=True)

    shared = {
        name
        for name in {_slug(row["name"]) for row in rows}
        if sum(1 for row in rows if _slug(row["name"]) == name) > 1
    }

    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _slug(row["name"])
        if key in shared:
            key = _slug(row["folder"])
        while key in found:
            key = f"{key}-2"
        found[key] = {"id": key, **row}

    # Ids are only known once every row has one, so the fork -> parent link is
    # a second pass. `parent_id` is empty for a trunk, and for a fork whose
    # harness is not in the catalog — a fork can be registered on its own, and
    # a row that claims a parent nothing lists would be a dead link.
    by_path = {entry["path"]: entry["id"] for entry in found.values()}
    for entry in found.values():
        root = entry.get("root_path", entry["path"])
        entry["is_fork"] = root != entry["path"]
        entry["parent_id"] = by_path.get(root, "") if entry["is_fork"] else ""
    return found


def _resolve(
    extra_dirs: list[str], harness_id: str, scan_dirs: list[str] | None = None
) -> dict[str, Any]:
    entry = _catalog(extra_dirs, scan_dirs).get(harness_id)
    if entry is None:
        raise LookupError(f"unknown harness {harness_id!r}")
    return entry


# --------------------------------------------------------------------- #
# Version labels
# --------------------------------------------------------------------- #
# A version hash is what the harness *is*; a label is what you call it while
# you are working. Kept per harness folder rather than per browser, because
# "baseline" is a fact about the harness that a second machine — or the same
# machine after a cleared cache — should still see.
def _read_tags(harness_dir: str) -> dict[str, str]:
    path = Path(harness_dir) / _TAGS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}


def _write_tags(harness_dir: str, tags: dict[str, str]) -> None:
    path = Path(harness_dir) / _TAGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tags, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _upload_name(name: str) -> str:
    """A filename from a browser, reduced to one path segment we chose.

    The name a person's file happens to have is untrusted input: it can carry
    separators, `..`, or nothing at all. Taking only the basename and reducing
    it to a known alphabet means the path that reaches `safe_path` is already
    a single segment, and `safe_path` still has the final say on containment.
    """
    stem = Path(name.strip()).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    return cleaned or "attachment"


# --------------------------------------------------------------------- #
# The workbench's own credentials
# --------------------------------------------------------------------- #
# `~/.hiveloom/.env` — one place to put the keys you develop with, so every
# harness in the rail can be run without each one carrying a copy. It is the
# workbench's configuration, not a harness's: a harness's own `.env` travels
# with the harness and is part of what it is, while this is part of the machine
# you are working on.
#
# Loaded into the process environment rather than consulted at call time,
# because a run happens *inside* this process and hiveloom's provider factories
# read `os.environ`. Never overriding: a name already set when the API started
# was set deliberately by whoever started it, and that has to win.
#
# Precedence a run therefore sees:  process environment  >  this file  >  a
# harness's own `.env` (hiveloom loads that one with override=False too, so it
# fills what is still missing rather than replacing what is set).


def _load_workbench_env(adopted: set[str]) -> set[str]:
    """Adopt `~/.hiveloom/.env` into the environment; return the names taken.

    Re-read on demand rather than only at startup, so adding a key is "write
    the file, reload the page" instead of "restart the API". Values are never
    logged, returned, or echoed — only the names, so the UI can say where a key
    came from.

    The set belongs to the app rather than the module: two apps in one process
    (the tests build several) would otherwise inherit each other's answers
    about where a key came from.
    """
    path = paths_mod.hiveloom_home() / ".env"
    if not path.exists():
        return adopted
    try:
        from dotenv import dotenv_values

        for name, value in dotenv_values(path).items():
            if not name or not (value or "").strip():
                continue
            if name in os.environ:
                continue
            os.environ[name] = value
            adopted.add(name)
    except Exception:  # noqa: BLE001 - an unreadable file is not a dead workbench
        pass
    return adopted


# --------------------------------------------------------------------- #
# Error shape
# --------------------------------------------------------------------- #
def _error(message: str, status: int, *, code: str = "error", **extra: Any) -> JSONResponse:
    """One error envelope, so the client has a single thing to render.

    ``code`` is what the UI branches on — notably ``trust_required``, which is
    an actionable state with a button rather than a failure.
    """
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status)


def _guarded(handler):
    """Map hiveloom's exceptions to the error envelope, once, for every route."""

    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except LookupError as exc:
            return _error(str(exc), 404, code="not_found")
        # There is no dedicated trust exception — the gate raises SpecError like
        # any other spec problem — so trust is checked explicitly where it
        # applies (see run_endpoint) rather than caught by type here.
        except (HiveloomError, ValueError) as exc:
            return _error(f"{type(exc).__name__}: {exc}", 400, code="spec_error")
        except Exception as exc:  # noqa: BLE001 - a dev tool must show the traceback
            return _error(f"{type(exc).__name__}: {exc}", 500, detail=traceback.format_exc())

    return wrapped


# --------------------------------------------------------------------- #
# Copilot service — caller-owned context for the dedicated expert harness
# --------------------------------------------------------------------- #
class _CopilotWorkbench:
    """The constrained framework surface injected into one copilot run.

    Code tools receive this object through ``run_harness(context=...)``. It is
    not global, serialised, or traced, so concurrent conversations keep their
    own selection while every mutation still goes through Hiveloom's validated
    construction/evolution paths.
    """

    def __init__(
        self,
        *,
        catalog,
        resolve,
        creation_root: Path,
        memory: _MemoryStore | None = None,
        selected_harness: str = "",
        selected_run: str = "",
    ) -> None:
        self._catalog = catalog
        self._resolve = resolve
        self._creation_root = creation_root.resolve()
        self._memory = memory
        self._selected_harness = selected_harness.strip()
        self._selected_run = selected_run.strip()

    def _entry(self, harness_id: str = "") -> dict[str, Any]:
        target = harness_id.strip() or self._selected_harness
        if not target:
            raise ValueError("no harness is selected; name the harness to use")
        return self._resolve(target)

    def selection(self) -> dict[str, Any]:
        harness: dict[str, Any] | None = None
        if self._selected_harness:
            try:
                entry = self._entry()
                harness = {
                    "id": entry["id"],
                    "name": entry["name"],
                    "description": entry.get("description", ""),
                    "path": entry["path"],
                }
            except Exception:  # noqa: BLE001 - stale selection is context, not a crash
                harness = None
        return {"harness": harness, "run_id": self._selected_run or None}

    def recall_memories(self, query: str = "") -> dict[str, Any]:
        if self._memory is None:
            return {"memories": [], "count": 0}
        rows = self._memory.list(
            harness_id=self._selected_harness,
            query=query,
            limit=50,
        )
        return {"memories": rows, "count": len(rows)}

    def remember_memory(self, content: str, scope: str = "global") -> dict[str, Any]:
        if self._memory is None:
            raise ValueError("workbench memory is unavailable")
        clean_scope = scope.strip().lower()
        if clean_scope == "global":
            harness_id = ""
        elif clean_scope == "harness":
            harness_id = self._entry()["id"]
        else:
            raise ValueError("memory scope must be 'global' or 'harness'")
        return self._memory.remember(content, harness_id=harness_id)

    def forget_memory(self, memory_id: str) -> dict[str, Any]:
        if self._memory is None:
            raise ValueError("workbench memory is unavailable")
        self._memory.forget(memory_id)
        return {"ok": True, "id": memory_id}

    def list_harnesses(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in sorted(self._catalog().values(), key=lambda row: row["name"]):
            stats = self.stats(entry["id"]) if entry.get("ok") else None
            rows.append(
                {
                    "id": entry["id"],
                    "name": entry["name"],
                    "description": entry.get("description", ""),
                    "ok": bool(entry.get("ok")),
                    "trusted": trust_mod.is_trusted(entry["path"]),
                    "version_hash": entry.get("version_hash"),
                    "total_runs": (stats or {}).get("total_runs", 0),
                    "success_rate": (stats or {}).get("success_rate", 0.0),
                }
            )
        return rows

    def inspect_harness(self, harness_id: str = "") -> dict[str, Any]:
        entry = self._entry(harness_id)
        spec = load_spec(harness_path(entry["path"]))
        version = spec_version_hash(spec, Path(entry["path"]))
        raw = spec.model_dump(mode="json")
        return {
            "id": entry["id"],
            "name": spec.name,
            "description": spec.description,
            "path": entry["path"],
            "trusted": trust_mod.is_trusted(entry["path"]),
            "version_hash": version,
            "model": raw.get("model"),
            "system_prompt": spec.system_prompt,
            "tools": raw.get("tools") or [],
            "playbooks": raw.get("playbooks") or [],
            "verification": raw.get("verify") or {},
            "guardrails": raw.get("guardrails") or [],
            "loop": raw.get("loop") or {},
            "input_contract": _input_contract(spec.description, spec.system_prompt),
            "stats": self.stats(entry["id"]),
        }

    def read_harness_file(
        self, path: str, harness_id: str = ""
    ) -> dict[str, Any]:
        """Read a text attachment without widening the copilot's filesystem.

        The browser uploads into a target harness and names the returned
        relative path in the turn. This tool resolves that path through the
        same containment and protected-state gate as Hiveloom's file tools.
        """
        entry = self._entry(harness_id)
        base = Path(entry["path"])
        spec = load_spec(harness_path(base))
        trace_dir = trace_dir_relative_to(base, spec.logging.trace_dir)
        try:
            target = safe_path(base, path.strip(), trace_dir=trace_dir)
        except ToolError as exc:
            raise ValueError(str(exc)) from exc
        if not target.is_file():
            raise ValueError(f"file {path!r} does not exist in harness {entry['name']!r}")
        raw = target.read_bytes()
        if len(raw) > _MAX_COPILOT_FILE_BYTES:
            raise ValueError(
                f"file is {len(raw)} bytes; copilot inspection is limited to "
                f"{_MAX_COPILOT_FILE_BYTES} bytes"
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("the copilot can inspect UTF-8 text attachments only") from exc
        return {
            "harness_id": entry["id"],
            "harness_name": entry["name"],
            "path": str(target.relative_to(base)),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": content,
        }

    def create_harness(
        self,
        *,
        name: str,
        task: str,
        system_prompt: str,
        builtin_tools: list[str],
        output_schema_json: str,
        max_turns: int,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        clean_task = task.strip()
        clean_prompt = system_prompt.strip()
        if not clean_name or not clean_task or not clean_prompt:
            raise ValueError("name, task, and system_prompt must all be non-empty")
        if not 1 <= int(max_turns) <= 100:
            raise ValueError("max_turns must be between 1 and 100")

        directory = (self._creation_root / _slug(clean_name)).resolve()
        if directory.parent != self._creation_root:
            raise ValueError("the harness name does not resolve inside the creation root")
        if directory.exists():
            raise ValueError(f"a file or directory already exists at {directory}")

        known_tools = set(catalog_mod.CATALOGS["tools"])
        unknown = sorted(set(builtin_tools) - known_tools)
        if unknown:
            raise ValueError(f"unknown builtin tools: {unknown}")

        schema: dict[str, Any] | None = None
        if output_schema_json.strip():
            parsed = json.loads(output_schema_json)
            if not isinstance(parsed, dict):
                raise ValueError("output_schema_json must encode a JSON object")
            schema = parsed

        try:
            construct.init_harness(directory, name=clean_name, task=clean_task)
            construct.set_value(directory, "system_prompt", clean_prompt)
            construct.set_value(directory, "loop.max_turns", int(max_turns))
            for builtin in builtin_tools:
                construct.add_tool(directory, builtin=builtin)
            if schema is not None:
                schema_path = directory / "schemas" / "output.json"
                atomic_write_text(schema_path, json.dumps(schema, indent=2) + "\n")
                construct.add_validator(
                    directory,
                    builtin="output_schema",
                    schema_file="./schemas/output.json",
                )
                construct.set_value(directory, "loop.require_verification", True)
            validate_harness(directory)
        except Exception:
            if directory.exists():
                shutil.rmtree(directory)
            trust_mod.revoke_trust(directory)
            raise

        try:
            registry_mod.register(directory)
            trust_mod.record_trust(directory)
            created = next(
                (
                    row
                    for row in self._catalog().values()
                    if Path(row["path"]).resolve() == directory
                ),
                None,
            )
            if created is None:
                raise RuntimeError("the created harness was not discoverable after registration")
        except Exception:
            # Creation is one operation to the caller. Registration and trust
            # are part of its commit, so a failure in either cannot leave a
            # valid-looking but half-attached harness behind.
            try:
                registry_mod.unregister(directory)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
            trust_mod.revoke_trust(directory)
            if directory.exists():
                shutil.rmtree(directory)
            raise
        return self.inspect_harness(created["id"])

    def validate_harness(self, harness_id: str = "") -> dict[str, Any]:
        entry = self._entry(harness_id)
        spec = validate_harness(entry["path"])
        return {
            "ok": True,
            "id": entry["id"],
            "name": spec.name,
            "version_hash": spec_version_hash(spec, Path(entry["path"])),
        }

    def dry_run(self, harness_id: str, sample_input: str) -> dict[str, Any]:
        entry = self._entry(harness_id)
        if not trust_mod.is_trusted(entry["path"]):
            raise ValueError(f"harness {entry['name']!r} is not trusted")
        assembled = runner_mod.dry_run(entry["path"], sample_input)
        return {**assembled, "id": entry["id"]}

    def run_target(
        self, harness_id: str, input_value: str, *, copilot_run_id: str = ""
    ) -> dict[str, Any]:
        entry = self._entry(harness_id)
        if not trust_mod.is_trusted(entry["path"]):
            raise ValueError(f"harness {entry['name']!r} is not trusted")
        result = runner_mod.run_harness(
            entry["path"],
            input_value,
            literal_input=True,
            context={"copilot_run_id": copilot_run_id},
        )
        payload = runner_mod.run_result_payload(result)
        payload.update(
            {
                "harness_id": entry["id"],
                "harness_name": entry["name"],
                "copilot_run_id": copilot_run_id or None,
                "verdicts": [
                    {
                        "verifier": verdict.verifier,
                        "passed": verdict.passed,
                        "feedback": verdict.feedback,
                    }
                    for verdict in result.verdicts
                ],
            }
        )
        return payload

    def inspect_run(self, run_id: str = "") -> dict[str, Any]:
        target = run_id.strip() or self._selected_run
        if not target:
            raise ValueError("no run is selected; name the run to inspect")
        with Hive() as hive:
            for entry in self._catalog().values():
                if entry.get("ok"):
                    _ingest_entry_traces(hive, entry)
            run = hive.get_run(target)
            lineage = hive.lineage(target)
        if run is None:
            raise LookupError(f"run {target!r} is not in the Hive")
        trace_path = Path(run.get("trace_path") or "")
        events = read_events(trace_path) if trace_path.is_file() else []
        evidence = []
        for event in events:
            payload = event.get("payload") or {}
            if event.get("type") == "verification_result" and not payload.get("passed"):
                evidence.append(
                    {
                        "seq": event.get("seq"),
                        "type": "verification_failure",
                        "name": payload.get("verifier"),
                        "detail": payload.get("feedback"),
                    }
                )
            elif event.get("type") == "guardrail_triggered":
                evidence.append(
                    {
                        "seq": event.get("seq"),
                        "type": "guardrail",
                        "name": payload.get("guardrail"),
                        "detail": payload.get("reason"),
                    }
                )
            elif event.get("type") == "tool_result" and payload.get("is_error"):
                evidence.append(
                    {
                        "seq": event.get("seq"),
                        "type": "tool_error",
                        "name": payload.get("name"),
                        "detail": payload.get("content"),
                    }
                )
        return {
            "run": run,
            "evidence": evidence,
            "lineage": lineage,
            "integrity": verify_chain(trace_path).summary() if trace_path.is_file() else None,
            "event_count": len(events),
        }

    def list_runs(self, harness_id: str = "", limit: int = 10) -> dict[str, Any]:
        entry = self._entry(harness_id)
        if not 1 <= int(limit) <= 50:
            raise ValueError("limit must be between 1 and 50")
        with Hive() as hive:
            _ingest_entry_traces(hive, entry)
            rows = [
                run
                for path in sorted(
                    _trace_files(entry["path"]),
                    key=lambda candidate: candidate.stat().st_mtime,
                    reverse=True,
                )
                if (run := hive.get_run(path.stem)) is not None
            ][: int(limit)]
        return {
            "harness_id": entry["id"],
            "harness_name": entry["name"],
            "runs": rows,
            "count": len(rows),
        }

    def stats(self, harness_id: str = "") -> dict[str, Any]:
        entry = self._entry(harness_id)
        with Hive() as hive:
            _ingest_entry_traces(hive, entry)
            return hive.summary(entry.get("key") or entry["name"])

    def compare(self, harness_id: str, left: str, right: str) -> dict[str, Any]:
        entry = self._entry(harness_id)
        with Hive() as hive:
            _ingest_entry_traces(hive, entry)
            return hive.compare_versions(entry.get("key") or entry["name"], left, right)

    def propose(self, harness_id: str, model_id: str | None) -> dict[str, Any]:
        entry = self._entry(harness_id)
        path = Path(entry["path"])
        with Hive() as hive:
            name = runner_mod.resolve_and_ingest(path, hive)
            spec = load_spec(harness_path(path))
            version = spec_version_hash(spec, path)
            report = evolve_mod.analyze(hive, name, version=version)
            if report.is_empty():
                return {
                    "ok": True,
                    "changed": False,
                    "summary": (
                        "No failures are recorded for this version, so there is "
                        "nothing to improve yet."
                    ),
                    "harness_id": entry["id"],
                    "version_hash": version,
                }
            strong_model = build_strong_model(model_id, path)
            record = proposals_mod.create_proposal(
                hive, spec, path, report, strong_model, trigger="copilot"
            )
        return {
            "ok": True,
            "changed": True,
            "summary": "Drafted and safety-gated an improvement proposal; it has not been applied.",
            "harness_id": entry["id"],
            **proposals_mod.proposal_payload(record),
        }

    def create_interface(
        self,
        harness_id: str,
        *,
        title: str,
        input_label: str,
        submit_label: str,
        input_kind: str,
    ) -> dict[str, Any]:
        entry = self._entry(harness_id)
        spec = load_spec(harness_path(entry["path"]))
        inferred = _input_contract(spec.description, spec.system_prompt)
        kind = input_kind.strip().lower() or "auto"
        if kind == "auto":
            kind = inferred["kind"]
        if kind not in {"url", "text", "file"}:
            raise ValueError("input_kind must be auto, url, text, or file")
        contract = {
            **inferred,
            "kind": kind,
            "label": input_label.strip() or inferred["label"],
        }
        html_text = _standalone_interface_html(
            harness_id=entry["id"],
            harness_name=spec.name,
            title=title.strip() or spec.name.replace("-", " ").title(),
            description=spec.description,
            submit_label=submit_label.strip() or "Run",
            contract=contract,
        )
        target = Path(entry["path"]) / "interfaces" / "default" / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_bytes() if target.exists() else None
        atomic_write_text(target, html_text)
        return {
            "harness_id": entry["id"],
            "harness_name": spec.name,
            "harness_version_hash": spec_version_hash(spec, Path(entry["path"])),
            "path": str(target),
            "contract": contract,
            "html": html_text,
            "sha256": hashlib.sha256(html_text.encode()).hexdigest(),
            "replaced_sha256": hashlib.sha256(previous).hexdigest() if previous else None,
        }


def _input_contract(description: str, system_prompt: str) -> dict[str, str]:
    """Derive only task shapes the harness states explicitly."""
    text = f"{description}\n{system_prompt}".lower()
    if re.search(r"task input (?:is|should be) (?:the |a )?url", text):
        return {
            "kind": "url",
            "label": "Article URL" if "article" in text else "URL",
            "help": "Paste one complete public http(s) URL.",
            "placeholder": "https://example.com/article",
        }
    if re.search(r"(?:read|open) the file (?:named|path) in the task input", text):
        return {
            "kind": "file",
            "label": "Source file",
            "help": "Choose a file to place in the harness workspace.",
            "placeholder": "Choose a file",
        }
    return {
        "kind": "text",
        "label": "Task",
        "help": description or "Describe the task to run.",
        "placeholder": "What should this harness do?",
    }


def _standalone_interface_html(
    *,
    harness_id: str,
    harness_name: str,
    title: str,
    description: str,
    submit_label: str,
    contract: dict[str, str],
) -> str:
    """A dependency-free interface that works in the sandbox bridge or at /run."""
    field_type = "url" if contract["kind"] == "url" else "text"
    field = (
        '<input id="task" type="file" required>'
        if contract["kind"] == "file"
        else (
            f'<input id="task" type="{field_type}" required '
            f'placeholder="{html.escape(contract["placeholder"], quote=True)}">'
        )
    )
    config = json.dumps({"harnessId": harness_id, "kind": contract["kind"]})
    template = Path(__file__).with_name("interface-template.html").read_text(encoding="utf-8")
    replacements = {
        "__TITLE__": html.escape(title),
        "__DESCRIPTION__": html.escape(description),
        "__FIELD__": field,
        "__INPUT_LABEL__": html.escape(contract["label"]),
        "__INPUT_HELP__": html.escape(contract["help"]),
        "__SUBMIT_LABEL__": html.escape(submit_label),
        "__HARNESS_NAME__": html.escape(harness_name),
        "__CONFIG__": config,
    }
    marker_pattern = re.compile("|".join(re.escape(marker) for marker in replacements))
    return marker_pattern.sub(lambda match: replacements[match.group(0)], template)


# --------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------- #
def build_app(extra_dirs: list[str], scan_dirs: list[str] | None = None) -> Starlette:
    scan_dirs = list(scan_dirs or [])
    # Before anything can be run or reported on: a run happens in this process,
    # so the workbench's keys have to be in this process's environment.
    workbench_keys: set[str] = _load_workbench_env(set())

    def catalog() -> dict[str, dict[str, Any]]:
        return _catalog(extra_dirs, scan_dirs)

    def resolve(harness_id: str) -> dict[str, Any]:
        return _resolve(extra_dirs, harness_id, scan_dirs)

    def _stats(entry: dict[str, Any]) -> dict[str, Any]:
        with Hive() as hive:
            _ingest_entry_traces(hive, entry)
            return hive.summary(entry.get("key") or entry["name"])

    creation_root = (
        Path(scan_dirs[0])
        if scan_dirs
        else (Path(extra_dirs[0]).resolve().parent if extra_dirs else Path.cwd() / "harnesses")
    )
    creation_root.mkdir(parents=True, exist_ok=True)
    conversation_database = Path(
        os.environ.get("HIVELOOM_UI_DB", _workbench_home() / "workbench.db")
    ).expanduser()
    conversations = _ConversationStore(
        conversation_database,
        legacy_directory=paths_mod.hiveloom_home() / "workbench" / "conversations",
    )
    memories = _MemoryStore(conversation_database)
    # This harness is part of the workbench itself, not a foreign target
    # folder. Starting the workbench is the explicit act that authorizes its
    # bundled code tools; target harnesses retain their ordinary trust gates.
    # Resolved here rather than at import because resolving it can materialize a
    # directory, which importing a module must not do.
    copilot_dir = _copilot_dir()
    validate_harness(copilot_dir)
    trust_mod.record_trust(copilot_dir)

    def copilot_service(selection: dict[str, Any] | None = None) -> _CopilotWorkbench:
        picked = selection or {}
        return _CopilotWorkbench(
            catalog=catalog,
            resolve=resolve,
            creation_root=creation_root,
            memory=memories,
            selected_harness=str(picked.get("harness_id") or ""),
            selected_run=str(picked.get("run_id") or ""),
        )

    # ---------------- copilot ---------------- #
    @_guarded
    async def copilot_info(request: Request) -> Response:
        spec = await asyncio.to_thread(validate_harness, copilot_dir)
        return JSONResponse(
            {
                "ok": True,
                "name": spec.name,
                "description": spec.description,
                "model": f"{spec.model.provider}/{spec.model.id}",
                "version_hash": spec_version_hash(spec, copilot_dir),
                "suggestions": [
                    "Create a harness that extracts structured data from a document",
                    "Explain why the selected run failed",
                    "Compare the latest versions of this harness",
                    "Create a simple web interface for this harness",
                ],
            }
        )

    @_guarded
    async def list_conversations(request: Request) -> Response:
        return JSONResponse(
            {"conversations": await asyncio.to_thread(conversations.list)}
        )

    @_guarded
    async def create_conversation(request: Request) -> Response:
        body = json.loads(await request.body() or b"{}")
        if body:
            raise ValueError("conversation creation takes an empty object")
        return JSONResponse(
            await asyncio.to_thread(conversations.create), status_code=201
        )

    @_guarded
    async def get_conversation(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(
                conversations.get, request.path_params["conversation_id"]
            )
        )

    @_guarded
    async def put_conversation(request: Request) -> Response:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("conversation body must be an object")
        return JSONResponse(
            await asyncio.to_thread(
                conversations.save,
                request.path_params["conversation_id"],
                body,
            )
        )

    @_guarded
    async def delete_conversation(request: Request) -> Response:
        await asyncio.to_thread(
            conversations.delete, request.path_params["conversation_id"]
        )
        return JSONResponse({"ok": True})

    @_guarded
    async def list_memories(request: Request) -> Response:
        harness_id = str(request.query_params.get("harness") or "").strip()
        if harness_id:
            harness_id = (await asyncio.to_thread(resolve, harness_id))["id"]
        query = str(request.query_params.get("q") or "")
        rows = await asyncio.to_thread(
            memories.list,
            harness_id=harness_id,
            query=query,
            limit=100,
        )
        return JSONResponse({"memories": rows})

    @_guarded
    async def create_memory(request: Request) -> Response:
        body = json.loads(await request.body() or b"{}")
        unknown = sorted(set(body) - {"content", "harness_id"})
        if unknown:
            raise ValueError(f"unknown fields: {unknown}")
        harness_id = str(body.get("harness_id") or "").strip()
        if harness_id:
            harness_id = (await asyncio.to_thread(resolve, harness_id))["id"]
        record = await asyncio.to_thread(
            memories.remember,
            str(body.get("content") or ""),
            harness_id=harness_id,
        )
        return JSONResponse(record, status_code=201)

    @_guarded
    async def delete_memory(request: Request) -> Response:
        await asyncio.to_thread(memories.forget, request.path_params["memory_id"])
        return JSONResponse({"ok": True})

    @_guarded
    async def copilot_chat(request: Request) -> Response:
        """Run the bundled expert harness; every artifact is a typed UI card."""
        _load_workbench_env(workbench_keys)
        body = json.loads(await request.body() or b"{}")
        unknown = sorted(set(body) - {"input", "messages", "model", "selection"})
        if unknown:
            raise ValueError(f"unknown fields: {unknown}")
        conversation = body.get("messages")
        text = body.get("input")
        if (conversation is None) == (text is None):
            raise ValueError("pass exactly one of 'input' or 'messages'")
        selection = body.get("selection") or {}
        if not isinstance(selection, dict):
            raise ValueError("'selection' must be an object")

        events: queue.Queue = queue.Queue()
        run_id = runner_mod.new_run_id()
        control = RunControl()
        selector = str(body.get("model") or "").strip()
        if selector:
            provider, _, model_id = selector.partition("/")
            if not model_id:
                raise ValueError("'model' must be 'provider/model-id'")
            control.switch_model(
                model_id, provider=provider, reason="copilot model selected in workbench"
            )

        def on_event(event: Any) -> None:
            events.put(event.model_dump_json())

        def work() -> None:
            _LIVE.register(run_id, control)
            try:
                result = runner_mod.run_harness(
                    copilot_dir,
                    text,
                    conversation=conversation,
                    context={"workbench": copilot_service(selection), "selection": selection},
                    on_event=on_event,
                    literal_input=True,
                    run_id=run_id,
                    control=control,
                )
                payload = runner_mod.run_result_payload(result)
                payload["verdicts"] = [
                    {
                        "verifier": verdict.verifier,
                        "passed": verdict.passed,
                        "feedback": verdict.feedback,
                    }
                    for verdict in result.verdicts
                ]
                events.put(json.dumps({"type": "run_result", **payload}))
            except Exception as exc:  # noqa: BLE001 - the stream already started
                events.put(
                    json.dumps({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                )
            finally:
                _LIVE.release(run_id)
                events.put(_STREAM_DONE)

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            yield json.dumps({"type": "run_accepted", "run_id": run_id}) + "\n"
            while True:
                item = await asyncio.to_thread(events.get)
                if item is _STREAM_DONE:
                    break
                yield item + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @_guarded
    async def get_interface(request: Request) -> Response:
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        path = Path(entry["path"]) / "interfaces" / "default" / "index.html"
        if not path.is_file():
            return JSONResponse({"exists": False, "harness_id": entry["id"], "html": ""})
        return JSONResponse(
            {
                "exists": True,
                "harness_id": entry["id"],
                "path": str(path),
                "html": await asyncio.to_thread(path.read_text, encoding="utf-8"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )

    # ---------------- harnesses ---------------- #
    @_guarded
    async def list_harnesses(request: Request) -> Response:
        entries = await asyncio.to_thread(catalog)

        def decorate() -> list[dict[str, Any]]:
            rows = []
            for entry in sorted(entries.values(), key=lambda e: e["id"]):
                row = dict(entry)
                row["trusted"] = trust_mod.is_trusted(entry["path"])
                row["stats"] = _stats(entry) if entry["ok"] else None
                rows.append(row)
            return rows

        return JSONResponse({"harnesses": await asyncio.to_thread(decorate)})

    @_guarded
    async def create_harness(request: Request) -> Response:
        body = json.loads(await request.body() or b"{}")
        directory = body.get("directory")
        name = body.get("name")
        task = body.get("task")
        # Trust is the caller's to grant, not this endpoint's to assume. It
        # defaults to on because creating a harness here *is* the act of
        # vouching for the directory — the user named it — but a workbench set
        # to ask first must be able to say no, and the gate has to be told.
        trust = body.get("trust", True)
        if not (directory and name and task):
            raise ValueError("directory, name, and task are all required")

        def work() -> dict[str, Any]:
            spec = construct.init_harness(directory, name=name, task=task)
            # Registering is what puts it in the rail; trusting is the separate
            # thing you would otherwise do at a terminal before it could run.
            registry_mod.register(directory)
            # `init_harness` trusts what it creates as a convenience of its own.
            # A workbench set to ask first has to undo that rather than merely
            # decline to add it, or "ask first" would silently mean "always".
            if trust:
                trust_mod.record_trust(directory)
            else:
                trust_mod.revoke_trust(directory)
            return {
                "id": _slug(spec.name),
                "name": spec.name,
                "directory": directory,
                "trusted": bool(trust),
            }

        return JSONResponse(await asyncio.to_thread(work), status_code=201)

    @_guarded
    async def get_harness(request: Request) -> Response:
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])

        def work() -> dict[str, Any]:
            yaml_path = harness_path(entry["path"])
            payload: dict[str, Any] = {
                **entry,
                "trusted": trust_mod.is_trusted(entry["path"]),
                "yaml": yaml_path.read_text(encoding="utf-8"),
                "yaml_path": str(yaml_path),
            }
            if entry["ok"]:
                spec = load_spec(yaml_path)
                payload["spec"] = spec.model_dump(mode="json")
                # The version a run started now would be recorded under, so
                # the browser can mark when the harness moved between runs.
                payload["version_hash"] = spec_version_hash(spec, Path(entry["path"]))
                payload["stats"] = _stats(entry)
            return payload

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def put_spec(request: Request) -> Response:
        """Write the spec file, but never leave an invalid one on disk.

        The editor is free-form YAML, so the file is written, validated, and
        rolled back if validation fails — the user keeps their text in the
        editor and sees why it was refused, rather than a harness that no longer
        loads.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        text = body.get("yaml")
        if not isinstance(text, str):
            raise ValueError("body must carry a 'yaml' string")

        def work() -> dict[str, Any]:
            yaml_path = harness_path(entry["path"])
            previous = yaml_path.read_text(encoding="utf-8")
            yaml_path.write_text(text, encoding="utf-8")
            try:
                spec = validate_harness(entry["path"])
            except Exception:
                yaml_path.write_text(previous, encoding="utf-8")
                raise
            return {"ok": True, "name": spec.name, "id": _slug(spec.name)}

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def list_providers(request: Request) -> Response:
        """The model directory: which providers exist and whether their key is set.

        The same data `hiveloom models --json` prints, and the same discipline:
        whether a key is *present*, never its value. Presence is judged the way
        a run judges it — the process environment *or* the harness's own
        `.env`, which is where `hiveloom init` tells people to put the key.

        With ``?harness=<id>`` it also answers *for that harness*, which is a
        different question. The provider registry is process-global, and
        listing the rail loads every harness's spec — so one harness shipping
        an extension that registers a provider (routing-lab's offline demo
        provider is exactly this) puts that provider in front of every other
        harness, which could not run it: the id is not registered when its own
        spec is built. ``scope`` says where a provider came from and
        ``available`` says whether this harness may pick it, so the pickers can
        offer models that will actually load.
        """
        harness_id = request.query_params.get("harness", "")

        def work() -> dict[str, Any]:
            from hiveloom import ext

            # Where a key actually lives.
            #
            # The provider factories load `<harness>/.env` before they read the
            # environment — that is the documented place to put a key, and
            # `hiveloom init` writes the `.env.example` that says so. A
            # directory that only looked at `os.environ` therefore reported
            # "no key" for the one provider the person has configured, and
            # every picker fell back to offering all of them. Read as values,
            # not loaded: answering a GET must not mutate the process
            # environment, and only the *presence* of a name is ever returned.
            # Written since the API started? Adopt it now, so a key added
            # while the workbench is open needs a page reload, not a restart.
            workbench = _load_workbench_env(workbench_keys)
            local_env: dict[str, str | None] = {}
            declared: set[str] = set()
            if harness_id:
                try:
                    entry = resolve(harness_id)
                    declared = set(load_spec(harness_path(entry["path"])).extensions or [])
                    env_file = Path(entry["path"]) / ".env"
                    if env_file.exists():
                        from dotenv import dotenv_values

                        local_env = dict(dotenv_values(env_file))
                except Exception:  # noqa: BLE001 - a broken spec still gets a directory
                    pass

            def key_from(name: str) -> str:
                """Where the key comes from: '', 'process', 'workbench', 'harness'."""
                if not name:
                    return "none"  # a provider that needs no key is always ready
                if os.environ.get(name):
                    return "workbench" if name in workbench else "process"
                if (local_env.get(name) or "").strip():
                    return "harness"
                return ""

            # `declared` above is the extension refs this harness names,
            # exactly as written in its spec — the same string the registry
            # recorded as the source. Matching by ref means two harnesses that
            # both declare `extensions/x.py` cannot be told apart; the registry
            # does not record which folder a source came from, and a demo
            # provider leaking between two harnesses that ship the same
            # relative path is a far smaller wrong than leaking to all of them.
            def scope_of(source: str) -> str:
                return "harness" if source.startswith("harness:") else "global"

            def available(source: str) -> bool:
                if not source.startswith("harness:"):
                    return True
                return source[len("harness:") :] in declared

            return {
                "providers": [
                    {
                        "name": provider.name,
                        "label": provider.label,
                        "api_key_env": provider.api_key_env,
                        "api_key_set": bool(key_from(provider.api_key_env)),
                        # Which of the two places it was found in, so the page
                        # that manages keys can say so without being asked.
                        "api_key_from": key_from(provider.api_key_env),
                        "open_catalog": provider.open_catalog,
                        "source": provider.source,
                        "scope": scope_of(provider.source),
                        "available": available(provider.source),
                        # Cost and context travel with the id, because the
                        # only useful thing to know when picking a model is
                        # what it will cost and how much it can hold. Both
                        # come from the same registry the cost estimator and
                        # the budget guardrail read, so a number shown here is
                        # the number a run will be charged against.
                        "models": [
                            {
                                "id": model.id,
                                "label": getattr(model, "label", "") or model.id,
                                "context_window": model.context_window,
                                "input_cost_per_mtok": model.input_cost_per_mtok,
                                "output_cost_per_mtok": model.output_cost_per_mtok,
                            }
                            for model in ext.models_for_provider(provider.name)
                        ],
                    }
                    for provider in ext.providers()
                ]
            }

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def put_model(request: Request) -> Response:
        """Move a harness to another provider/model, and its sampling limits.

        Through ``construct``, never by writing YAML here: provider and id
        validate against each other, so they have to move in one commit — and
        that commit is the same transactional, rolled-back-on-error path
        `hiveloom set model` uses. The two numeric fields go through
        ``set_value`` for the same reason, one commit each, so an out-of-range
        temperature is refused by the schema rather than left on disk.

        The field list is closed on purpose. This is not a generic "write any
        spec path" endpoint — the Spec tab is that, with a validating save and
        a rollback — it is the three fields the Models pane owns.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        selector = str(body.get("selector") or "").strip()
        temperature = body.get("temperature")
        max_input_tokens = body.get("max_input_tokens")
        if not selector and temperature is None and max_input_tokens is None:
            raise ValueError(
                "nothing to set: pass 'selector', 'temperature', or 'max_input_tokens'"
            )
        if selector and "/" not in selector:
            raise ValueError("'selector' must be 'provider/model-id'")

        def work() -> dict[str, Any]:
            spec = None
            if selector:
                spec = construct.set_model(entry["path"], selector)
            if temperature is not None:
                # An empty string means "omit it" — required for models that
                # deprecate the field, which is why None is a real value here
                # rather than a missing one.
                value = None if temperature == "" else float(temperature)
                spec = construct.set_value(entry["path"], "model.temperature", value)
            if max_input_tokens is not None:
                spec = construct.set_value(
                    entry["path"], "context.max_input_tokens", int(max_input_tokens)
                )
            if spec is None:
                spec = load_spec(harness_path(entry["path"]))
            return {
                "ok": True,
                "provider": spec.model.provider,
                "id": spec.model.id,
                "temperature": spec.model.temperature,
                "max_input_tokens": spec.context.max_input_tokens,
            }

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def validate_endpoint(request: Request) -> Response:
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])

        def work() -> dict[str, Any]:
            spec = validate_harness(entry["path"])
            return {"ok": True, "name": spec.name}

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def trust_endpoint(request: Request) -> Response:
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        await asyncio.to_thread(trust_mod.record_trust, entry["path"])
        return JSONResponse({"ok": True, "trusted": True})

    # ---------------- running ---------------- #
    @_guarded
    async def run_endpoint(request: Request) -> Response:
        """Run a harness, streaming its trace as NDJSON.

        NDJSON rather than SSE because it is the format `hiveloom run --stream`
        and `hiveloom serve` already emit, so the UI learns one event
        vocabulary that matches what the CLI prints. (The HTTP control plane
        streams the same frames over SSE — same vocabulary, different
        transport.)
        """
        # The workbench's keys, before a provider is built rather than only
        # when the directory is asked for: a run must not depend on the browser
        # having happened to load the model list first.
        _load_workbench_env(workbench_keys)
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        unknown = sorted(set(body) - {"input", "messages", "model"})
        if unknown:
            raise ValueError(f"unknown fields: {unknown}")
        conversation = body.get("messages")
        text = body.get("input")
        if (conversation is None) == (text is None):
            raise ValueError("pass exactly one of 'input' or 'messages'")

        if not await asyncio.to_thread(trust_mod.is_trusted, entry["path"]):
            return _error(
                f"{entry['path']} is not trusted yet",
                403,
                code="trust_required",
                path=entry["path"],
            )

        events: queue.Queue = queue.Queue()

        # Pre-allocated, not discovered from the first event: the id is
        # announced on the stream's opening frame so the UI can address stop /
        # steer / model / playbook while the first model call is still in
        # flight. This matches `hiveloom serve`'s streaming contract.
        run_id = runner_mod.new_run_id()
        control = RunControl()
        # A run may use a model the spec does not name. It is queued as an
        # ordinary operator model switch rather than written into the spec: the
        # loop consumes switches at the top of the turn, *before* the first
        # model call, so turn 1 already runs on it, and the change is journalled
        # as a `model_swap` like every other one. The harness on disk is
        # untouched, which is the whole point.
        selector = str(body.get("model") or "").strip()
        if selector:
            provider, _, model_id = selector.partition("/")
            if not model_id:
                raise ValueError("'model' must be 'provider/model-id'")
            control.switch_model(
                model_id, provider=provider, reason="run model, set in the workbench"
            )

        def on_event(event: Any) -> None:
            events.put(event.model_dump_json())

        def work() -> None:
            _LIVE.register(run_id, control)
            try:
                result = runner_mod.run_harness(
                    entry["path"],
                    text,
                    conversation=conversation,
                    on_event=on_event,
                    literal_input=True,
                    run_id=run_id,
                    control=control,
                )
                payload = runner_mod.run_result_payload(result)
                payload["verdicts"] = [
                    {"verifier": v.verifier, "passed": v.passed, "feedback": v.feedback}
                    for v in result.verdicts
                ]
                events.put(json.dumps({"type": "run_result", **payload}))
            except Exception as exc:  # noqa: BLE001 - already streaming; report inline
                events.put(json.dumps({"type": "error", "error": f"{type(exc).__name__}: {exc}"}))
            finally:
                # Released before the terminator so a control call racing the
                # last frame gets a clean 404 rather than acting on a dead run.
                _LIVE.release(run_id)
                events.put(_STREAM_DONE)

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            yield json.dumps({"type": "run_accepted", "run_id": run_id}) + "\n"
            while True:
                item = await asyncio.to_thread(events.get)
                if item is _STREAM_DONE:
                    break
                yield item + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    # ---------------- live run control ---------------- #
    def _live(run_id: str) -> RunControl:
        control = _LIVE.get(run_id)
        if control is None:
            raise LookupError(f"run {run_id!r} is not running in this process")
        return control

    @_guarded
    async def list_live_runs(request: Request) -> Response:
        """Runs executing right now — what a reconnecting UI re-attaches to."""
        return JSONResponse({"run_ids": _LIVE.ids()})

    @_guarded
    async def stop_run(request: Request) -> Response:
        """Ask a run to stop gracefully at its next turn boundary.

        Not a cancellation: the run completes with status ``stopped``, its
        journal intact and its partial output kept, rather than being killed
        mid-tool. Aborting the fetch would leave the run going.
        """
        body = json.loads(await request.body() or b"{}")
        control = _live(request.path_params["run_id"])
        control.request_stop(str(body.get("reason") or "stopped from the workbench"))
        return JSONResponse({"ok": True, "stopping": True})

    @_guarded
    async def list_pending_messages(request: Request) -> Response:
        return JSONResponse({"messages": _live(request.path_params["run_id"]).pending_messages()})

    @_guarded
    async def queue_message(request: Request) -> Response:
        """Queue a steering message for the run's next turn boundary."""
        body = json.loads(await request.body() or b"{}")
        content = str(body.get("content") or "").strip()
        if not content:
            raise ValueError("'content' must be a non-empty string")
        message_id = _live(request.path_params["run_id"]).send_message(content)
        return JSONResponse({"ok": True, "id": message_id})

    @_guarded
    async def edit_message(request: Request) -> Response:
        """Rewrite a queued message, if the loop has not consumed it yet."""
        body = json.loads(await request.body() or b"{}")
        content = str(body.get("content") or "").strip()
        if not content:
            raise ValueError("'content' must be a non-empty string")
        control = _live(request.path_params["run_id"])
        if not control.edit_message(request.path_params["message_id"], content):
            raise LookupError("that message was already delivered to the agent")
        return JSONResponse({"ok": True})

    @_guarded
    async def remove_message(request: Request) -> Response:
        """Withdraw a queued message, if the loop has not consumed it yet."""
        control = _live(request.path_params["run_id"])
        if not control.remove_message(request.path_params["message_id"]):
            raise LookupError("that message was already delivered to the agent")
        return JSONResponse({"ok": True})

    @_guarded
    async def switch_model(request: Request) -> Response:
        """Move a running run onto another model at its next turn boundary."""
        body = json.loads(await request.body() or b"{}")
        model = body.get("model") or None
        provider = body.get("provider") or None
        if not model and not provider:
            raise ValueError("pass 'model' and/or 'provider'")
        _live(request.path_params["run_id"]).switch_model(
            model, provider=provider, reason=str(body.get("reason") or "")
        )
        return JSONResponse({"ok": True, "queued_for_next_turn": True})

    @_guarded
    async def switch_playbook(request: Request) -> Response:
        """Put a running run into another playbook at its next turn boundary.

        The mode's own entry/exit gates still run and may refuse; the refusal
        appears in the journal as a ``playbook_switch`` with ``ok: false``.
        """
        body = json.loads(await request.body() or b"{}")
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("'name' must be a non-empty string")
        _live(request.path_params["run_id"]).switch_playbook(
            name, reason=str(body.get("reason") or "")
        )
        return JSONResponse({"ok": True, "queued_for_next_turn": True})

    # ---------------- history ---------------- #
    @_guarded
    async def list_runs(request: Request) -> Response:
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])

        def work() -> list[dict[str, Any]]:
            with Hive() as hive:
                # Ingest first: a run written by the CLI, or by a copied-back
                # deployment, belongs in this list too. Ingestion is idempotent
                # by run id.
                _ingest_entry_traces(hive, entry)
                rows = []
                for path in sorted(_trace_files(entry["path"]), reverse=True):
                    run = hive.get_run(path.stem)
                    if run is not None:
                        rows.append(run)
                return rows

        return JSONResponse({"runs": await asyncio.to_thread(work)})

    @_guarded
    async def get_run(request: Request) -> Response:
        run_id = request.path_params["run_id"]

        def work() -> dict[str, Any]:
            with Hive() as hive:
                for entry in catalog().values():
                    if entry.get("ok"):
                        _ingest_entry_traces(hive, entry)
                run = hive.get_run(run_id)
                lineage = hive.lineage(run_id)
            if run is None:
                raise LookupError(f"run {run_id!r} is not in the Hive")
            events: list[dict[str, Any]] = []
            trace_file = Path(run.get("trace_path", ""))
            if trace_file.exists():
                events = read_events(trace_file)
                chain = verify_chain(trace_file)
                integrity = {
                    "ok": chain.ok,
                    "chained": chain.chained,
                    "checked": chain.checked,
                    "broken_at": chain.broken_at,
                    "reason": chain.reason,
                    "summary": chain.summary(),
                }
            else:
                integrity = None

            finished = next(
                (event for event in reversed(events) if event.get("type") == "run_finished"),
                None,
            )
            finish_payload = (finished or {}).get("payload") or {}
            return {
                "run": run,
                "events": events,
                "integrity": integrity,
                "lineage": lineage,
                "fork_points": [
                    {
                        "seq": point.seq,
                        "turn": point.turn,
                        "phase": point.phase,
                        "num_messages": point.num_messages,
                        "timestamp": point.timestamp,
                    }
                    for point in fork_points(events)
                ],
                "artifacts": finish_payload.get("artifacts") or [],
            }

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def materialize_context(request: Request) -> Response:
        """Fold the journal into the request state visible at one event.

        For a model call this is the exact request immediately before the call;
        for every other event it is the state including that event. This is the
        same fold used by ``hiveloom trace --materialize`` and ``hiveloom fork``.
        """
        run_id = request.path_params["run_id"]
        try:
            seq = int(request.path_params["seq"])
        except (TypeError, ValueError) as exc:
            raise ValueError("seq must be an integer") from exc

        def work() -> dict[str, Any]:
            with Hive() as hive:
                run = hive.get_run(run_id)
            if run is None:
                raise LookupError(f"run {run_id!r} is not in the Hive")
            trace_file = Path(run.get("trace_path", ""))
            if not trace_file.exists():
                raise LookupError(f"journal for run {run_id!r} is missing")
            events = read_events(trace_file)
            target = next((event for event in events if event.get("seq") == seq), None)
            if target is None:
                raise LookupError(f"run {run_id!r} has no event at seq {seq}")

            is_model_call = target.get("type") == "model_call"
            state = state_at(events, seq, inclusive=not is_model_call)
            target_payload = target.get("payload") or {}
            recorded = target_payload.get("messages_hash")
            # A 1.0 model call has `context_head`; older full traces carried a
            # request snapshot on model_call. Some early summary traces have
            # neither, and an empty fold from those is "not recorded", not an
            # exact empty request.
            available = any(
                event.get("type")
                in {
                    "context_append",
                    "context_compaction",
                    "context_system",
                    "context_tools",
                }
                and event.get("seq", 0) <= seq
                for event in events
            ) or (
                is_model_call
                and (
                    "context_head" in target_payload
                    or any(key in target_payload for key in ("system", "messages", "tools"))
                )
            )
            faithful = available and (recorded is None or recorded == payload_hash(state.messages))
            return {
                "run_id": run_id,
                "seq": seq,
                "type": target.get("type"),
                "available": available,
                "faithful": faithful,
                "request": state.as_request(),
            }

        return JSONResponse(await asyncio.to_thread(work))

    # ---------------- catalog ---------------- #
    # ---------------- fork & export ---------------- #
    def _journal_of(run_id: str) -> Path:
        with Hive() as hive:
            run = hive.get_run(run_id)
        if run is None:
            raise LookupError(f"run {run_id!r} is not in the Hive")
        trace_file = Path(run.get("trace_path", ""))
        if not trace_file.exists():
            raise LookupError(f"journal for run {run_id!r} is missing")
        return trace_file

    @_guarded
    async def fork_run(request: Request) -> Response:
        """Re-enter a finished run at one of its model calls.

        The target directory is resolved under the source harness's protected
        ``.hiveloom/forks`` directory and its name is slug-checked: a fork
        writes files, and a browser-supplied path is exactly the input that
        must never be able to choose where. Keeping it below ``.hiveloom`` also
        prevents the parent harness's file tools from reading or mutating the
        experiment. ``--dir`` on the CLI is a developer's own shell; this is
        not, so there is no equivalent here.
        """
        run_id = request.path_params["run_id"]
        body = json.loads(await request.body() or b"{}")
        at = body.get("at")
        if at is not None and not isinstance(at, int):
            raise ValueError("'at' must be an integer event seq")
        name = str(body.get("name") or f"{run_id}-fork").strip()

        def work() -> dict[str, Any]:
            trace_file = _journal_of(run_id)
            # The harness folder that owns the journal; the fork stays inside
            # its protected workbench state, never at a caller-chosen path.
            source = trace_file.resolve().parent
            while source != source.parent and not (source / "harness.yaml").is_file():
                source = source.parent
            if not (source / "harness.yaml").is_file():
                raise LookupError(f"cannot locate the harness folder for run {run_id!r}")
            # One resolver for the CLI, the workbench and MCP, so they cannot
            # disagree about where a fork goes or which names are allowed.
            target = fork_target(source, name)
            result = create_fork(
                trace_file,
                target,
                at=at,
                model=body.get("model") or None,
                model_provider=body.get("provider") or None,
                allow_drift=bool(body.get("allow_drift")),
            )
            # Registered on creation for the same reason `create_harness`
            # registers: a fork you cannot find is a fork you did not make. The
            # catalog disambiguates it from the parent it shares a name with.
            registry_mod.register(str(result.directory))
            return {
                "ok": True,
                "directory": str(result.directory),
                "harness_id": next(
                    (
                        entry["id"]
                        for entry in catalog().values()
                        if entry["path"] == str(Path(result.directory).resolve())
                    ),
                    "",
                ),
                "parent_run_id": result.parent_run_id,
                "at_seq": result.at_seq,
                "turn": result.turn,
                "messages": result.messages,
                "version_hash": result.version_hash,
                "model_override": result.model_override,
                "trust_inherited": result.trust_inherited,
                "warnings": result.warnings,
            }

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def export_run(request: Request) -> Response:
        """Download a run's journal verbatim.

        The file as written, not a re-serialization: the hash chain is over the
        bytes on disk, so a re-encoded export would not verify.
        """
        run_id = request.path_params["run_id"]
        trace_file = await asyncio.to_thread(_journal_of, run_id)
        return FileResponse(
            trace_file,
            media_type="application/x-ndjson",
            filename=f"{run_id}.jsonl",
        )

    @_guarded
    async def resume_fork(request: Request) -> Response:
        """Re-run a fork from the journal point it was created at.

        The parent's prefix is replayed verbatim against the fork's (possibly
        edited) harness — the same seam as ``hiveloom run <dir> --resume``. No
        new task statement is added: the seeded thread already ends where the
        parent was, which is what makes the two runs comparable.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        if body:
            raise ValueError(f"unknown fields: {sorted(body)}")
        record = await asyncio.to_thread(load_fork, entry["path"])
        if record is None:
            raise ValueError(
                f"{entry['path']} is not a fork directory; only a fork can be resumed"
            )
        if not await asyncio.to_thread(trust_mod.is_trusted, entry["path"]):
            return _error(
                f"{entry['path']} is not trusted yet",
                403,
                code="trust_required",
                path=entry["path"],
            )

        events: queue.Queue = queue.Queue()
        run_id = runner_mod.new_run_id()
        control = RunControl()

        def on_event(event: Any) -> None:
            events.put(event.model_dump_json())

        def work() -> None:
            _LIVE.register(run_id, control)
            try:
                result = runner_mod.run_harness(
                    entry["path"],
                    resume_messages=load_fork_context(entry["path"]),
                    lineage={
                        "parent_run_id": record.get("parent_run_id", ""),
                        "forked_at_seq": record.get("at_seq"),
                        "parent_line_hash": record.get("parent_line_hash", ""),
                    },
                    on_event=on_event,
                    run_id=run_id,
                    control=control,
                )
                payload = runner_mod.run_result_payload(result)
                payload["verdicts"] = [
                    {"verifier": v.verifier, "passed": v.passed, "feedback": v.feedback}
                    for v in result.verdicts
                ]
                events.put(json.dumps({"type": "run_result", **payload}))
            except Exception as exc:  # noqa: BLE001 - already streaming; report inline
                events.put(json.dumps({"type": "error", "error": f"{type(exc).__name__}: {exc}"}))
            finally:
                _LIVE.release(run_id)
                events.put(_STREAM_DONE)

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            yield json.dumps({"type": "run_accepted", "run_id": run_id}) + "\n"
            while True:
                item = await asyncio.to_thread(events.get)
                if item is _STREAM_DONE:
                    break
                yield item + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    # ---------------- comparison ---------------- #
    @_guarded
    async def compare_versions(request: Request) -> Response:
        """Two harness versions side by side, with deltas and failure movement.

        What makes an evolution or a fork judgeable: not "the new one scored
        71%" but how much moved, at what cost, and which failure signature
        stopped appearing.
        """
        left = request.query_params.get("left")
        right = request.query_params.get("right")
        if not left or not right:
            raise ValueError("pass both 'left' and 'right' version hashes")
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])

        def work() -> dict[str, Any]:
            with Hive() as hive:
                _ingest_entry_traces(hive, entry)
                return hive.compare_versions(entry.get("key") or entry["name"], left, right)

        return JSONResponse(await asyncio.to_thread(work))

    # ---------------- evolution ---------------- #
    @_guarded
    async def propose_evolution(request: Request) -> Response:
        """Draft a gated evolution proposal from this harness's failures.

        Drafts only. Applying is a separate, explicit call — the same split the
        CLI and the control plane enforce, because an agent that can both
        propose and apply its own changes has no gate at all.

        ``from_parent`` analyses the parent run's version instead of the one on
        disk. This screen is where forks are made, so it is also where the
        scoping bites: a fork exists *because* its parent failed, but until it
        is resumed it has no runs of its own, and the default reports nothing
        to evolve at the one moment there is most to say.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        from_parent = bool(body.get("from_parent"))

        def work() -> dict[str, Any]:
            path = Path(entry["path"])
            with Hive() as hive:
                name = runner_mod.resolve_and_ingest(path, hive)
                spec = load_spec(harness_path(path))
                version = (
                    parent_version_hash(path) if from_parent else spec_version_hash(spec, path)
                )
                report = evolve_mod.analyze(hive, name, version=version)
                if report.is_empty():
                    return {
                        "ok": True,
                        "changed": False,
                        "reason": (
                            f"no failures recorded for the parent version {version}"
                            if from_parent
                            else "no failures to learn from"
                        ),
                    }
                model = build_strong_model(body.get("model"), path)
                record = proposals_mod.create_proposal(
                    hive,
                    spec,
                    path,
                    report,
                    model,
                    trigger="fork" if from_parent else "workbench",
                )
            return {"ok": True, "changed": True, **proposals_mod.proposal_payload(record)}

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def list_proposals(request: Request) -> Response:
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        status = request.query_params.get("status")

        def work() -> dict[str, Any]:
            with Hive() as hive:
                _ingest_entry_traces(hive, entry)
                records = proposals_mod.list_proposals(
                    hive, harness_name=entry["name"], status=status
                )
            return {"proposals": [proposals_mod.proposal_payload(r) for r in records]}

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def get_proposal(request: Request) -> Response:
        proposal_id = request.path_params["proposal_id"]

        def work() -> dict[str, Any]:
            with Hive() as hive:
                record = proposals_mod.get_proposal(hive, proposal_id)
            if record is None:
                raise LookupError(f"proposal {proposal_id!r} is not in the Hive")
            return proposals_mod.proposal_payload(record)

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def apply_proposal(request: Request) -> Response:
        """Apply a queued proposal to a harness.

        ``approve_code`` is the HTTP substitute for the CLI's per-file y/n:
        a code change not named in it stays pending. Silence is a refusal, not
        consent — a caller that forgets the field applies no code.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        proposal_id = request.path_params["proposal_id"]
        body = json.loads(await request.body() or b"{}")
        approved = body.get("approve_code") or []
        if not isinstance(approved, list):
            raise ValueError("'approve_code' must be a list of file paths")
        apply_yaml = body.get("apply_yaml", True)

        def work() -> dict[str, Any]:
            with Hive() as hive:
                result = proposals_mod.apply_proposal_by_id(
                    hive,
                    entry["path"],
                    proposal_id,
                    approve_code=lambda change: change.file in approved,
                    apply_yaml=bool(apply_yaml),
                )
            return {"ok": True, **json.loads(result.model_dump_json())}

        return JSONResponse(await asyncio.to_thread(work))

    @_guarded
    async def reject_proposal(request: Request) -> Response:
        proposal_id = request.path_params["proposal_id"]
        body = json.loads(await request.body() or b"{}")

        def work() -> dict[str, Any]:
            with Hive() as hive:
                proposals_mod.reject_proposal(
                    hive, proposal_id, str(body.get("reason") or "rejected in the workbench")
                )
            return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}

        return JSONResponse(await asyncio.to_thread(work))

    # ---------------- version labels ---------------- #
    @_guarded
    async def list_version_tags(request: Request) -> Response:
        """Human labels for this harness's version hashes."""
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        return JSONResponse({"tags": await asyncio.to_thread(_read_tags, entry["path"])})

    @_guarded
    async def put_version_tag(request: Request) -> Response:
        """Label a version, or clear its label with an empty string.

        Free text on purpose: what a version is called — `baseline`, `the one
        that shipped`, `opus probe` — is the reader's judgement, and a fixed
        vocabulary would just be a worse version of the hash.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        version = str(body.get("version") or "").strip()
        label = str(body.get("label") or "").strip()
        if not version:
            raise ValueError("'version' must be a version hash")
        if len(label) > 64:
            raise ValueError("a label is at most 64 characters")

        def work() -> dict[str, Any]:
            tags = _read_tags(entry["path"])
            if label:
                tags[version] = label
            else:
                tags.pop(version, None)
            _write_tags(entry["path"], tags)
            return {"ok": True, "tags": tags}

        return JSONResponse(await asyncio.to_thread(work))

    # ---------------- attachments ---------------- #
    @_guarded
    async def upload_file(request: Request) -> Response:
        """Put a file into the harness's workspace so a turn can reference it.

        The browser sends bytes; the harness gets a *path*. That asymmetry is
        deliberate — hiveloom has no notion of an attachment, but every harness
        with `file_read` can already open a file in its own directory, so the
        honest way to hand one over is to write it where the harness can reach
        it and let the message name it. The journal then records a path that
        still resolves when someone reads the run back a week later, rather
        than a blob that only existed in a browser tab.

        `safe_path` is the same containment chokepoint the file tools and the
        control plane's `input_file` go through: an upload cannot escape the
        harness directory, and cannot land in `.hiveloom/` or on a `.env`.
        """
        entry = await asyncio.to_thread(resolve, request.path_params["harness_id"])
        body = json.loads(await request.body() or b"{}")
        name = str(body.get("name") or "")
        encoded = body.get("content_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("body must carry 'name' and base64 'content_base64'")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"'content_base64' is not valid base64: {exc}") from exc
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise ValueError(
                f"{len(raw)} bytes is over the {_MAX_UPLOAD_BYTES} byte attachment ceiling — "
                "put the file in the harness directory and reference it by path instead"
            )

        def work() -> dict[str, Any]:
            base = Path(entry["path"])
            trace_dir = None
            if entry["ok"]:
                trace_dir = trace_dir_relative_to(
                    base, load_spec(harness_path(base)).logging.trace_dir
                )
            relative = f"{_UPLOAD_SUBDIR}/{_upload_name(name)}"
            try:
                target = safe_path(base, relative, trace_dir=trace_dir)
            except ToolError as exc:
                raise ValueError(str(exc)) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return {
                "ok": True,
                "path": relative,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

        return JSONResponse(await asyncio.to_thread(work), status_code=201)

    @_guarded
    async def get_catalog(request: Request) -> Response:
        """The whole catalog, so the editor offers what the spec actually accepts.

        Catalog-as-truth: the UI must never carry its own list of tool or
        validator names, because it would drift from the one the loader checks.
        """

        def work() -> dict[str, Any]:
            from hiveloom import ext

            ext.ensure_environment_loaded()
            return {
                kind: [entry.model_dump(mode="json") for entry in entries.values()]
                for kind, entries in catalog_mod.CATALOGS.items()
            }

        return JSONResponse({"catalog": await asyncio.to_thread(work)})

    @_guarded
    async def health(request: Request) -> Response:
        """Liveness plus identity, for whatever launched this process.

        The launcher polls this to know the API is up, and compares
        ``version`` with its own: a UI and an API from different releases share
        45 routes and a hand-written type mirror, and disagreeing about them
        produces failures far stranger than a startup message. Deliberately
        cheap -- no catalog scan, no disk walk -- because it is polled.
        """
        return JSONResponse(
            {
                "ok": True,
                "service": "hiveloom-workbench",
                "version": __version__,
                "serves_web": _web_root() is not None,
            }
        )

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/copilot", copilot_info, methods=["GET"]),
        Route("/api/copilot/chat", copilot_chat, methods=["POST"]),
        Route("/api/conversations", list_conversations, methods=["GET"]),
        Route("/api/conversations", create_conversation, methods=["POST"]),
        Route("/api/memories", list_memories, methods=["GET"]),
        Route("/api/memories", create_memory, methods=["POST"]),
        Route("/api/memories/{memory_id}", delete_memory, methods=["DELETE"]),
        Route(
            "/api/conversations/{conversation_id}",
            get_conversation,
            methods=["GET"],
        ),
        Route(
            "/api/conversations/{conversation_id}",
            put_conversation,
            methods=["PUT"],
        ),
        Route(
            "/api/conversations/{conversation_id}",
            delete_conversation,
            methods=["DELETE"],
        ),
        Route("/api/harnesses", list_harnesses, methods=["GET"]),
        Route("/api/harnesses", create_harness, methods=["POST"]),
        Route("/api/catalog", get_catalog, methods=["GET"]),
        Route("/api/providers", list_providers, methods=["GET"]),
        Route("/api/harnesses/{harness_id}", get_harness, methods=["GET"]),
        Route("/api/harnesses/{harness_id}/spec", put_spec, methods=["PUT"]),
        Route("/api/harnesses/{harness_id}/model", put_model, methods=["PUT"]),
        Route("/api/harnesses/{harness_id}/validate", validate_endpoint, methods=["POST"]),
        Route("/api/harnesses/{harness_id}/trust", trust_endpoint, methods=["POST"]),
        Route("/api/harnesses/{harness_id}/run", run_endpoint, methods=["POST"]),
        Route("/api/harnesses/{harness_id}/interface", get_interface, methods=["GET"]),
        Route("/api/harnesses/{harness_id}/runs", list_runs, methods=["GET"]),
        Route("/api/harnesses/{harness_id}/resume", resume_fork, methods=["POST"]),
        Route("/api/harnesses/{harness_id}/tags", list_version_tags, methods=["GET"]),
        Route("/api/harnesses/{harness_id}/tags", put_version_tag, methods=["PUT"]),
        Route("/api/harnesses/{harness_id}/files", upload_file, methods=["POST"]),
        Route("/api/harnesses/{harness_id}/compare", compare_versions, methods=["GET"]),
        Route(
            "/api/harnesses/{harness_id}/evolve/propose", propose_evolution, methods=["POST"]
        ),
        Route("/api/harnesses/{harness_id}/proposals", list_proposals, methods=["GET"]),
        Route("/api/proposals/{proposal_id}", get_proposal, methods=["GET"]),
        Route(
            "/api/harnesses/{harness_id}/proposals/{proposal_id}/apply",
            apply_proposal,
            methods=["POST"],
        ),
        Route("/api/proposals/{proposal_id}/reject", reject_proposal, methods=["POST"]),
        Route("/api/runs/live", list_live_runs, methods=["GET"]),
        Route("/api/runs/{run_id}", get_run, methods=["GET"]),
        Route("/api/runs/{run_id}/context/{seq:int}", materialize_context, methods=["GET"]),
        Route("/api/runs/{run_id}/export", export_run, methods=["GET"]),
        Route("/api/runs/{run_id}/fork", fork_run, methods=["POST"]),
        Route("/api/runs/{run_id}/stop", stop_run, methods=["POST"]),
        Route("/api/runs/{run_id}/messages", list_pending_messages, methods=["GET"]),
        Route("/api/runs/{run_id}/messages", queue_message, methods=["POST"]),
        Route(
            "/api/runs/{run_id}/messages/{message_id}", edit_message, methods=["PATCH"]
        ),
        Route(
            "/api/runs/{run_id}/messages/{message_id}", remove_message, methods=["DELETE"]
        ),
        Route("/api/runs/{run_id}/model", switch_model, methods=["POST"]),
        Route("/api/runs/{run_id}/playbook", switch_playbook, methods=["POST"]),
    ]

    # ---------------- the app itself ---------------- #
    web_root = _web_root()

    async def web(request: Request) -> Response:
        """Serve the built single-page app, when this install carries one.

        Declared last so it can never shadow the API: every ``/api`` route above
        is matched first, and an ``/api`` path that reaches here is a real 404
        that must stay JSON rather than become a page. Everything else falls back
        to ``index.html``, because the client owns its own routing and a deep
        link typed into the address bar has no file behind it.
        """
        relative = request.path_params["path"]
        if relative.startswith("api/") or relative == "api":
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if web_root is None:
            return Response(_NO_WEB_BUILD, status_code=503, media_type="text/plain")
        if relative:
            # Containment, not decoration: the path comes from the URL, so a
            # traversal has to be answered by where it actually resolves to and
            # not by what it looks like.
            asset = (web_root / relative).resolve()
            if web_root in asset.parents and asset.is_file():
                return FileResponse(asset)
        return FileResponse(web_root / "index.html")

    routes.append(Route("/{path:path}", web, methods=["GET"]))
    app = Starlette(routes=routes)
    # The Vite dev server is a different origin (5173) from this API. Loopback
    # only, and this process is a dev tool that never runs in production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def _ingest_entry_traces(hive: Hive, entry: dict[str, Any]) -> None:
    """Ingest journals as data without loading an untrusted harness's hooks."""
    for trace_file in _trace_files(entry["path"]):
        hive.ingest_trace_file(trace_file)


def _trace_files(harness_dir: str) -> list[Path]:
    traces = Path(harness_dir) / ".hiveloom" / "traces"
    if not traces.is_dir():
        return []
    return [p for p in traces.rglob("*.jsonl") if p.is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--dir",
        action="append",
        default=[],
        dest="dirs",
        help="Harness dir to offer beyond the registry (repeatable).",
    )
    parser.add_argument(
        "--scan-dir",
        action="append",
        default=[],
        dest="scan_dirs",
        help="Recursively discover harness.yaml files below this directory.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hiveloom-workbench {__version__}",
    )
    args = parser.parse_args()

    import uvicorn

    # Built before the catalog scan: a missing bundle is a packaging problem the
    # user can act on, and saying so before a slow scan beats saying it after.
    serves_web = _web_root() is not None
    found = _catalog(args.dirs, args.scan_dirs)
    url = f"http://{args.host}:{args.port}"
    banner = [
        f"hiveloom workbench{'' if serves_web else ' api'} on {url}"
        f" — {len(found)} harness(es)"
    ]
    if not serves_web:
        banner.append("no built interface bundled; this process serves /api only")
    if args.host == "127.0.0.1":
        banner.append("(only reachable from this machine — pass --host 0.0.0.0")
        banner.append(" if your browser is elsewhere)")
    # Flushed explicitly: uvicorn.run below never returns, so a buffered stdout
    # would hold the address back for as long as the workbench is up whenever
    # this is piped or captured rather than attached to a terminal.
    print("\n".join(banner), flush=True)
    uvicorn.run(
        build_app(args.dirs, args.scan_dirs),
        host=args.host,
        port=args.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
