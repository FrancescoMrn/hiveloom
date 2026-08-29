"""Verifier ABC and verdict type.

Feedback must be actionable text: on ``retry_with_feedback`` it is injected back
into the model's context so it can self-correct.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict

from hiveloom.execution import StepExecutionRecord


class VerdictResult(BaseModel):
    """The result of a single verifier."""

    passed: bool
    feedback: str = ""
    verifier: str = ""


class ToolEvidenceRecord(BaseModel):
    """One allowed, executed tool call exposed to verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    input: Any = None
    result: Any = None
    is_error: bool = False
    step_id: str | None = None
    step_index: int | None = None
    truncated: bool = False


class VerificationContext(BaseModel):
    """Redacted, run-local evidence available to verifiers without trace parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    tool_calls: tuple[ToolEvidenceRecord, ...] = ()
    steps: tuple[StepExecutionRecord, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    evidence_truncated: bool = False


class Verifier(ABC):
    """A validator over the run output."""

    name: str = "verifier"

    @abstractmethod
    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        """Return a verdict with actionable feedback on failure."""


def invoke_verifier(
    verifier: Verifier,
    run_output: str,
    run_context: dict[str, Any],
    verification_context: VerificationContext,
) -> VerdictResult:
    """Call new and legacy verifier objects through one additive adapter."""
    method = verifier.validate
    parameters = list(inspect.signature(method).parameters.values())
    accepts_context = len(parameters) >= 3 or any(
        parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
        for parameter in parameters
    )
    if accepts_context:
        return method(run_output, run_context, verification_context)
    return method(run_output, run_context)  # type: ignore[call-arg]
