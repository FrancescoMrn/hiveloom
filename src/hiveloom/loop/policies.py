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

from typing import TYPE_CHECKING

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
        loop.context.add_user(
            "Before acting, output a brief numbered plan of the steps you will take. "
            "Do not call any tools yet."
        )
        response = loop.model_turn(phase="plan")
        loop.context.add_assistant(loop.assistant_blocks(response))
        loop.context.set_plan(response.text)
        loop.state.turns += 1


def build_policy(name: str) -> LoopPolicy:
    """Construct the policy registered under ``name`` (builtin or extension)."""
    return ext.build("policies", name, {}, ext.BuildContext())


def _register_factories() -> None:
    ext.register_builtin_factory("policies", "react", lambda _p, _c: ReactPolicy())
    ext.register_builtin_factory(
        "policies", "plan_then_act", lambda _p, _c: PlanThenActPolicy()
    )


_register_factories()
