"""Evolver: propose, gate, and apply harness mutations.

Safety invariants (enforced here, in code — not by convention):

* The evolver can never modify ``guardrails``, ``model``, or ``logging.redact``
  (``schema.ALWAYS_FROZEN``), nor any path the harness lists as ``frozen``.
* A proposed change must fall within the harness's ``mutable`` set.
* Code-hook regeneration always requires explicit human approval.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.errors import HiveloomError
from hiveloom.evolve.analyzer import FailureReport
from hiveloom.generate.llm import StrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import (
    dump_spec,
    harness_path,
    load_raw,
    load_spec,
    spec_from_dict,
    validate_harness,
)
from hiveloom.spec.schema import ALWAYS_FROZEN, HarnessSpec

_PROMPT_PATH = Path(__file__).parent / "prompts" / "evolve_contract.md"
_COUNTER_RE = re.compile(r"^#\s*evolved:\s*(\d+)", re.MULTILINE)


class ProposalError(HiveloomError):
    """Raised when a mutation proposal is malformed."""


class YamlChange(BaseModel):
    path: str
    value: Any
    rationale: str = ""


class CodeChange(BaseModel):
    file: str
    source: str
    rationale: str = ""


class MutationProposal(BaseModel):
    rationale: str = ""
    yaml_changes: list[YamlChange] = Field(default_factory=list)
    code_changes: list[CodeChange] = Field(default_factory=list)


class GateResult(BaseModel):
    accepted: list[YamlChange] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)  # {path, reason}
    code_changes: list[CodeChange] = Field(default_factory=list)


class ApplyResult(BaseModel):
    changed: bool
    old_version_hash: str
    new_version_hash: str
    counter: int
    rationale: str = ""
    applied_yaml: list[YamlChange] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)
    applied_code: list[str] = Field(default_factory=list)
    pending_code: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Propose
# --------------------------------------------------------------------------- #
def build_evolve_prompt(spec: HarnessSpec, report: FailureReport) -> tuple[str, str]:
    """Return (system, user) prompts for the proposing model."""
    system = _PROMPT_PATH.read_text(encoding="utf-8").replace(
        "{always_frozen}", ", ".join(ALWAYS_FROZEN)
    )
    user = (
        "Current harness spec (YAML):\n"
        f"{dump_spec(spec)}\n"
        f"Mutable paths: {spec.evolution.mutable}\n"
        f"Frozen paths: {spec.evolution.frozen}\n\n"
        "Failure report (JSON):\n"
        f"{report.model_dump_json(indent=2)}\n\n"
        "Return the mutation proposal JSON."
    )
    return system, user


def parse_proposal(text: str) -> MutationProposal:
    """Parse a mutation proposal from model text (tolerating code fences)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"proposal is not valid JSON: {exc}") from exc
    try:
        return MutationProposal.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - pydantic validation error → actionable message
        raise ProposalError(f"malformed proposal: {exc}") from exc


def propose(spec: HarnessSpec, report: FailureReport, model: StrongModel) -> MutationProposal:
    """Ask the strong model for a mutation proposal."""
    system, user = build_evolve_prompt(spec, report)
    return parse_proposal(model.generate(system=system, user=user))


# --------------------------------------------------------------------------- #
# Gate (frozen-path enforcement)
# --------------------------------------------------------------------------- #
def _covered(path: str, patterns: set[str]) -> bool:
    return any(path == p or path.startswith(p + ".") for p in patterns)


def gate(spec: HarnessSpec, proposal: MutationProposal) -> GateResult:
    """Split proposed YAML changes into accepted and rejected (frozen/non-mutable).

    Code changes pass through unchanged — they are gated separately by human
    approval at apply time.
    """
    frozen = set(spec.evolution.frozen) | set(ALWAYS_FROZEN)
    mutable = set(spec.evolution.mutable)
    accepted: list[YamlChange] = []
    rejected: list[dict[str, str]] = []
    for change in proposal.yaml_changes:
        if _covered(change.path, frozen):
            rejected.append({"path": change.path, "reason": "frozen path"})
        elif not _covered(change.path, mutable):
            rejected.append({"path": change.path, "reason": "not in the mutable set"})
        else:
            accepted.append(change)
    return GateResult(accepted=accepted, rejected=rejected, code_changes=proposal.code_changes)


# --------------------------------------------------------------------------- #
# Apply & version
# --------------------------------------------------------------------------- #
def _read_counter(yaml_path: Path) -> int:
    if not yaml_path.exists():
        return 0
    match = _COUNTER_RE.search(yaml_path.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else 0


def _set_dotted(raw: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: Any = raw
    for segment in parts[:-1]:
        if segment not in cursor or not isinstance(cursor[segment], dict):
            cursor[segment] = {}
        cursor = cursor[segment]
    cursor[parts[-1]] = value


def apply_proposal(
    harness_dir: str | Path,
    proposal: MutationProposal,
    *,
    hive: Hive | None = None,
    approve_code: Callable[[CodeChange], bool] | None = None,
    apply_yaml: bool = True,
) -> ApplyResult:
    """Gate and apply a proposal, versioning the spec and recording in the Hive.

    ``approve_code`` is asked for each code change (defaults to reject). YAML
    changes apply when ``apply_yaml`` is true and they pass the gate.
    """
    yaml_path = harness_path(harness_dir)
    base = yaml_path.parent
    spec = load_spec(yaml_path)
    old_hash = spec_version_hash(spec, base)

    result = gate(spec, proposal)

    applied_yaml: list[YamlChange] = []
    new_spec = spec
    if apply_yaml and result.accepted:
        raw = load_raw(yaml_path)
        for change in result.accepted:
            _set_dotted(raw, change.path, change.value)
            applied_yaml.append(change)
        new_spec = spec_from_dict(raw, source=str(yaml_path))

    applied_code: list[str] = []
    pending_code: list[str] = []
    for change in result.code_changes:
        approved = approve_code(change) if approve_code is not None else False
        if approved:
            target = base / change.file
            if target.exists():
                target.with_suffix(target.suffix + ".bak").write_text(
                    target.read_text(encoding="utf-8"), encoding="utf-8"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.source, encoding="utf-8")
            applied_code.append(change.file)
        else:
            pending_code.append(change.file)

    changed = bool(applied_yaml or applied_code)
    counter = _read_counter(yaml_path)
    new_hash = old_hash
    if changed:
        counter += 1
        yaml_path.write_text(f"# evolved: {counter}\n" + dump_spec(new_spec), encoding="utf-8")
        validate_harness(yaml_path)  # full re-validation incl. code hooks
        new_hash = spec_version_hash(new_spec, base)
        if hive is not None:
            hive.record_evolution(
                spec.name,
                old_hash,
                new_hash,
                counter,
                proposal.rationale,
                datetime.now(UTC).isoformat(),
            )

    return ApplyResult(
        changed=changed,
        old_version_hash=old_hash,
        new_version_hash=new_hash,
        counter=counter,
        rationale=proposal.rationale,
        applied_yaml=applied_yaml,
        rejected=result.rejected,
        applied_code=applied_code,
        pending_code=pending_code,
    )
