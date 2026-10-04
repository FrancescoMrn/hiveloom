"""A research program on disk: its folder, charter, state, and how it starts.

A program lives beside the harness it works on, nested like forks:

    <harness>/.hiveloom/research/<name>/
        research.yaml        the charter (the user's; never written by the engine)
        ledger.jsonl         hash-chained record of everything
        state.json           engine-written only
        candidates/<id>/     full copies of the harness, one per version tried
        director/            the director's own journals
        replay.jsonl         recorded tool results, when a tool is replayed
        report.md            generated at the end

Every candidate carries a program-scoped harness ``id`` so the program's runs
sit under their own Hive key: they never mix with the live harness's
production runs, and the live harness's evidence never leaks into the
program's measurements. Promotion applies only the director's changes to the
live harness, whose ``id`` is untouched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hiveloom import construct, trust
from hiveloom.errors import HiveloomError, SpecError
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import harness_path, load_spec
from hiveloom.spec.schema import ALWAYS_FROZEN

from .budget import Budget
from .charter import Charter, check_program_name, load_charter
from .execution import ReplayStore, resolve
from .ledger import Ledger

_COPY_IGNORE = shutil.ignore_patterns(".hiveloom", ".venv", "__pycache__", "*.pyc", "out")
MIN_WORKING_CASES = 2


def program_key(identity: str, name: str) -> str:
    """The Hive key a program's runs live under: readable, and never reused.

    Unique per program instance (a nonce), so a program deleted and started
    again under the same name, or two names that share a long prefix, never
    read each other's runs.
    """
    import uuid

    nonce = uuid.uuid4().hex[:8]
    return f"{identity[:32]}-r-{name[:18]}-{nonce}"


def program_root(harness_dir: str | Path, name: str) -> Path:
    base = harness_path(harness_dir).parent
    return base / ".hiveloom" / "research" / check_program_name(name)


class ProgramBusy(HiveloomError):
    """Another process (or thread) is advancing this program right now."""


class Program:
    """Paths, charter, ledger, budget and state of one program."""

    def __init__(self, harness_dir: str | Path, name: str):
        self.base = harness_path(harness_dir).parent
        self.name = check_program_name(name)
        self.root = program_root(self.base, name)
        if not (self.root / "research.yaml").exists():
            raise SpecError(f"no research program '{name}' in {self.base}")
        self.charter: Charter = load_charter(self.root / "research.yaml")
        self.ledger = Ledger(self.root / "ledger.jsonl")
        self.budget = Budget(self.charter, self.ledger)

    # -- files -------------------------------------------------------------- #
    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def candidate_dir(self, candidate_id: str) -> Path:
        return self.root / "candidates" / candidate_id

    @property
    def director_dir(self) -> Path:
        return self.root / "director"

    @property
    def replay(self) -> ReplayStore:
        return ReplayStore(self.root / "replay.jsonl")

    @contextmanager
    def lock(self):
        """Exclusive right to advance the program; ProgramBusy if someone has it."""
        from .ledger import exclusive

        path = self.root / ".lock"
        with path.open("a+") as handle, exclusive(handle, blocking=False) as held:
            if not held:
                raise ProgramBusy(f"research program '{self.name}' is being advanced by "
                                  "another process; wait for it or stop it")
            yield

    # A stop can arrive while a unit runs (from another process); it lives in
    # its own file so the running unit's save of state.json cannot erase it.
    @property
    def stop_path(self) -> Path:
        return self.root / "stop_request.json"

    def request_stop(self, reason: str) -> None:
        tmp = self.stop_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"reason": reason}), encoding="utf-8")
        tmp.replace(self.stop_path)

    def stop_requested(self) -> str | None:
        if self.stop_path.exists():
            return json.loads(self.stop_path.read_text(encoding="utf-8")).get("reason") \
                or "requested"
        # Programs stopped before stop requests had their own file.
        return self.load_state().get("stop_requested")

    def load_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = datetime.now(UTC).isoformat()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    def eval_path(self, candidate_id: str) -> Path:
        if self.charter.concepts_mode:
            from .evaluation import EVAL_FILE

            return self.candidate_dir(candidate_id) / EVAL_FILE
        return self.candidate_dir(candidate_id) / self.charter.eval

    def version_of(self, candidate_id: str) -> str:
        directory = self.candidate_dir(candidate_id)
        return spec_version_hash(load_spec(directory), directory)

    def key(self) -> str:
        return load_spec(self.candidate_dir("c0")).identity

    # -- candidates --------------------------------------------------------- #
    def copy_candidate(self, source: Path, candidate_id: str) -> Path:
        target = self.candidate_dir(candidate_id)
        if target.exists():
            raise SpecError(f"candidate {candidate_id} already exists")
        shutil.copytree(source, target, ignore=_COPY_IGNORE)
        # The base folder is trusted (init checks it), and a candidate is a copy
        # the engine made of it, under the program the user started.
        trust.record_trust(target)
        return target


def list_programs(harness_dir: str | Path) -> list[dict[str, Any]]:
    """Every program of a harness, with where it stands."""
    parent = harness_path(harness_dir).parent / ".hiveloom" / "research"
    programs = []
    for root in sorted(parent.iterdir()) if parent.is_dir() else []:
        state_path = root / "state.json"
        if not state_path.exists():
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        programs.append({key: state.get(key) for key in (
            "name", "status", "unit", "round", "incumbent", "started_at", "updated_at")})
    return programs


def _init_concepts(base: Path, name: str, charter: Charter, spec, policy) -> Program:
    """A program that builds its own evaluation: it starts at ``frame``."""
    from .concepts import seed_program

    root = program_root(base, name)
    root.mkdir(parents=True)
    (root / "research.yaml").write_text(
        yaml.safe_dump(charter.model_dump(mode="json", exclude_none=True), sort_keys=False),
        encoding="utf-8",
    )
    program = Program(base, name)
    c0 = program.copy_candidate(base, "c0")
    construct.set_value(c0, "id", program_key(spec.identity, name))
    if spec.delegation.enabled:
        # Research runs never hand work to a peer: its tools would run outside
        # the research execution policy. The live harness keeps its delegation.
        construct.set_value(c0, "delegation.enabled", False)
    seeds = seed_program(root, base, charter.concepts or "", charter.seeds)
    program.ledger.append(
        "program_started", name=name, mode="concepts",
        base_version=spec_version_hash(spec, base),
        delegation_disabled=spec.delegation.enabled, base_identity=spec.identity,
        program_identity=load_spec(c0).identity, seeds=seeds,
        policy={"tools": policy.tools, "mcp": policy.mcp},
        charter=charter.model_dump(mode="json", exclude_none=True),
    )
    program.save_state(_initial_state(name, program, "frame", {"working": [], "holdout": []}))
    return program


def _initial_state(name: str, program: Program, unit: str,
                   split: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "name": name, "status": "active", "unit": unit, "round": 0,
        "started_at": datetime.now(UTC).isoformat(), "incumbent": "c0",
        "candidates": {"c0": {"parent": None, "changes": [], "hypothesis": None,
                              "version": program.version_of("c0")}},
        "split": split, "hypotheses": [], "experiments": [], "pending_experiments": [],
        "findings": [], "calibration": [], "rounds_without_progress": 0, "cost_model": None,
        "stop_reason": None, "confirmation": None, "promotion": None,
    }


def _model_problems(charter: Charter) -> list[str]:
    """Every model the charter names must be one the model registry knows.

    Checked here, before anything runs: an unknown director otherwise surfaces
    minutes later, in the middle of the first round. (The harness's extensions
    are loaded by then, so its own providers count.)
    """
    from pydantic import ValidationError

    from hiveloom.spec.schema import ModelConfig

    named = [("models.director", charter.models.director)]
    if charter.models.examiner:
        named.append(("models.examiner", charter.models.examiner))
    named += [(f"models.judges.{i}", judge) for i, judge in enumerate(charter.models.judges)]
    problems = []
    for where, selector in named:
        provider, _, model_id = selector.partition("/")
        try:
            ModelConfig(provider=provider, id=model_id)
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"].removeprefix("Value error, ")
            problems.append(f"{where} '{selector}': {reason}")
    return problems


def _covered(path: str, patterns: list[str]) -> bool:
    return any(path == p or path.startswith(p + ".") for p in patterns)


def _split(eval_id: str, case_ids: list[str], holdout: float) -> tuple[list[str], list[str]]:
    """Deterministic working / held-out split, seeded by the eval's identity."""
    ranked = sorted(
        case_ids, key=lambda cid: hashlib.sha256(f"{eval_id}:{cid}".encode()).hexdigest()
    )
    sealed = round(len(ranked) * holdout)
    if holdout > 0 and sealed == 0 and len(ranked) > MIN_WORKING_CASES:
        sealed = 1
    return sorted(ranked[sealed:]), sorted(ranked[:sealed])


