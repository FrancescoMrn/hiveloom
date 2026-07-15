"""Guardrail ABC, decision types, and the shared run state.

A guardrail returns one of:

* :class:`Allow` — proceed,
* :class:`Block` — cancel this specific action and inform the model,
* :class:`Halt` — abort the whole run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from hiveloom.models.provider import ModelResponse, ToolCall
from hiveloom.tools.registry import ToolResult


@dataclass
class Decision:
    """A guardrail decision."""

    kind: Literal["allow", "block", "halt"]
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"


def Allow() -> Decision:  # noqa: N802 - reads as a constructor
    return Decision("allow")


def Block(reason: str) -> Decision:  # noqa: N802
    return Decision("block", reason)


def Halt(reason: str) -> Decision:  # noqa: N802
    return Decision("halt", reason)


@dataclass
class RunState:
    """Mutable run bookkeeping shared with guardrails."""

    cost_usd: float = 0.0
    turns: int = 0
    tool_names: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)
    output: str | None = None

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at


class Guardrail:
    """Base guardrail. Every hook defaults to :func:`Allow`."""

    name: str = "guardrail"

    def before_run(self, state: RunState) -> Decision:
        return Allow()

    def before_model_call(self, state: RunState) -> Decision:
        return Allow()

    def after_model_response(self, state: RunState, response: ModelResponse) -> Decision:
        return Allow()

    def before_tool_call(self, state: RunState, call: ToolCall) -> Decision:
        return Allow()

    def after_tool_call(self, state: RunState, call: ToolCall, result: ToolResult) -> Decision:
        return Allow()

    def on_output(self, state: RunState, output: str) -> Decision:
        return Allow()
