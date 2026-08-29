"""Forking a run: re-enter a finished run at one of its model calls.

A journal records *what happened*; a fork turns that back into *somewhere to
start from*. ``hiveloom fork <run_id> --at <seq>`` materialises a new harness
directory holding

* the harness that actually produced the parent run — reconstructed from the
  ``run_started`` snapshot, and checked against the working folder so a fork
  can never silently inherit a harness that has since been edited;
* ``fork.yaml``, the lineage record;
* the folded conversation at that point, ready to be resumed.

Editing the fork's ``harness.yaml`` and resuming re-runs the identical prefix
against a changed harness — the same failure from the turn where it went
wrong, rather than a fresh run that may not reproduce it at all.

``model=`` makes the commonest such edit at fork time: replay this exact
prefix on a different model. Because it rewrites the fork's *spec* rather than
swapping mid-run, the fork is a clean sample of a different harness version —
one variable changed, an identical prefix, and both arms land in their own
fitness bucket instead of one of them being held out as swapped.

**Fork points are model calls.** The folded state immediately before a
``model_call`` is by construction a valid provider request; an arbitrary seq
can land mid-turn, with a dangling ``tool_use`` block and no result, which no
provider accepts. So ``--at`` names a model call, and a seq that isn't one
snaps back to the most recent one.

**Forks live inside the harness they came from**, at
``<harness>/.hiveloom/forks/<name>``. A fork is an experiment on a harness
rather than a harness of its own, so it belongs in that harness's workbench
state: archiving the harness takes its experiments with it, a folder of
harnesses stays a folder of harnesses, and the parent's file tools — rooted at
the harness directory, which they do not descend into ``.hiveloom`` from —
cannot read or mutate the experiment. :func:`fork_target` is the one place
that resolves a fork name to a directory, so the CLI, the workbench and MCP
cannot disagree about where a fork goes.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hiveloom.errors import SpecError
from hiveloom.logging.journal import (
    ContextState,
    read_events,
    state_at_model_call,
    verify_chain,
)
from hiveloom.spec.loader import atomic_write_text

FORK_FILE = "fork.yaml"
CONTEXT_FILE = ".hiveloom/fork-context.json"

# Where a fork belongs: inside the harness it came from, under the protected
# workbench state rather than beside it. A fork is an experiment *on* a
# harness, so keeping it in the harness's own `.hiveloom` means moving,
# archiving or deleting the harness takes its experiments with it, and a
# folder listing shows harnesses rather than harnesses interleaved with the
# forks of harnesses. It also keeps the fork out of reach of the parent's file
# tools, which are rooted at the harness directory but do not descend into
# `.hiveloom`.
FORKS_SUBDIR = Path(".hiveloom") / "forks"
_FORK_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def harness_root(directory: str | Path) -> Path:
    """The original harness a folder belongs to, unwinding any fork nesting.

    Forking a fork would otherwise bury the third generation two levels down,
    and the fourth three — a tree whose depth records nothing anyone asked
    about. Every fork of every generation is a sibling under the harness the
    line started from, so ``.hiveloom/forks`` stays a flat list of experiments
    and ``fork.yaml`` remains the only record of who came from whom.
    """
    current = Path(directory).resolve()
    while True:
        parent = current.parent
        if parent.name == "forks" and parent.parent.name == ".hiveloom":
            candidate = parent.parent.parent
            if (candidate / "harness.yaml").is_file():
                current = candidate
                continue
        return current


def forks_dir(directory: str | Path) -> Path:
    """Where this harness's forks live — its root's ``.hiveloom/forks``."""
    return harness_root(directory) / FORKS_SUBDIR


def owning_harness(directory: str | Path) -> Path | None:
    """The harness that contains ``directory`` as a fork, or None.

    Path containment, not the lineage record: this answers "whose folder is
    this in", which is what a listing needs in order to nest a fork under the
    harness it belongs to even when its ``fork.yaml`` is unreadable.
    """
    root = harness_root(directory)
    return None if root == Path(directory).resolve() else root


def fork_target(directory: str | Path, name: str) -> Path:
    """Resolve a fork name to its directory inside the owning harness.

    The name is slug-checked rather than treated as a path: a fork writes
    files, and callers include a browser and an MCP client, so the one input
    that must never be able to choose a location is a caller-supplied one.
    """
    if not _FORK_NAME_RE.fullmatch(name or ""):
        raise SpecError(
            f"fork name {name!r} must be 1-64 characters of letters, digits, "
            "'.', '_' or '-' — it names a directory inside the harness, not a path"
        )
    return forks_dir(directory) / name


@dataclass
class ForkPoint:
    """One model call in a parent run: somewhere a fork can start."""

    seq: int
    turn: int
    phase: str
    num_messages: int
    timestamp: str

    def label(self) -> str:
        return f"seq {self.seq}  turn {self.turn}  {self.phase}  ({self.num_messages} messages)"


@dataclass
class ForkResult:
    """What :func:`create_fork` produced, and what the operator should know."""

    directory: Path
    parent_run_id: str
    at_seq: int
    turn: int
    messages: int
    warnings: list[str] = field(default_factory=list)
    model_override: dict[str, str] | None = None
    version_hash: str = ""
    trust_inherited: bool = False


def fork_points(events: list[dict[str, Any]]) -> list[ForkPoint]:
    """Every model call in a journal that a fork may start from.

    The compaction turn is excluded: it is an out-of-band summarisation
    request, not a point in the conversation.
    """
    points: list[ForkPoint] = []
    for event in events:
        if event.get("type") != "model_call":
            continue
        payload = event.get("payload") or {}
        if payload.get("inline"):
            continue
        points.append(
            ForkPoint(
                seq=event.get("seq", 0),
                turn=payload.get("turn", 0),
                phase=payload.get("phase", "act"),
                num_messages=payload.get("num_messages", 0),
                timestamp=event.get("timestamp", ""),
            )
        )
    return points


def resolve_fork_point(points: list[ForkPoint], at: int | None) -> ForkPoint:
    """The fork point ``at`` names, snapping back to the previous model call."""
    if not points:
        raise SpecError(
            "this run has no model calls to fork from — it may have halted before "
            "its first turn, or been journalled at logging.level 'summary'"
        )
    if at is None:
        return points[-1]
    exact = next((p for p in points if p.seq == at), None)
    if exact is not None:
        return exact
    earlier = [p for p in points if p.seq < at]
    if not earlier:
        raise SpecError(
            f"seq {at} is before the run's first model call (seq {points[0].seq}). "
            f"Fork points: {', '.join(str(p.seq) for p in points)}"
        )
    return earlier[-1]


def _snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    started = next((e for e in events if e.get("type") == "run_started"), None)
    if started is None:
        raise SpecError("journal has no run_started event; it cannot be forked")
    snapshot = (started.get("payload") or {}).get("harness")
    if not isinstance(snapshot, dict) or "spec" not in snapshot:
        raise SpecError(
            "this run was journalled before hiveloom 1.0 and carries no harness "
            "snapshot, so the harness that produced it cannot be reconstructed. "
            "Re-run it on 1.0 to get a forkable journal."
        )
    return snapshot


def _redaction_warning(state: ContextState) -> str | None:
    """Redaction is applied before persistence, so a fork replays the marker."""
    blob = json.dumps(state.messages) + state.system
    if "[REDACTED]" not in blob:
        return None
    return (
        "the replayed prefix contains [REDACTED] spans: logging.redact scrubbed "
        "them before they were persisted, so the fork will send the literal "
        "marker to the model where the original run sent a real value"
    )


def _materialize_files(
    snapshot: dict[str, Any],
    source: Path | None,
    target: Path,
    *,
    allow_drift: bool,
) -> list[str]:
    """Write the harness's behavioural files, verifying them against the manifest."""
    warnings: list[str] = []
    manifest: dict[str, str] = snapshot.get("files") or {}
    contents: dict[str, str] = snapshot.get("contents") or {}
    import hashlib

    for relative, expected in manifest.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        if relative in contents:
            # The journal carries the body (logging.snapshot_files): portable,
            # and authoritative — it is what actually ran.
            destination.write_text(contents[relative], encoding="utf-8")
            continue

        if source is None:
            warnings.append(
                f"{relative} is in the manifest but neither inlined in the journal "
                "nor available from a source folder; the fork is missing it"
            )
            continue

        origin = source / relative
        if not origin.is_file():
            warnings.append(f"{relative} is missing from {source}; the fork is missing it")
            continue
        raw = origin.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected and expected != "<missing>":
            message = (
                f"{relative} has changed since the parent run "
                f"(manifest {expected[:12]}, working copy {actual[:12]})"
            )
            if not allow_drift:
                raise SpecError(
                    message
                    + ". The fork would not reproduce the parent. Re-run with "
                    "--allow-drift to fork against the current file anyway, or use "
                    "a harness whose files still match."
                )
            warnings.append(message + " — forked against the working copy")
        destination.write_bytes(raw)
    return warnings


