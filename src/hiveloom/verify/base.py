"""Verifier ABC and verdict type.

Feedback must be actionable text: on ``retry_with_feedback`` it is injected back
into the model's context so it can self-correct.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class VerdictResult(BaseModel):
    """The result of a single verifier."""

    passed: bool
    feedback: str = ""
    verifier: str = ""


class Verifier(ABC):
    """A validator over the run output."""

    name: str = "verifier"

    @abstractmethod
    def validate(self, run_output: str, run_context: dict[str, Any]) -> VerdictResult:
        """Return a verdict with actionable feedback on failure."""
