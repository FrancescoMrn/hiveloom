"""Evolution: analyze Hive failures and propose gated harness mutations."""

from hiveloom.evolve.analyzer import FailureReport, analyze
from hiveloom.evolve.evolver import (
    ApplyResult,
    MutationProposal,
    ProseChange,
    apply_proposal,
    gate,
    preview_yaml_changes,
    propose,
    resolve_code_change_path,
)

__all__ = [
    "ApplyResult",
    "FailureReport",
    "MutationProposal",
    "ProseChange",
    "analyze",
    "apply_proposal",
    "gate",
    "preview_yaml_changes",
    "propose",
    "resolve_code_change_path",
]
