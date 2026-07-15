"""Evolution: analyze Hive failures and propose gated harness mutations."""

from hiveloom.evolve.analyzer import FailureReport, analyze
from hiveloom.evolve.evolver import (
    ApplyResult,
    MutationProposal,
    apply_proposal,
    gate,
    propose,
)

__all__ = [
    "ApplyResult",
    "FailureReport",
    "MutationProposal",
    "analyze",
    "apply_proposal",
    "gate",
    "propose",
]
