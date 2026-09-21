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
from hiveloom.evolve.evolver import (
    MEMORY_APPEND_PATH,
    ApplyResult,
    CodeChange,
    GateResult,
    MutationProposal,
    YamlChange,
)
from hiveloom.generate.llm import StrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import harness_path, load_spec
from hiveloom.spec.schema import HarnessSpec, MemoryEntry


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
    evidence_json: str | None = None
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

    @property
    def evidence(self) -> dict[str, Any] | None:
        """Bounded evidence-selection receipt, never the copied event payloads."""
        return json.loads(self.evidence_json) if self.evidence_json else None


def _dedup_key(report: FailureReport) -> str:
    """Deterministic key over a failure report's cluster signatures.

    Identical clusters, evidence, operator findings, and attempt history against
    the same spec version reuse a pending proposal, regardless of cluster order.
    """
    signatures = sorted(f"{cluster.kind}:{cluster.signature}" for cluster in report.clusters)
    evidence = report.evidence_receipt() or {}
    material = json.dumps(
        {
            "signatures": signatures,
            "evidence": evidence,
            "analyst_notes": report.analyst_notes,
            "attempt_history": [item.model_dump(mode="json") for item in report.attempt_history],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def create_proposal(
    hive: Hive,
    spec: HarnessSpec,
    harness_dir: str | Path,
    report: FailureReport,
    model: StrongModel,
    *,
    trigger: str,
    record_empty_as_rejected: bool = False,
) -> ProposalRecord:
    """Propose + gate a harness mutation from a failure report and queue it.

    Deduped by ``(harness_name, spec_version_hash, dedup_key)``: the dedup
    slot is checked *before* calling ``model`` so a colliding request never
    triggers a second (paid) strong-model call — it returns the existing
    pending proposal instead.

    ``record_empty_as_rejected`` (the auto-trigger passes it) records a
    terminal ``rejected`` row when gating leaves nothing to queue, instead of
    raising. Without it the auto-trigger would insert no row, so
    ``last_auto_proposal_at`` never advanced and the cooldown never engaged —
    every subsequent failing run re-paid a strong-model call. A rejected row is
    terminal (not a can-never-apply pending row), and its ``created_at`` is what
    the cooldown/failure-window keys off. Manual and HTTP callers keep the
    raise, so their UX is unchanged.
    """
    trust_mod.ensure_trusted(harness_dir)
    base = harness_path(harness_dir).parent
    version_hash = spec_version_hash(spec, base)
    dedup_key = _dedup_key(report)

    # Proposals bind to the harness's stable identity (its `id`, or the name
    # for a pre-identity spec) so a same-named harness elsewhere can neither
    # see nor apply them.
    existing = hive.find_pending_proposal(spec.identity, version_hash, dedup_key)
    if existing is not None:
        return ProposalRecord.model_validate(existing)

    proposal = evolver.propose(spec, report, model)
    gate_result = evolver.gate(spec, proposal)
    queueable = bool(gate_result.accepted or gate_result.code_changes)
    if not queueable and not record_empty_as_rejected:
        raise ProposalQueueError("proposal has no applicable changes after gating")

    now = datetime.now(UTC).isoformat()
    row = {
        "id": f"prop_{uuid4().hex[:16]}",
        "harness_name": spec.identity,
        "spec_version_hash": version_hash,
        "dedup_key": dedup_key,
        "status": "pending" if queueable else "rejected",
        "trigger": trigger,
        "rationale": proposal.rationale,
        "proposal_json": proposal.model_dump_json(),
        "gate_json": gate_result.model_dump_json(),
        "evidence_json": (
            json.dumps(report.evidence_receipt(), sort_keys=True)
            if report.evidence_receipt() is not None
            else None
        ),
        "apply_result_json": (
            None
            if queueable
            else json.dumps({"reason": "no applicable changes after gating"})
        ),
        "created_at": now,
        "resolved_at": None if queueable else now,
    }
    stored = hive.insert_proposal(row)
    return ProposalRecord.model_validate(stored)


def _memory_dedup_key(entry: MemoryEntry) -> str:
    """Deterministic key over a proposed lesson's *content*.

    Keyed on the lesson rather than the whole entry so a run that rediscovers
    something it already proposed — under a different title, with fresher
    evidence — reuses the pending row instead of queueing a near-duplicate for
    a reviewer to deduplicate by hand. Whitespace and case are normalized for
    the same reason. The ``mem:`` prefix keeps this key space disjoint from
    :func:`_dedup_key`'s failure-cluster keys in the same dedup slot.
    """
    material = " ".join(entry.content.split()).casefold()
    return f"mem:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def create_memory_proposal(
    hive: Hive,
    spec: HarnessSpec,
    harness_dir: str | Path,
    entry: MemoryEntry,
    *,
    run_id: str,
) -> ProposalRecord:
    """Queue an append to ``memory.entries`` — no model call, no spec write.

    The executor-side counterpart to :func:`create_proposal`: the running model
    offers a durable lesson through the ``propose_memory`` tool and this turns
    it into an ordinary queued :class:`MutationProposal`. Everything after this
    point is the existing review path — ``proposals list/show/apply/reject``,
    the gate, full re-validation, and rollback — so a lesson the executor
    proposed reaches ``harness.yaml`` by exactly the route an evolved one does,
    and only when a human says so.

    No strong model is involved: the change is one deterministic YAML append at
    ``memory.entries.+``, gated by :func:`evolver.gate` like any other. A
    gate that accepts nothing (a harness that declares a narrower
    ``evolution.mutable``, or an entry that would break a memory budget) raises
    :class:`ProposalQueueError` rather than queueing a row that can never
    apply.
    """
    trust_mod.ensure_trusted(harness_dir)
    base = harness_path(harness_dir).parent
    version_hash = spec_version_hash(spec, base)
    dedup_key = _memory_dedup_key(entry)

    existing = hive.find_pending_proposal(spec.identity, version_hash, dedup_key)
    if existing is not None:
        return ProposalRecord.model_validate(existing)

    rationale = f"the executor proposed a durable lesson during run {run_id}"
    proposal = MutationProposal(
        rationale=rationale,
        yaml_changes=[
            YamlChange(
                # `+` appends, resolved against the live list at apply time —
                # never the position the list happened to have when this run
                # proposed, which would replace an entry once the list grows.
                path=MEMORY_APPEND_PATH,
                value=entry.model_dump(mode="json", exclude_none=True),
                rationale=entry.evidence or entry.title,
            )
        ],
    )
    gate_result = evolver.gate(spec, proposal)
    if not gate_result.accepted:
        reason = (
            gate_result.rejected[0]["reason"]
            if gate_result.rejected
            else "no applicable changes after gating"
        )
        raise ProposalQueueError(f"the proposed lesson was refused by the gate: {reason}")

    now = datetime.now(UTC).isoformat()
    row = {
        "id": f"prop_{uuid4().hex[:16]}",
        "harness_name": spec.identity,
        "spec_version_hash": version_hash,
        "dedup_key": dedup_key,
        "status": "pending",
        "trigger": "executor",
        "rationale": rationale,
        "proposal_json": proposal.model_dump_json(),
        "gate_json": gate_result.model_dump_json(),
        # A receipt for the reviewer: which run offered this, and which entry
        # it becomes. Never the run's transcript — the lesson is the evidence.
        "evidence_json": json.dumps(
            {"run_id": run_id, "entry_id": entry.id, "kind": entry.kind},
            sort_keys=True,
        ),
        "apply_result_json": None,
        "created_at": now,
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
    return hive.last_auto_proposal_at(harness_name)


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


def _appends_memory_only(proposal: MutationProposal) -> bool:
    """True when every change this proposal carries is a memory append.

    Such a proposal says nothing about the harness version it was drafted
    against: ``memory.entries.+`` resolves against the list on disk at apply
    time and adds an entry without touching one already there. Applying it to a
    newer version is therefore exactly the change that was reviewed — which is
    what lets several lessons from one run reach the same harness. Without this,
    the first applied memory proposal moved the spec version hash and left every
    other queued one permanently unappliable.

    Code changes are excluded deliberately: regenerated source is written
    against a spec the harness may no longer have. The gate, full re-validation,
    and rollback still run at apply, so an append that would break a budget is
    refused there like any other.
    """
    return (
        bool(proposal.yaml_changes)
        and not proposal.code_changes
        and all(change.path == MEMORY_APPEND_PATH for change in proposal.yaml_changes)
    )


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
    A proposal that only appends durable memory is the one exception — see
    :func:`_appends_memory_only`.

    ``confirm_apply_yaml``, when given, is called *after* the trust/existence/
    staleness checks above pass — and overrides ``apply_yaml`` with its
    result — so an interactive caller's confirmation prompt (like
    ``approve_code``'s, per code change) never fires for a proposal that was
    going to be rejected anyway.

    A call that applies *nothing* — the YAML was declined and no code change
    approved — leaves the row ``pending`` and releases the claim, so the
    proposal is still there to apply, not silently resolved as though the
    lesson had landed.
    """
    trust_mod.ensure_trusted(harness_dir)
    row = _require_pending(hive, proposal_id)

    spec = load_spec(harness_dir)
    if spec.identity != row["harness_name"]:
        raise ProposalQueueError(
            f"proposal '{proposal_id}' belongs to harness '{row['harness_name']}', "
            f"not '{spec.identity}'"
        )
    proposal = MutationProposal.model_validate_json(row["proposal_json"])
    base = harness_path(harness_dir).parent
    live_hash = spec_version_hash(spec, base)
    if live_hash != row["spec_version_hash"] and not _appends_memory_only(proposal):
        raise ProposalQueueError(
            f"harness has changed since proposal '{proposal_id}' was drafted "
            f"({row['spec_version_hash']} -> {live_hash}); regenerate"
        )

    if confirm_apply_yaml is not None:
        apply_yaml = confirm_apply_yaml()

    if not hive.claim_pending_proposal(proposal_id):
        current = hive.get_proposal(proposal_id)
        if current is None:
            raise ProposalQueueError(f"no proposal with id '{proposal_id}'")
        raise ProposalQueueError(f"proposal '{proposal_id}' is already {current['status']}")

    try:
        result = evolver.apply_proposal(
            harness_dir, proposal, hive=hive, approve_code=approve_code, apply_yaml=apply_yaml
        )
    except BaseException:
        hive.release_proposal_claim(proposal_id)
        raise

    if not result.changed:
        hive.release_proposal_claim(proposal_id)
        return result

    hive.update_proposal(
        proposal_id,
        status="applied",
        apply_result_json=result.model_dump_json(),
        resolved_at=datetime.now(UTC).isoformat(),
    )
    return result


def proposal_payload(record: ProposalRecord) -> dict[str, Any]:
    """Expand a ``ProposalRecord``'s JSON-text columns into nested objects.

    Shared by the CLI and the HTTP control plane so both callers build the
    exact same JSON shape for a proposal — one place to keep it correct.

    The raw ``*_json`` columns are excluded: emitting them alongside the parsed
    forms sent every gate result and code-change body twice, once opaque and
    once usable.
    """
    payload = record.model_dump(
        exclude={"proposal_json", "gate_json", "evidence_json", "apply_result_json"}
    )
    payload["proposal"] = record.proposal.model_dump()
    payload["gate"] = record.gate.model_dump()
    payload["apply_result"] = record.apply_result
    payload["evidence"] = record.evidence
    return payload


def reject_proposal(hive: Hive, proposal_id: str, reason: str) -> None:
    """Reject a pending proposal, recording the reason. Never touches harness.yaml."""
    _require_pending(hive, proposal_id)
    hive.update_proposal(
        proposal_id,
        status="rejected",
        apply_result_json=json.dumps({"reason": reason}),
        resolved_at=datetime.now(UTC).isoformat(),
    )
