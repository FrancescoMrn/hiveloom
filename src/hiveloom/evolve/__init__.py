"""Evolution: analyze Hive failures and propose gated harness mutations."""

from hiveloom.evolve.analyzer import FailureReport, analyze
from hiveloom.evolve.evidence import IncidentEvidence, IncidentPacket
from hiveloom.evolve.evolver import (
    ApplyResult,
    MutationProposal,
    ObjectiveExpectation,
    apply_proposal,
    gate,
    preview_yaml_changes,
    propose,
    resolve_code_change_path,
)
from hiveloom.evolve.metric_evidence import MetricEvidence

__all__ = [
    "ApplyResult",
    "FailureReport",
    "IncidentEvidence",
    "IncidentPacket",
    "MetricEvidence",
    "MutationProposal",
    "ObjectiveExpectation",
    "analyze",
    "apply_proposal",
    "gate",
    "preview_yaml_changes",
    "propose",
    "resolve_code_change_path",
]
