"""Failure analysis over the Hive.

Queries a harness's recent failures and clusters them by failure signature
(verifier feedback text, guardrail type, run status) into a structured report
the evolver sends to the proposing model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive


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

    def is_empty(self) -> bool:
        return not self.clusters and not self.recent_failures


def analyze(hive: Hive, harness_name: str, *, recent: int = 5) -> FailureReport:
    """Build a :class:`FailureReport` for ``harness_name`` from the Hive."""
    summary = hive.summary(harness_name)
    sigs = summary["failure_signatures"]

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

    return FailureReport(
        harness_name=harness_name,
        total_runs=summary["total_runs"],
        success_rate=summary["success_rate"],
        clusters=clusters,
        recent_failures=hive.recent_failures(harness_name, recent),
    )
