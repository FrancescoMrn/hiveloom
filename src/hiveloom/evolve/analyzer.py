"""Failure analysis over the Hive.

Queries a harness's recent failures and clusters them by failure signature
(verifier feedback text, guardrail type, run status) into a structured report
the evolver sends to the proposing model.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive
from hiveloom.spec.schema import MetricObjective, RedactionConfig, TraceExcerptConfig

from .evidence import IncidentEvidence, build_incident_evidence
from .metric_evidence import MetricEvidence, build_metric_evidence


class FailureCluster(BaseModel):
    """A group of failures sharing a signature."""

    kind: str  # verdict | guardrail | status
    signature: str
    count: int


class AttemptRecord(BaseModel):
    """A previous mutation and its review or measured outcome.

    Drivers supply kept/reverted/inconclusive verdicts with their measurements.
    The proposal queue supplies applied/rejected decisions without implying an
    improvement or regression. Records are ordered newest first.
    """

    outcome: str
    rationale: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    yaml_diff: str = ""
    measured: dict[str, Any] = Field(default_factory=dict)
    version_hash: str = ""
    note: str = ""


class FailureReport(BaseModel):
    """A structured summary of a harness's recent failures."""

    harness_name: str
    total_runs: int
    success_rate: float
    clusters: list[FailureCluster] = Field(default_factory=list)
    recent_failures: list[dict[str, Any]] = Field(default_factory=list)
    # Per-playbook fitness, when the harness has modes: localizes a failure to
    # one mode instead of blaming the whole harness.
    playbooks: list[dict[str, Any]] = Field(default_factory=list)
    # What the world said afterwards (see Hive.record_outcome). Empty for
    # harnesses nobody labels — most of them.
    outcomes: dict[str, Any] = Field(default_factory=dict)
    outcome_failures: list[dict[str, Any]] = Field(default_factory=list)
    friction: dict[str, Any] = Field(default_factory=dict)
    recent_friction: list[dict[str, Any]] = Field(default_factory=list)
    incident_evidence: IncidentEvidence | None = None
    metric_evidence: MetricEvidence | None = None
    # Mutations already tried against this harness, with their measured
    # outcome. Search memory: see AttemptRecord.
    attempt_history: list[AttemptRecord] = Field(default_factory=list)
    # Operator-supplied findings about this harness that are not failures.
    #
    # Everything else in this report is derived from runs that went wrong, so
    # everything else can only ever motivate fixing something that *looks* like
    # a failure. A missed opportunity does not: a harness that samples a task
    # once, and would have been right had it sampled three times, produces a
    # clean run with no signature at all. That is not a gap in the clustering,
    # it is a consequence of building evidence out of failures, and the only
    # repair is a channel for what analysis found that failure never shows.
    analyst_notes: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return (
            not self.clusters
            and not self.recent_failures
            and not self.outcome_failures
            and not self.recent_friction
            and not self.analyst_notes
            and not (
                self.metric_evidence is not None
                and self.metric_evidence.has_observations()
            )
        )

    def evidence_receipt(self) -> dict[str, Any] | None:
        """Selection provenance suitable for proposal storage."""
        receipt = (
            self.incident_evidence.receipt()
            if self.incident_evidence is not None
            else {}
        )
        if self.metric_evidence is not None:
            receipt["metric_history"] = self.metric_evidence.receipt()
            receipt.setdefault("digest", self.metric_evidence.digest)
        return receipt or None


