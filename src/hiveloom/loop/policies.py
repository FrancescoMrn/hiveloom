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
from hiveloom.models.provider import ModelResponse

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
    """Walk a fixed, ordered list of objectives, refusing completion early.

    Each element of ``steps`` is pinned as the current objective in turn (via
    ``ContextManager.set_plan``, the same durable-context mechanism
    ``plan_then_act`` uses). A no-tool-call reply before the last step is
    consumed is refused: the loop is nudged to the next step instead of
    accepting the output. v1 does not verify a step was actually done — a
    lazy model can emit a no-op turn to advance; tying steps to per-step
    validators is a documented v2.
    """

    name = "sequential_steps"

    def __init__(self, steps: list[str]) -> None:
        self._steps = steps
        self._index = 0

    def on_run_start(self, loop: AgentLoop) -> None:
        loop.context.set_plan(self._render())

    def wants_continue(self, loop: AgentLoop, response: ModelResponse) -> str | None:
        if self._index >= len(self._steps) - 1:
            return None  # last step already consumed: accept the completion
        finished = self._index + 1  # 1-based number of the step just done
        self._index += 1
        loop.context.set_plan(self._render())
        return (
            f"Step {finished} is done. Continue with step {self._index + 1} of "
            f"{len(self._steps)}: {self._steps[self._index]}"
        )

    def _render(self) -> str:
        lines = ["Sequential steps:"]
        for i, step in enumerate(self._steps):
            if i < self._index:
                marker = "done"
            elif i == self._index:
                marker = "current"
            else:
                marker = "pending"
            lines.append(f"{i + 1}. [{marker}] {step}")
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