def find_harness_source(trace_path: str | Path) -> Path | None:
    """Walk up from a trace file to the harness folder that owns it."""
    current = Path(trace_path).resolve().parent
    for candidate in [current, *current.parents]:
        if (candidate / "harness.yaml").is_file():
            return candidate
    return None


def create_fork(
    trace_path: str | Path,
    target_dir: str | Path,
    *,
    at: int | None = None,
    source_dir: str | Path | None = None,
    allow_drift: bool = False,
    model: str | None = None,
    model_provider: str | None = None,
) -> ForkResult:
    """Materialise a forkable harness directory from a parent run's journal.

    ``model``/``model_provider`` rewrite the fork's declared model through the
    construct API, so the change is validated and rolled back like any other
    spec edit — and so the fork carries its own harness version hash.
    """
    events = read_events(trace_path)
    if not events:
        raise SpecError(f"journal at {trace_path} is empty")

    warnings: list[str] = []
    chain = verify_chain(trace_path)
    if not chain.ok:
        raise SpecError(
            f"the parent journal's hash chain is broken ({chain.reason}). "
            "Forking from a journal that has been edited would produce a run "
            "whose lineage claims something untrue."
        )
    if not chain.chained:
        warnings.append("the parent journal is unchained (pre-1.0); it cannot be verified")

    point = resolve_fork_point(fork_points(events), at)
    if at is not None and point.seq != at:
        warnings.append(f"seq {at} is not a model call; snapped back to seq {point.seq}")

    snapshot = _snapshot(events)
    state = state_at_model_call(events, point.seq)
    redaction = _redaction_warning(state)
    if redaction:
        warnings.append(redaction)

    target = Path(target_dir)
    if target.exists() and any(target.iterdir()):
        raise SpecError(f"{target} already exists and is not empty")
    target.mkdir(parents=True, exist_ok=True)

    source = Path(source_dir) if source_dir is not None else find_harness_source(trace_path)
    warnings.extend(_materialize_files(snapshot, source, target, allow_drift=allow_drift))

    # The spec comes from the journal, never from the folder: what ran is what
    # gets forked, even if the folder has moved on since.
    atomic_write_text(target / "harness.yaml", snapshot["spec"])

    trust_inherited = _inherit_trust(source, snapshot, target)
    if not trust_inherited:
        warnings.append(
            f"the fork is not trusted; run `hiveloom trust {target}` before "
            "resuming it, since its code hooks would run with your permissions"
        )

    override = _apply_model_override(target, model, model_provider)
    version_hash = _fork_version_hash(target)

    envelope = events[0]
    lineage = {
        "parent_run_id": envelope.get("run_id", ""),
        "parent_harness_version_hash": snapshot.get("version_hash", ""),
        "at_seq": point.seq,
        "at_turn": point.turn,
        "parent_line_hash": _line_hash(trace_path, point.seq),
        "created_at": datetime.now(UTC).isoformat(),
        "context_file": CONTEXT_FILE,
        "harness_version_hash": version_hash,
    }
    if override is not None:
        lineage["model_override"] = override
    atomic_write_text(
        target / FORK_FILE,
        yaml.dump(lineage, sort_keys=False, default_flow_style=False),
    )
    context_path = target / CONTEXT_FILE
    context_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        context_path,
        json.dumps({"system": state.system, "messages": state.messages}, indent=2),
    )

    return ForkResult(
        directory=target,
        parent_run_id=lineage["parent_run_id"],
        at_seq=point.seq,
        turn=point.turn,
        messages=len(state.messages),
        warnings=warnings,
        model_override=override,
        version_hash=version_hash,
        trust_inherited=trust_inherited,
    )


