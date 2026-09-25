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

import hashlib
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

    def select_output(self, loop: AgentLoop, output: str) -> str:
        """Choose the run's final output from a finished attempt.

        The default accepts what the loop arrived at. A policy that samples the
        task more than once overrides this: it is holding the other attempts,
        so it is the only thing that can say which one is the answer.
        """
        del loop
        return output

    def fallback_output(self, loop: AgentLoop, output: str) -> str:
        """The best answer the policy holds when the loop ran out of turns.

        Distinct from :meth:`select_output`, which only ever sees a *finished*
        attempt. This is the exhausted path, where a policy mid-way through
        something may still be holding a usable answer that would otherwise be
        thrown away for a partial one.
        """
        del loop
        return output

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


class BestOfNPolicy(LoopPolicy):
    """Solve the task ``attempts`` times independently, then submit the consensus.

    Every other policy here shapes a *single* line of reasoning. This one
    changes how many there are, which is the only lever a harness has against
    a model that is simply wrong: prompts, tools and limits cannot make a bad
    inference good, but drawing the inference several times and keeping what
    the draws agree on can. On any task where the answer is checkable and the
    model is right more often than it is wrong in the same *way*, independent
    samples concentrate on the truth and scatter on the errors.

    Independence is the whole mechanism, and it is easy to lose by accident:
    an attempt that can read the previous one is not a second sample, it is a
    continuation, and it will anchor on the answer it can see. So each attempt
    rewinds the context to the pinned prefix — the system prompt and the task
    statement — and starts again with no memory of its predecessors. What a
    later attempt does see is one restart instruction, which names neither the
    attempt number nor how many remain: a model told it has tries left can
    spend less on the current one, and a cheaper draw is not the same draw.

    Selection is a plurality vote over normalized output text, first-seen
    order breaking ties. No extra model call, nothing to mis-transcribe, and a
    deterministic answer to "which one?" — an agreement count is evidence, and
    asking a model to pick its own favourite is not. ``attempts`` that all
    disagree fall back to the first, because with no agreement the samples are
    interchangeable and the first is the only one not chosen after the fact.

    Budget: every attempt spends from the same ``loop.max_turns``, so N
    attempts need roughly N times the turns a single attempt needed. Set it
    accordingly — a policy that runs out of turns mid-sweep submits whatever it
    is holding, which is a worse answer than one honest attempt.
    """

    name = "best_of_n"

    def __init__(self, attempts: int = 3) -> None:
        if attempts < 1:
            raise ValueError("best_of_n requires attempts >= 1")
        self._target = attempts
        self._prefix = 0
        self._candidates: list[str] = []
        self._selected = False

    def on_run_start(self, loop: AgentLoop) -> None:
        # Whatever is on the context now is the shared preamble: the system
        # prompt, the task statement, and any seeded history from a fork or a
        # resume. Measuring it rather than assuming "one pinned message" is
        # what makes the rewind correct for those runs too.
        self._prefix = len(loop.context.messages)

    def _restart(self, loop: AgentLoop, candidate: str) -> str | None:
        """Bank the finished attempt and set up the next independent one."""
        if self._selected:
            # Selection already happened; this is a verification retry
            # revising the chosen answer, not a fresh sample.
            return None
        self._candidates.append(candidate or "")
        loop.emit_step_event(
            "attempt_recorded",
            policy=self.name,
            attempt=len(self._candidates),
            attempts=self._target,
            output_chars=len(candidate or ""),
            # Which attempts agreed is the whole claim this policy makes, and a
            # length is not an identity — two different answers of the same size
            # look identical in the trace. A short digest of the normalized text
            # makes the vote reconstructable afterwards without storing the
            # answers themselves, which on some harnesses are enormous.
            fingerprint=_fingerprint(candidate or ""),
        )
        if len(self._candidates) >= self._target:
            return None
        loop.context.rewind_to(self._prefix)
        # Deliberately says neither which attempt this is nor how many remain.
        # A model told it has two more tries can spend less on this one, and an
        # attempt sampled at lower effort is not the same draw as the others —
        # it would bias the vote it is supposed to be an independent member of.
        return (
            "Your answer has been recorded. Now solve the task again from the "
            "beginning, working independently: do not assume any earlier answer "
            "was right."
        )

    def wants_continue(self, loop: AgentLoop, response: ModelResponse) -> str | None:
        return self._restart(loop, response.text)

    def wants_continue_after_tools(
        self, loop: AgentLoop, response: ModelResponse
    ) -> str | None:
        # A `submit_answer`-style tool ends the run from inside a tool result,
        # so this — not `wants_continue` — is where an attempt finishes on the
        # harnesses most likely to want more than one of them. The answer is on
        # the loop rather than in `response`, whose text is empty here.
        return self._restart(loop, loop.pending_output or response.text)

    def select_output(self, loop: AgentLoop, output: str) -> str:
        if self._selected:
            return output
        if not self._candidates or self._candidates[-1] != output:
            # The terminating-tool path, and the final attempt of the
            # no-tool-call path, both arrive here with an unbanked answer.
            if len(self._candidates) < self._target:
                self._candidates.append(output)
        if len(self._candidates) < self._target:
            return output
        self._selected = True
        winner, votes = _plurality(self._candidates)
        loop.emit_step_event(
            "attempts_selected",
            policy=self.name,
            attempts=len(self._candidates),
            votes=votes,
            distinct=len({_normalize(c) for c in self._candidates}),
            unanimous=votes == len(self._candidates),
            winner=_fingerprint(winner),
        )
        return winner

    def fallback_output(self, loop: AgentLoop, output: str) -> str:
        """Out of turns mid-sweep: submit the best of what was finished.

        Running out of turns with two attempts banked and one in flight used to
        submit whatever partial text the loop was holding — usually nothing at
        all. Two completed answers are strictly better evidence than that, and
        discarding them turns a budget mistake into a zero.
        """
        if self._selected or not self._candidates:
            return output
        winner, votes = _plurality(self._candidates)
        loop.emit_step_event(
            "attempts_truncated",
            policy=self.name,
            attempts=len(self._candidates),
            target=self._target,
            votes=votes,
        )
        return winner

    def execution_records(self) -> list[StepExecutionRecord]:
        return []


def _normalize(text: str) -> str:
    """Compare answers on content, not on how they were spaced."""
    return " ".join((text or "").split())


def _fingerprint(text: str) -> str:
    """A short, stable digest of an answer, for counting agreement in traces."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:12]


def _plurality(candidates: list[str]) -> tuple[str, int]:
    """The most-agreed candidate and its vote count, first-seen breaking ties."""
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for candidate in candidates:
        key = _normalize(candidate)
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, candidate)
    best = max(counts, key=lambda key: counts[key])
    return first[best], counts[best]


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
    ext.register_builtin_factory(
        "policies",
        "best_of_n",
        lambda p, _c: BestOfNPolicy(int(p.get("attempts", 3))),
    )


_register_factories()