def analyze(
    hive: Hive,
    harness_name: str,
    *,
    recent: int = 5,
    version: str | None = None,
    excerpt_config: TraceExcerptConfig | None = None,
    redaction: RedactionConfig | None = None,
    objectives: list[MetricObjective] | None = None,
    attempt_history: list[AttemptRecord] | None = None,
    analyst_notes: list[str] | None = None,
) -> FailureReport:
    """Build a :class:`FailureReport` for ``harness_name`` from the Hive.

    Pass ``version`` to analyse the harness as it is now. Without it the report
    pools every version ever run under this name, so failures a previous
    evolution already repaired keep driving the next proposal — and, because
    harness identity is only the name, so do failures from an unrelated harness
    that happens to share it.

    Scoping applies to the counts, the clusters and the examples together: a
    report whose aggregates exclude a version but whose examples come from it
    would be worse than either choice made consistently.

    ``attempt_history`` is the opposite of ``version``: deliberately *not*
    scoped, because what a previous version already tried and failed is the one
    thing worth carrying forward. A driver that measures its own mutations
    (the autoresearch loop) passes its ledger; everyone else gets the
    unmeasured record of applied and rejected proposals from the queue, which
    is still enough to stop the proposer suggesting the same mutation twice.
    """
    sigs = hive.failure_signatures(harness_name, version=version)

    clusters: list[FailureCluster] = []
    for verdict in sigs["verdicts"]:
        clusters.append(
            FailureCluster(kind="verdict", signature=verdict["feedback"], count=verdict["count"])
        )
    for guardrail in sigs["guardrails"]:
        clusters.append(
            FailureCluster(
                kind="guardrail",
                signature=f"{guardrail['guardrail']} ({guardrail['kind']})",
                count=guardrail["count"],
            )
        )
    for status in sigs["statuses"]:
        clusters.append(
            FailureCluster(kind="status", signature=status["status"], count=status["count"])
        )

    friction = hive.friction_summary(harness_name, version=version)
    for category in friction["categories"]:
        clusters.append(
            FailureCluster(
                kind="friction",
                signature=category["category"],
                count=category["events"],
            )
        )

    # version_stats buckets runs by version, so scoping is filtering those
    # buckets and pooling is summing them — one query serving both, and the two
    # cannot disagree. (hive.summary() would re-run the signature query above
    # just to hand back its two totals.)
    buckets = hive.version_stats(harness_name)
    if version is not None:
        buckets = [b for b in buckets if b["version"] == version]
    total_runs = sum(b["runs"] for b in buckets)
    successes = sum(b["successes"] for b in buckets)

    recent_failures = hive.recent_failures(harness_name, recent, version=version)
    outcome_failures = hive.failed_outcome_traces(
        harness_name, recent, version=version
    )
    friction_limit = max(recent, excerpt_config.max_incidents if excerpt_config else recent)
    recent_friction = hive.list_friction(
        harness_name, version=version, limit=friction_limit
    )
    incident_evidence = None
    if excerpt_config is not None and excerpt_config.enabled:
        incident_evidence = build_incident_evidence(
            hive,
            recent_friction=recent_friction,
            outcome_failures=outcome_failures,
            recent_failures=recent_failures,
            config=excerpt_config,
            redaction=redaction or RedactionConfig(),
        )
    metric_evidence = build_metric_evidence(
        hive,
        harness_name,
        objectives=objectives or [],
        version=version,
    )
    history = (
        attempt_history
        if attempt_history is not None
        else queued_attempt_history(hive, harness_name)
    )

    return FailureReport(
        harness_name=harness_name,
        total_runs=total_runs,
        success_rate=(successes / total_runs) if total_runs else 0.0,
        clusters=clusters,
        recent_failures=recent_failures,
        playbooks=hive.playbook_stats(harness_name, version=version),
        outcomes=hive.outcome_summary(harness_name, version=version),
        outcome_failures=outcome_failures,
        friction=friction,
        recent_friction=recent_friction,
        incident_evidence=incident_evidence,
        metric_evidence=metric_evidence,
        attempt_history=history,
        analyst_notes=list(analyst_notes or []),
    )


# How many past attempts travel with a report. The list is prompt material, so
# it is bounded like every other evidence section; newest first, because a
# proposer that reads only the head still reads the most relevant refusals.
MAX_ATTEMPT_HISTORY = 12


def queued_attempt_history(hive: Hive, harness_name: str) -> list[AttemptRecord]:
    """Resolved proposals as search memory, newest first.

    These carry no measurement — the queue records that a mutation was applied
    or rejected, never whether it helped. Said plainly in ``note`` so the
    proposer does not read "applied" as "worked": a driver with real numbers
    passes them to :func:`analyze` instead.
    """
    records: list[AttemptRecord] = []
    for row in hive.list_proposals(harness_name):
        status = str(row.get("status") or "")
        if status not in ("applied", "rejected"):
            continue
        def object_json(value: Any) -> dict[str, Any]:
            try:
                decoded = json.loads(value or "{}")
            except (TypeError, ValueError):
                return {}
            return decoded if isinstance(decoded, dict) else {}

        applied = object_json(row.get("apply_result_json"))
        proposed = object_json(row.get("proposal_json"))
        changes = (
            applied.get("applied_yaml", []) if status == "applied"
            else proposed.get("yaml_changes", [])
        )
        paths = [
            change["path"] for change in changes if isinstance(change, dict)
            and isinstance(change.get("path"), str)
        ] if isinstance(changes, list) else []
        code = (
            applied.get("applied_code", []) if status == "applied"
            else [item.get("file") for item in (proposed.get("code_changes") or [])
                  if isinstance(item, dict)]
        )
        if isinstance(code, list):
            paths.extend(path for path in code if isinstance(path, str))
        note = "from the proposal queue; no measured effect attached"
        if applied.get("reason"):
            note += f"; reason: {applied['reason']}"
        records.append(
            AttemptRecord(
                outcome=status,
                rationale=str(row.get("rationale") or ""),
                changed_paths=list(dict.fromkeys(paths)),
                version_hash=str(row.get("spec_version_hash") or ""),
                note=note,
            )
        )
        if len(records) >= MAX_ATTEMPT_HISTORY:
            break
    return records
