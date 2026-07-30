"""The proposals queue: evolution proposals as reviewable, queueable artifacts.

``evolve --propose`` (and, later, an automatic post-run trigger and an HTTP
control plane) call :func:`create_proposal` instead of applying immediately.
A human reviews the queue and calls :func:`apply_proposal_by_id` or
:func:`reject_proposal` when ready. All propose/gate/apply logic still lives
in :mod:`hiveloom.evolve.evolver`; this module only orchestrates and persists
it in the Hive.

Every function takes its ``hive``/``spec``/``harness_dir`` explicitly (no
globals) so later callers — the auto-trigger and the HTTP control plane — can
drive this queue without going through the CLI.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from hiveloom import trust as trust_mod
from hiveloom.errors import ProposalQueueError
from hiveloom.evolve import evolver
from hiveloom.evolve.analyzer import FailureReport
from hiveloom.evolve.evolver import ApplyResult, CodeChange, GateResult, MutationProposal
from hiveloom.generate.llm import StrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import harness_path, load_spec
from hiveloom.spec.schema import HarnessSpec


class ProposalRecord(BaseModel):
    """Mirrors a row of the ``proposals`` Hive table."""

    id: str
    harness_name: str
    spec_version_hash: str
    dedup_key: str
    status: str
    trigger: str
    rationale: str
    proposal_json: str
    gate_json: str
    apply_result_json: str | None = None
    created_at: str
    resolved_at: str | None = None

    @property
    def proposal(self) -> MutationProposal:
        """The stored ``proposal_json``, parsed."""
        return MutationProposal.model_validate_json(self.proposal_json)

    @property
    def gate(self) -> GateResult:
        """The stored ``gate_json``, parsed."""
        return GateResult.model_validate_json(self.gate_json)

    @property
    def apply_result(self) -> dict[str, Any] | None:
        """The stored ``apply_result_json``, parsed, or ``None`` if unresolved."""
        return json.loads(self.apply_result_json) if self.apply_result_json else None


def _dedup_key(report: FailureReport) -> str:
    """Deterministic key over a failure report's cluster signatures.

    Same failure state (same clusters) against the same spec version always
    dedups to the same pending proposal, regardless of cluster ordering.
    """
    signatures = sorted(f"{cluster.kind}:{cluster.signature}" for cluster in report.clusters)
    return hashlib.sha256("\n".join(signatures).encode("utf-8")).hexdigest()[:12]


def create_proposal(
    hive: Hive,
    spec: HarnessSpec,
    harness_dir: str | Path,
    report: FailureReport,
    model: StrongModel,
    *,
    trigger: str,
) -> ProposalRecord:
    """Propose + gate a harness mutation from a failure report and queue it.

    Deduped by ``(harness_name, spec_version_hash, dedup_key)``: the dedup
    slot is checked *before* calling ``model`` so a colliding request never
    triggers a second (paid) strong-model call — it returns the existing
    pending proposal instead.
    """
    trust_mod.ensure_trusted(harness_dir)
    base = harness_path(harness_dir).parent
    version_hash = spec_version_hash(spec, base)
    dedup_key = _dedup_key(report)

    existing = hive.find_pending_proposal(spec.name, version_hash, dedup_key)
    if existing is not None:
        return ProposalRecord.model_validate(existing)

    proposal = evolver.propose(spec, report, model)
    gate_result = evolver.gate(spec, proposal)

    row = {
        "id": f"prop_{uuid4().hex[:16]}",
        "harness_name": spec.name,
        "spec_version_hash": version_hash,
        "dedup_key": dedup_key,
        "status": "pending",
        "trigger": trigger,
        "rationale": proposal.rationale,
        "proposal_json": proposal.model_dump_json(),
        "gate_json": gate_result.model_dump_json(),
        "apply_result_json": None,
        "created_at": datetime.now(UTC).isoformat(),
        "resolved_at": None,
    }
    stored = hive.insert_proposal(row)
    return ProposalRecord.model_validate(stored)


def list_proposals(
    hive: Hive, harness_name: str | None = None, status: str | None = None
) -> list[ProposalRecord]:
    """List queued proposals, optionally filtered by harness and/or status."""
    return [
        ProposalRecord.model_validate(row)
        for row in hive.list_proposals(harness_name=harness_name, status=status)
    ]


def last_auto_proposal_at(hive: Hive, harness_name: str) -> str | None:
    """``created_at`` of the most recent auto-triggered proposal for this harness.

    ``None`` if there isn't one yet. Used by the runner's post-run trigger both
    to window the failure count (only failures since the last auto-proposal
    matter) and to enforce the cooldown between auto-drafted proposals.
    """
    for row in hive.list_proposals(harness_name=harness_name):
        if row["trigger"] == "auto":
            return row["created_at"]
    return None


def get_proposal(hive: Hive, proposal_id: str) -> ProposalRecord | None:
    """Fetch a single proposal by id, or ``None`` if unknown."""
    row = hive.get_proposal(proposal_id)
    return ProposalRecord.model_validate(row) if row is not None else None


def _require_pending(hive: Hive, proposal_id: str) -> dict[str, Any]:
    row = hive.get_proposal(proposal_id)
    if row is None:
        raise ProposalQueueError(f"no proposal with id '{proposal_id}'")
    if row["status"] != "pending":
        raise ProposalQueueError(f"proposal '{proposal_id}' is already {row['status']}")
    return row


def apply_proposal_by_id(
    hive: Hive,
    harness_dir: str | Path,
    proposal_id: str,
    *,
    approve_code: Callable[[CodeChange], bool] | None = None,
    apply_yaml: bool = True,
    confirm_apply_yaml: Callable[[], bool] | None = None,
) -> ApplyResult:
    """Apply a queued proposal, delegating gate+apply to :mod:`evolver` unchanged.

    Re-derives ``spec_version_hash`` from the live harness first; if it no
    longer matches the hash the proposal was drafted against, raises
    :class:`ProposalQueueError` without touching disk. A matching hash means
    the harness is byte-identical to what was gated, so re-gating inside
    ``evolver.apply_proposal`` reproduces the same accepted/rejected split.

    ``confirm_apply_yaml``, when given, is called *after* the trust/existence/
    staleness checks above pass — and overrides ``apply_yaml`` with its
    result — so an interactive caller's confirmation prompt (like
    ``approve_code``'s, per code change) never fires for a proposal that was
    going to be rejected anyway.
    """
    trust_mod.ensure_trusted(harness_dir)
    row = _require_pending(hive, proposal_id)

    spec = load_spec(harness_dir)
    base = harness_path(harness_dir).parent
    live_hash = spec_version_hash(spec, base)
    if live_hash != row["spec_version_hash"]:
        raise ProposalQueueError(
            f"harness has changed since proposal '{proposal_id}' was drafted "
            f"({row['spec_version_hash']} -> {live_hash}); regenerate"
        )

    if confirm_apply_yaml is not None:
        apply_yaml = confirm_apply_yaml()

    proposal = MutationProposal.model_validate_json(row["proposal_json"])
    result = evolver.apply_proposal(
        harness_dir, proposal, hive=hive, approve_code=approve_code, apply_yaml=apply_yaml
    )

    hive.update_proposal(
        proposal_id,
        status="applied",
        apply_result_json=result.model_dump_json(),
        resolved_at=datetime.now(UTC).isoformat(),
    )
    return result


def reject_proposal(hive: Hive, proposal_id: str, reason: str) -> None:
    """Reject a pending proposal, recording the reason. Never touches harness.yaml."""
    _require_pending(hive, proposal_id)
    hive.update_proposal(
        proposal_id,
        status="rejected",
        apply_result_json=json.dumps({"reason": reason}),
        resolved_at=datetime.now(UTC).isoformat(),
    )
