"""Managed retention for raw Hiveloom journal files.

Retention is deliberately narrower than arbitrary file cleanup. A root must
carry Hiveloom's marker, every candidate must be a direct, non-symlinked JSONL
file whose first event identifies the same run as its filename, and dry-run
planning never mutates the root.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveloom.errors import SpecError

if TYPE_CHECKING:
    from hiveloom.logging.hive import Hive
    from hiveloom.spec.schema import RetentionConfig

TRACE_ROOT_MARKER = ".hiveloom-trace-root"
_MARKER_CONTENT = "hiveloom-trace-root-v1\n"
_FIRST_EVENT_LIMIT = 1024 * 1024


def _safe_root(trace_root: str | Path) -> Path:
    supplied = Path(trace_root).expanduser()
    if supplied.is_symlink():
        raise SpecError(f"managed trace root cannot be a symlink: {supplied}")
    root = supplied.resolve()
    anchor = Path(root.anchor)
    if root == anchor or root == Path.home().resolve():
        raise SpecError(f"refusing broad managed trace root: {root}")
    return root


def ensure_trace_root(trace_root: str | Path) -> Path:
    """Create or verify the marker that scopes future retention deletes."""
    root = _safe_root(trace_root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / TRACE_ROOT_MARKER
    if marker.is_symlink():
        raise SpecError(f"trace root marker cannot be a symlink: {marker}")
    if not marker.exists():
        # Publish a fully written inode. Opening the final name with ``x`` makes
        # an empty file visible before write(), so a concurrent TraceWriter can
        # read a partial marker and reject a valid managed root.
        temporary = root / f"{TRACE_ROOT_MARKER}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(_MARKER_CONTENT)
            try:
                os.link(temporary, marker)
            except FileExistsError:
                pass  # another writer atomically published the same marker
        finally:
            temporary.unlink(missing_ok=True)
    if marker.is_symlink():
        raise SpecError(f"trace root marker cannot be a symlink: {marker}")
    try:
        content = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read trace root marker at {marker}: {exc}") from exc
    if content != _MARKER_CONTENT:
        raise SpecError(f"invalid trace root marker at {marker}") from None
    return root


def validate_trace_root(trace_root: str | Path) -> Path:
    """Return a deletion-safe trace root or refuse it without mutation."""
    root = _safe_root(trace_root)
    marker = root / TRACE_ROOT_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise SpecError(
            f"{root} is not a managed Hiveloom trace root (missing {TRACE_ROOT_MARKER})"
        )
    try:
        content = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read trace root marker at {marker}: {exc}") from exc
    if content != _MARKER_CONTENT:
        raise SpecError(f"invalid trace root marker at {marker}")
    return root


@dataclass(frozen=True)
class TraceFile:
    path: Path
    run_id: str
    size: int
    modified_at: datetime

    def to_dict(self, *, reasons: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": str(self.path),
            "run_id": self.run_id,
            "size": self.size,
            "modified_at": self.modified_at.isoformat(),
        }
        if reasons is not None:
            result["reasons"] = reasons
        return result


@dataclass
class TraceRetentionPlan:
    root: Path
    policy: dict[str, Any]
    files: list[TraceFile]
    selected: dict[Path, list[str]]
    ignored: list[dict[str, str]]
    preserved: set[Path]
    limits_satisfied: bool
    applied: bool = False
    deleted: list[str] = field(default_factory=list)
    cleanup_errors: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        selected_files = [item for item in self.files if item.path in self.selected]
        kept_files = [item for item in self.files if item.path not in self.selected]
        return {
            "root": str(self.root),
            "policy": self.policy,
            "scanned_runs": len(self.files),
            "scanned_bytes": sum(item.size for item in self.files),
            "selected_runs": len(selected_files),
            "selected_bytes": sum(item.size for item in selected_files),
            "remaining_runs": len(kept_files),
            "remaining_bytes": sum(item.size for item in kept_files),
            "limits_satisfied": self.limits_satisfied,
            "selected": [
                item.to_dict(reasons=self.selected[item.path]) for item in selected_files
            ],
            "preserved": sorted(str(path) for path in self.preserved),
            "ignored": self.ignored,
            "applied": self.applied,
            "deleted": self.deleted,
            "cleanup_errors": self.cleanup_errors,
        }


def _journal_identity(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline(_FIRST_EVENT_LIMIT + 1)
    except OSError as exc:
        return None, f"cannot read: {exc}"
    if len(first) > _FIRST_EVENT_LIMIT or not first.endswith("\n"):
        return None, "first journal event is missing or too large"
    try:
        event = json.loads(first)
    except json.JSONDecodeError:
        return None, "first line is not JSON"
    if not isinstance(event, dict) or not isinstance(event.get("run_id"), str):
        return None, "first event has no run_id"
    run_id = event["run_id"]
    if path.name != f"{run_id}.jsonl":
        return None, "filename does not match the journal run_id"
    return run_id, None


def plan_trace_retention(
    trace_root: str | Path,
    policy: RetentionConfig,
    *,
    now: datetime | None = None,
    preserve: list[str | Path] | tuple[str | Path, ...] = (),
) -> TraceRetentionPlan:
    """Select age/count/byte candidates without changing files or the Hive."""
    root = validate_trace_root(trace_root)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    preserved: set[Path] = set()
    for value in preserve:
        candidate = Path(value).expanduser().resolve()
        if candidate.parent != root:
            raise SpecError(f"preserved trace is outside the managed root: {candidate}")
        preserved.add(candidate)

    files: list[TraceFile] = []
    ignored: list[dict[str, str]] = []
    if root.exists():
        for path in sorted(root.glob("*.jsonl")):
            if path.is_symlink():
                raise SpecError(f"retention refuses symlinked trace files: {path}")
            resolved = path.resolve()
            if resolved.parent != root:
                raise SpecError(f"retention candidate escaped the managed root: {path}")
            run_id, reason = _journal_identity(resolved)
            if run_id is None:
                ignored.append({"path": str(resolved), "reason": reason or "not a journal"})
                continue
            stat = resolved.stat()
            files.append(
                TraceFile(
                    path=resolved,
                    run_id=run_id,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )

    files.sort(key=lambda item: (item.modified_at, item.run_id))
    selected: dict[Path, list[str]] = {}

    def select(item: TraceFile, reason: str) -> None:
        if item.path not in preserved:
            selected.setdefault(item.path, []).append(reason)

    if policy.days is not None:
        cutoff = reference.astimezone(UTC) - timedelta(days=policy.days)
        for item in files:
            if item.modified_at < cutoff:
                select(item, f"older_than_{policy.days}_days")

    if policy.max_runs is not None:
        remaining = [item for item in files if item.path not in selected]
        excess = max(0, len(remaining) - policy.max_runs)
        for item in remaining:
            if excess == 0:
                break
            if item.path in preserved:
                continue
            select(item, f"over_{policy.max_runs}_runs")
            excess -= 1

    if policy.max_bytes is not None:
        remaining_bytes = sum(item.size for item in files if item.path not in selected)
        for item in files:
            if remaining_bytes <= policy.max_bytes:
                break
            if item.path in selected or item.path in preserved:
                continue
            select(item, f"over_{policy.max_bytes}_bytes")
            remaining_bytes -= item.size

    kept = [item for item in files if item.path not in selected]
    limits_satisfied = True
    if policy.days is not None:
        cutoff = reference.astimezone(UTC) - timedelta(days=policy.days)
        limits_satisfied = limits_satisfied and all(item.modified_at >= cutoff for item in kept)
    if policy.max_runs is not None:
        limits_satisfied = limits_satisfied and len(kept) <= policy.max_runs
    if policy.max_bytes is not None:
        limits_satisfied = limits_satisfied and sum(item.size for item in kept) <= policy.max_bytes

    return TraceRetentionPlan(
        root=root,
        policy=policy.model_dump(mode="json", exclude_none=True),
        files=files,
        selected=selected,
        ignored=ignored,
        preserved=preserved,
        limits_satisfied=limits_satisfied,
    )


def apply_trace_retention(
    plan: TraceRetentionPlan, *, hive: Hive | None = None
) -> TraceRetentionPlan:
    """Atomically hide selected journals, update Hive references, then unlink.

    Readers that already hold a file descriptor can finish. A failed rename or
    Hive update restores every original path before raising.
    """
    validate_trace_root(plan.root)
    moved: list[tuple[TraceFile, Path]] = []
    try:
        for item in plan.files:
            if item.path not in plan.selected:
                continue
            if item.path.is_symlink() or item.path.resolve().parent != plan.root:
                raise SpecError(f"retention candidate is no longer safe: {item.path}")
            tombstone = plan.root / f".{item.path.name}.pruning-{uuid.uuid4().hex}"
            os.replace(item.path, tombstone)
            moved.append((item, tombstone))
        if hive is not None:
            hive.mark_traces_pruned(
                [(item.run_id, str(item.path)) for item, _ in moved],
                pruned_at=datetime.now(UTC).isoformat(),
            )
    except Exception:
        for item, tombstone in reversed(moved):
            if tombstone.exists() and not item.path.exists():
                os.replace(tombstone, item.path)
        raise

    for item, tombstone in moved:
        try:
            tombstone.unlink()
            plan.deleted.append(str(item.path))
        except OSError as exc:
            # The public trace path is already gone and the Hive is consistent.
            # Keep the hidden tombstone for a later maintenance pass.
            plan.cleanup_errors.append({"path": str(tombstone), "error": str(exc)})
    plan.applied = True
    return plan


def prune_trace_root(
    trace_root: str | Path,
    policy: RetentionConfig,
    *,
    hive: Hive | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    preserve: list[str | Path] | tuple[str | Path, ...] = (),
) -> TraceRetentionPlan:
    """Plan retention and optionally apply the exact plan."""
    plan = plan_trace_retention(trace_root, policy, now=now, preserve=preserve)
    return plan if dry_run else apply_trace_retention(plan, hive=hive)