def _inherit_trust(source: Path | None, snapshot: dict[str, Any], target: Path) -> bool:
    """Trust the fork only when its code came from an already-trusted folder.

    A fork is the same code at a new path, and trust is keyed on the path — so
    re-asking for a folder hiveloom just built out of a harness the operator
    already trusted is friction without safety. But that reasoning holds only
    for files taken from that folder and checked against the manifest.

    When the bodies came from the journal's inlined ``contents`` instead, they
    are whatever the journal says they are. A journal is a file someone can
    hand you, and its hash chain proves internal consistency, not provenance —
    so inheriting trust there would turn "read this run" into "run this
    stranger's code". Those forks stay untrusted and say so.
    """
    if source is None or snapshot.get("contents"):
        return False
    from hiveloom import trust

    if not trust.is_trusted(source):
        return False
    trust.record_trust(target)
    return True


def _apply_model_override(
    target: Path, model: str | None, provider: str | None
) -> dict[str, str] | None:
    """Rewrite the fork's model through the construct API, or do nothing.

    Routed through ``construct.set_model`` rather than a direct YAML write:
    ``model.provider`` and ``model.id`` validate against each other, so they
    have to move in one committed, rolled-back-on-error step. A rejected
    override removes the half-built fork rather than leaving one behind that
    claims a model it does not have.
    """
    if not model and not provider:
        return None

    from hiveloom import construct
    from hiveloom.spec.loader import load_spec

    spec = load_spec(target / "harness.yaml")
    before = f"{spec.model.provider}:{spec.model.id}"
    target_provider = provider or spec.model.provider
    target_model = model or spec.model.id
    try:
        construct.set_model(target, f"{target_provider}/{target_model}")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"from": before, "provider": target_provider, "model": target_model}


