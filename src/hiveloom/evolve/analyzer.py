"""Failure analysis over the Hive.

Queries a harness's recent failures and clusters them by failure signature
(verifier feedback text, guardrail type, run status) into a structured report
the evolver sends to the proposing model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive
from hiveloom.spec.schema import RedactionConfig, TraceExcerptConfig

from .evidence import IncidentEvidence, build_incident_evidence


class FailureCluster(BaseModel):
    """A group of failures sharing a signature."""

    kind: str  # verdict | guardrail | status
    signature: str
    count: int


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

    def is_empty(self) -> bool:
        return (
            not self.clusters
            and not self.recent_failures
            and not self.outcome_failures
            and not self.recent_friction
        )

    def evidence_receipt(self) -> dict[str, Any] | None:
        """Selection provenance suitable for proposal storage."""
        return self.incident_evidence.receipt() if self.incident_evidence is not None else None


def analyze(
    hive: Hive,
    harness_name: str,
    *,
    recent: int = 5,
    version: str | None = None,
    excerpt_config: TraceExcerptConfig | None = None,
    redaction: RedactionConfig | None = None,
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
    )
