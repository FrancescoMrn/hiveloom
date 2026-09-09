"""Loop policies: pluggable strategies for how the agent loop is driven.

A policy hooks two points of :class:`~hiveloom.loop.agent_loop.AgentLoop`:

* :meth:`LoopPolicy.on_run_start` — runs once before the main loop (e.g. a
  planning turn);
* :meth:`LoopPolicy.wants_continue` — when the model responds without tool
  calls (the completion signal), the policy may return a user message that is
  injected to force another turn (e.g. a reflexion critique pass), or ``None``
  to accept the completion.

Policies are catalog entries: builtins here, more via
``ExtensionAPI.register_policy``. Inside a policy, the loop's public surface is
``loop.context`` (the :class:`ContextManager`), ``loop.state`` (the
:class:`RunState`), and ``loop.model_turn(phase=...)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hiveloom import ext
from hiveloom.execution import StepExecutionRecord
from hiveloom.models.provider import ModelResponse
from hiveloom.spec.schema import SequentialStep

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from hiveloom.loop.agent_loop import AgentLoop


class LoopPolicy:
    """Base policy: plain react (think -> tools -> observe -> repeat)."""

    name: str = "policy"

    def on_run_start(self, loop: AgentLoop) -> None:
        """Called once after the task input is added, before the main loop."""

    def wants_continue(self, loop: AgentLoop, response: ModelResponse) -> str | None:
        """When the model signals completion, return a message to keep going.

        ``None`` accepts the completion; a string is injected as a user message
        and the loop takes another turn.
        """
        return None

    def before_model_turn(self, loop: AgentLoop) -> None:
        """Enforce policy state immediately before a normal model turn."""

    def after_model_turn(self, loop: AgentLoop, response: ModelResponse) -> None:
        """Record a completed normal model turn."""

    def before_tool_call(self, loop: AgentLoop, name: str) -> str | None:
        """Return a policy block reason before a tool call, or allow it."""
        return None

    def after_tool_call(self, loop: AgentLoop, name: str, *, succeeded: bool) -> None:
        """Record the finalized result of an allowed tool call."""

    def wants_continue_after_tools(
        self, loop: AgentLoop, response: ModelResponse
    ) -> str | None:
        """Refuse a terminating tool result when the policy still has work."""
        return None

    def after_tool_turn(self, loop: AgentLoop, response: ModelResponse) -> str | None:
        """Optionally advance after a non-terminating tool batch."""
        return None

    def execution_records(self) -> list[StepExecutionRecord]:
        """Return bounded public policy receipts for RunResult and the Hive."""
        return []

    def current_step(self) -> tuple[str, int] | None:
        """Return the active structured step identity for tool evidence."""
        return None


class StepPolicyHalt(RuntimeError):
    """A structured step exhausted a declared deterministic limit."""


class ReactPolicy(LoopPolicy):
    name = "react"


class PlanThenActPolicy(LoopPolicy):
    """One planning turn produces a pinned step list, then react over it."""

    name = "plan_then_act"

    def on_run_start(self, loop: AgentLoop) -> None:
        # The task input is already the first user turn. Folding the planning
        # instruction into it preserves providers' strict role alternation.
        first = loop.context.messages[-1]
        first["content"] = (
            f"{first['content']}\n\n"
            "Before acting, output a brief numbered plan of the steps you will take. "
            "Do not call any tools yet."
        )
        response = loop.model_turn(phase="plan")
        loop.context.add_assistant(loop.assistant_blocks(response))
        loop.context.set_plan(response.text)


class SequentialStepsPolicy(LoopPolicy):
    """Walk fixed objectives while enforcing optional per-step constraints."""

    name = "sequential_steps"

    def __init__(self, steps: list[str | SequentialStep]) -> None:
        self._steps = [
            step
            if isinstance(step, SequentialStep)
            else SequentialStep(id=f"step-{index + 1}", instruction=step)
            for index, step in enumerate(steps)
        ]
        self._index = 0
        self._finished = False
        self._records = [
            StepExecutionRecord(
                id=step.id,
                index=index,
                instruction=step.instruction,
                required_tool_calls=step.require_tool_calls,
            )
            for index, step in enumerate(self._steps)
        ]

    def on_run_start(self, loop: AgentLoop) -> None:
        self._start_current(loop)

    @property
    def _step(self) -> SequentialStep:
        return self._steps[self._index]

    @property
    def _record(self) -> StepExecutionRecord:
        return self._records[self._index]

    def _start_current(self, loop: AgentLoop) -> None:
        step = self._step
        self._record.status = "running"
        loop.set_step_tools(step.tools)
        loop.context.set_plan(self._render())
        loop.emit_step_event(
            "step_started",
            step_id=step.id,
            step_index=self._index,
            instruction=step.instruction,
            tools=step.tools,
            require_tool_calls=step.require_tool_calls,
            max_model_calls=step.max_model_calls,
            max_tool_calls=step.max_tool_calls,
        )

    def _violation(self, loop: AgentLoop, kind: str, detail: str) -> None:
        detail = detail[:500]
        receipt = f"{kind}: {detail}"
        if receipt not in self._record.violations and len(self._record.violations) < 50:
            self._record.violations.append(receipt)
        loop.emit_step_event(
            "step_violation",
            step_id=self._step.id,
            step_index=self._index,
            kind=kind,
            detail=detail,
        )

    def _fail(self, loop: AgentLoop, kind: str, detail: str) -> None:
        self._violation(loop, kind, detail)
        self._record.status = "failed"
        loop.emit_step_event(
            "step_failed",
            step_id=self._step.id,
            step_index=self._index,
            kind=kind,
            detail=detail,
            model_calls=self._record.model_calls,
            tool_calls=self._record.tool_calls,
        )
        raise StepPolicyHalt(f"step '{self._step.id}' failed: {detail}")

    def before_model_turn(self, loop: AgentLoop) -> None:
        maximum = self._step.max_model_calls
        if maximum is not None and self._record.model_calls >= maximum:
            self._fail(loop, "model_call_limit", f"maximum {maximum} reached")

    def after_model_turn(self, loop: AgentLoop, response: ModelResponse) -> None:
        del loop, response
        self._record.model_calls += 1

    def before_tool_call(self, loop: AgentLoop, name: str) -> str | None:
        allowed = self._step.tools
        if allowed is not None and name not in allowed:
            reason = f"tool '{name}' is not available in step '{self._step.id}'"
            self._violation(loop, "hidden_tool", reason)
            return reason
        maximum = self._step.max_tool_calls
        if maximum is not None and self._record.tool_calls >= maximum:
            self._fail(loop, "tool_call_limit", f"maximum {maximum} reached")
        self._record.tool_calls += 1
        return None

    def after_tool_call(self, loop: AgentLoop, name: str, *, succeeded: bool) -> None:
        del loop
        if (
            succeeded
            and name in self._step.require_tool_calls
            and name not in self._record.completed_required_tool_calls
        ):
            self._record.completed_required_tool_calls.append(name)

    def _advance_or_nudge(self, loop: AgentLoop) -> str | None:
        if self._finished:
            return None
        missing = [
            name
            for name in self._step.require_tool_calls
            if name not in self._record.completed_required_tool_calls
        ]
        if missing:
            detail = "missing successful required call(s): " + ", ".join(missing)
            self._violation(loop, "missing_required_tool_calls", detail)
            return f"Step '{self._step.id}' cannot complete yet: {detail}."

        finished = self._index
        self._record.status = "completed"
        loop.emit_step_event(
            "step_completed",
            step_id=self._step.id,
            step_index=self._index,
            model_calls=self._record.model_calls,
            tool_calls=self._record.tool_calls,
            completed_required_tool_calls=self._record.completed_required_tool_calls,
        )
        if self._index >= len(self._steps) - 1:
            self._finished = True
            return None
        self._index += 1
        self._start_current(loop)
        return (
            f"Step {finished + 1} is done. Continue with step {self._index + 1} of "
            f"{len(self._steps)}: {self._step.instruction}"
        )

    def wants_continue(self, loop: AgentLoop, response: ModelResponse) -> str | None:
        del response
        return self._advance_or_nudge(loop)

    def wants_continue_after_tools(
        self, loop: AgentLoop, response: ModelResponse
    ) -> str | None:
        del response
        return self._advance_or_nudge(loop)

    def after_tool_turn(self, loop: AgentLoop, response: ModelResponse) -> str | None:
        del response
        if not self._step.require_tool_calls or self._index >= len(self._steps) - 1:
            return None
        missing = set(self._step.require_tool_calls) - set(
            self._record.completed_required_tool_calls
        )
        return None if missing else self._advance_or_nudge(loop)

    def execution_records(self) -> list[StepExecutionRecord]:
        return [record.model_copy(deep=True) for record in self._records]

    def current_step(self) -> tuple[str, int] | None:
        return self._step.id, self._index

    def _render(self) -> str:
        lines = ["Sequential steps:"]
        for i, step in enumerate(self._steps):
            if i < self._index:
                marker = "done"
            elif i == self._index:
                marker = "current"
            else:
                marker = "pending"
            lines.append(f"{i + 1}. [{marker}] {step.instruction}")
        return "\n".join(lines)


def build_policy(name: str, params: dict[str, Any] | None = None) -> LoopPolicy:
    """Construct the policy registered under ``name`` (builtin or extension)."""
    return ext.build("policies", name, params or {}, ext.BuildContext())


def _register_factories() -> None:
    ext.register_builtin_factory("policies", "react", lambda _p, _c: ReactPolicy())
    ext.register_builtin_factory(
        "policies", "plan_then_act", lambda _p, _c: PlanThenActPolicy()
    )
    ext.register_builtin_factory(
        "policies",
        "sequential_steps",
        lambda p, _c: SequentialStepsPolicy(p.get("steps", [])),
    )


_register_factories()