def _fork_version_hash(target: Path) -> str:
    """The fork's own harness version hash — the bucket its runs will land in."""
    from hiveloom.logging.trace import spec_version_hash
    from hiveloom.spec.loader import load_spec

    try:
        return spec_version_hash(load_spec(target / "harness.yaml"), target)
    except Exception:  # noqa: BLE001 - a missing manifest file is already warned about
        return ""


def _line_hash(trace_path: str | Path, seq: int) -> str:
    """sha256 of the journal line at ``seq`` — pins the fork to an exact point."""
    import hashlib

    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("seq") == seq:
            return hashlib.sha256(line.encode("utf-8")).hexdigest()
    return ""


def load_fork(directory: str | Path) -> dict[str, Any] | None:
    """Read a fork's lineage record, or None if this is not a fork directory."""
    path = Path(directory) / FORK_FILE
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def parent_version_hash(directory: str | Path) -> str:
    """The harness version the parent run executed, from a fork's lineage record.

    A fork's runs land under the fork's own version hash, so a fork that has
    not been resumed yet carries no evidence — while the failures that
    motivated it sit under the parent's. This is how a caller asks for those
    instead: ``hiveloom evolve --from-parent`` and the workbench's equivalent
    both come through here, so the two cannot disagree about which version
    "the parent" means.
    """
    record = load_fork(directory)
    if record is None:
        raise SpecError(
            f"{directory} is not a fork directory (no {FORK_FILE}); "
            "make one with `hiveloom fork <run_id> --at <seq>`"
        )
    parent = record.get("parent_harness_version_hash")
    if not parent:
        raise SpecError(
            f"{Path(directory) / FORK_FILE} names no parent version, so there is "
            "no parent to analyse — it was written by a hiveloom that did not "
            "record one. Evolve it normally once it has runs of its own."
        )
    return str(parent)


def load_fork_context(directory: str | Path) -> list[dict[str, Any]]:
    """The folded conversation a fork resumes from."""
    record = load_fork(directory)
    if record is None:
        raise SpecError(f"{directory} is not a fork directory (no {FORK_FILE})")
    path = Path(directory) / record.get("context_file", CONTEXT_FILE)
    if not path.is_file():
        raise SpecError(f"fork context file is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise SpecError(f"fork context file is malformed: {path}")
    return messages