def init_program(
    harness_dir: str | Path,
    name: str,
    charter: Charter | dict[str, Any] | str | Path,
    *,
    approve_trust=None,
    hive_path: str | Path | None = None,
) -> Program:
    """Create a program: validate everything, freeze the split, copy the base as c0."""
    from hiveloom.evals import resolve_eval_spec
    from hiveloom.logging.hive import Hive

    base = harness_path(harness_dir).parent
    trust.ensure_trusted(base, approve_trust)
    root = program_root(base, name)
    if root.exists():
        raise SpecError(f"research program '{name}' already exists in {base}")

    if isinstance(charter, (str, Path)):
        charter = load_charter(charter)
    elif isinstance(charter, dict):
        charter = Charter.model_validate(charter)

    spec = load_spec(base)
    problems: list[str] = []
    frozen = [*ALWAYS_FROZEN, *spec.evolution.frozen]
    for lever in charter.levers:
        if _covered(lever, ["delegation"]) or _covered("delegation", [lever]):
            problems.append(f"lever '{lever}' reaches delegation: a peer runs its own tools, "
                            "outside the research execution policy")
        if not _covered(lever, spec.evolution.mutable):
            problems.append(f"lever '{lever}' is not in this harness's evolution.mutable")
        if any(_covered(lever, [f]) or _covered(f, [lever]) for f in frozen):
            problems.append(f"lever '{lever}' touches a frozen path")
    if not charter.concepts_mode:
        eval_file = base / charter.eval
        if not eval_file.exists():
            problems.append(f"eval '{charter.eval}' not found in the harness folder")
    elif charter.seeds and not (base / charter.seeds).is_file():
        problems.append(f"seeds file '{charter.seeds}' not found in the harness folder")
    policy = resolve(spec, charter.execution)
    problems.extend(policy.errors)
    problems.extend(_model_problems(charter))
    if problems:
        raise SpecError("cannot start the research program:\n  - " + "\n  - ".join(problems))
    if charter.concepts_mode:
        return _init_concepts(base, name, charter, spec, policy)

    validated, cases = resolve_eval_spec(eval_file, approve_trust=lambda _p: True)
    eval_harness = (
        validated.harness_path if validated.harness_path.is_dir()
        else validated.harness_path.parent
    )
    if eval_harness.resolve() != base.resolve():
        raise SpecError(
            f"eval {charter.eval} runs {eval_harness}, not this harness; a program must "
            "measure the harness it changes (use 'harness: .')"
        )
    working, holdout = _split(validated.identity.eval_id, [c.id for c in cases], charter.holdout)
    if len(working) < MIN_WORKING_CASES:
        raise SpecError(
            f"the eval has {len(cases)} case(s); after the {charter.holdout:.0%} holdout only "
            f"{len(working)} remain to work on (need {MIN_WORKING_CASES}). Add cases or "
            "lower 'holdout'."
        )

    root.mkdir(parents=True)
    (root / "research.yaml").write_text(
        yaml.safe_dump(charter.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    program = Program(base, name)
    c0 = program.copy_candidate(base, "c0")
    construct.set_value(c0, "id", program_key(spec.identity, name))
    if spec.delegation.enabled:
        # Research runs never hand work to a peer: its tools would run outside
        # the research execution policy. The live harness keeps its delegation.
        construct.set_value(c0, "delegation.enabled", False)

    replayed = policy.replayed()
    recordings = 0
    if replayed:
        with Hive(hive_path) as hive:
            traces = [
                row["trace_path"]
                for row in hive._conn.execute(
                    "SELECT trace_path FROM runs WHERE harness_key=? AND trace_path IS NOT NULL",
                    (spec.identity,),
                )
            ]
        recordings = program.replay.build(traces, replayed)

    program.ledger.append(
        "program_started",
        name=name,
        base_version=spec_version_hash(spec, base),
        delegation_disabled=spec.delegation.enabled,
        base_identity=spec.identity,
        program_identity=load_spec(c0).identity,
        eval_id=validated.identity.eval_id,
        working=len(working),
        holdout=len(holdout),
        policy={"tools": policy.tools, "mcp": policy.mcp},
        replay_recordings=recordings,
        charter=charter.model_dump(mode="json"),
    )
    now = datetime.now(UTC).isoformat()
    program.save_state(
        {
            "name": name,
            "status": "active",
            "unit": "baseline",
            "round": 0,
            "started_at": now,
            "incumbent": "c0",
            "candidates": {"c0": {"parent": None, "changes": [], "hypothesis": None,
                                  "version": program.version_of("c0")}},
            "split": {"working": working, "holdout": holdout},
            "hypotheses": [],
            "experiments": [],
            "pending_experiments": [],
            "findings": [],
            "calibration": [],
            "rounds_without_progress": 0,
            "cost_model": None,
            "stop_reason": None,
            "confirmation": None,
            "promotion": None,
        }
    )
    return program
