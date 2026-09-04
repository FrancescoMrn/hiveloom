"""The synchronous agent loop engine.

The loop's *strategy* is a pluggable :class:`~hiveloom.loop.policies.LoopPolicy`
(``react``/``plan_then_act`` builtin, more via extensions). The loop drives
guardrail hooks, the lifecycle event bus, the tool registry, context assembly,
and the verify step, emitting a trace event at every step. Designed so an
async version is possible later.

Guardrails and event hooks compose: guardrails are the frozen safety layer
(Allow/Block/Halt), hooks are the extensible middleware layer (block/patch
tool calls, patch results, transform context). Guardrails always run first.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.context.manager import ContextManager
from hiveloom.events import EventBus
from hiveloom.execution import (
    RunExecutionEnvelope,
    VerificationSummary,
    execution_fingerprint,
)
from hiveloom.guardrails.base import Guardrail, RunState
from hiveloom.logging.trace import TraceWriter, harness_snapshot, payload_hash
from hiveloom.loop.control import RunControl
from hiveloom.loop.policies import LoopPolicy, build_policy
from hiveloom.models.provider import (
    ContextOverflowError,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    Usage,
)
from hiveloom.models.router import ModelRouter, portable_messages
from hiveloom.playbooks import PlaybookManager
from hiveloom.spec.schema import HarnessSpec
from hiveloom.tools.registry import ToolRegistry, ToolResult
from hiveloom.verify.base import VerdictResult, Verifier


class RunResult(BaseModel):
    """The outcome of a harness run."""

    status: str  # success | verify_failed | guardrail_halt | max_turns | stopped | error
    output: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    run_id: str = ""
    trace_path: str = ""
    verdicts: list[VerdictResult] = Field(default_factory=list)
    reason: str = ""
    # Structured tool side-products in dispatch order: {"kind", "data", "tool"}.
    # Populated even on a failed run — a turn that proposed something before
    # hitting max_turns still produced it.
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    # One bounded public receipt per provider call. Opaque provider metadata
    # stays on ModelResponse and in the redacted journal, never in this result.
    provider_calls: list[dict[str, Any]] = Field(default_factory=list)
    # Run-only model/provider choices. ``requested`` retains explicit CLI/SDK
    # overrides; ``resolved`` is the validated config that the router used.
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    execution: RunExecutionEnvelope | None = None

    def artifacts_of(self, kind: str) -> list[Any]:
        """The ``data`` payloads of every artifact of one kind, in order."""
        return [a["data"] for a in self.artifacts if a.get("kind") == kind]


class GuardrailHalt(RuntimeError):
    """Internal signal used when a model-call guardrail prevents a request."""


class ToolAbort(RuntimeError):
    """Internal signal used when ``loop.on_tool_error`` is ``abort``."""


def _blocked_result(call: Any, reason: str) -> dict[str, Any]:
    return {"tool_use_id": call.id, "content": f"blocked: {reason}", "is_error": True}


def _terminate_output(dispatched: list[Any], results: list[dict[str, Any]]) -> str | None:
    """The batch terminates only when every finalized result asked to."""
    if dispatched and len(dispatched) == len(results) and all(r.terminate for r in dispatched):
        return dispatched[-1].content
    return None


class AgentLoop:
    """Runs a harness to completion and returns a :class:`RunResult`."""

    def __init__(
        self,
        spec: HarnessSpec,
        base_dir: str | Path,
        provider: ModelProvider,
        registry: ToolRegistry,
        guardrails: list[Guardrail],
        verifiers: list[Verifier],
        context: ContextManager,
        trace: TraceWriter,
        run_input: str,
        run_id: str,
        events: EventBus | None = None,
        policy: LoopPolicy | None = None,
        history: list[dict[str, Any]] | None = None,
        context_values: dict[str, Any] | None = None,
        playbooks: PlaybookManager | None = None,
        control: RunControl | None = None,
        router: ModelRouter | None = None,
        resume: bool = False,
        lineage: dict[str, Any] | None = None,
        harness_version_hash: str = "",
        runtime_version: str = "",
        runtime_config: dict[str, Any] | None = None,
    ):
        self._spec = spec
        self._base = Path(base_dir)
        self._provider = provider
        self._registry = registry
        self._guardrails = guardrails
        self._verifiers = verifiers
        self._context = context
        self._trace = trace
        self._run_input = run_input
        self._run_id = run_id
        self._history = list(history or [])
        # A resumed fork seeds a folded conversation that already ends where
        # the parent run was, so there is no new task statement to append.
        self._resume = resume
        self._lineage = lineage
        self._harness_version_hash = harness_version_hash
        self._runtime_version = runtime_version
        self._runtime_config = runtime_config or {
            "requested": {"model": None, "provider": None},
            "resolved": {"model": spec.model.id, "provider": spec.model.provider},
        }
        self._started_at = ""
        # Kept by reference, not copied: a tool may accumulate run-scoped state
        # in it across calls, and the caller reads it back after the run.
        self._context_values = context_values if context_values is not None else {}
        self._playbooks = playbooks
        if playbooks is not None:
            self._context.set_playbooks(playbooks)
            switch_tool = registry.get("switch_playbook")
            if switch_tool is not None:
                switch_tool.bind(self._handle_switch_playbook)
        self._events = events if events is not None else EventBus(trace=trace)
        self._policy = policy if policy is not None else build_policy(
            spec.loop.policy, {"steps": spec.loop.steps}
        )
        self._router = router if router is not None else ModelRouter.create(
            self._base,
            ModelConfig(
                id=spec.model.id,
                max_tokens=spec.model.max_tokens,
                temperature=spec.model.temperature,
                provider=spec.model.provider,
            ),
            provider,
        )
        self._control = control
        self._state = RunState(tool_names=set(registry.names()))
        self._provider_calls: list[dict[str, Any]] = []
        self._usage = Usage()
        self._verification_attempts = 0
        self._context.set_compaction_model_call(self._compaction_model_turn)

    # ------------------------------------------------------------------ #
    # Public surface for policies and hooks
    # ------------------------------------------------------------------ #
    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def state(self) -> RunState:
        return self._state

    # ------------------------------------------------------------------ #
    def run(self) -> RunResult:
        loop = self._spec.loop
        started = self._trace.emit(
            "run_started",
            input=self._run_input,
            policy=loop.policy,
            model=self._router.config.id,
            provider=self._router.config.provider,
            schema_version=self._spec.schema_version,
            hiveloom_version=self._runtime_version,
            runtime_config=self._runtime_config,
            history_turns=len(self._history),
            resumed=self._resume,
            # Where this run came from, when it is a fork: the parent run and
            # the exact journal point it re-entered. This is what lets the Hive
            # compare a fork against its parent on the prefix they share.
            **({"lineage": self._lineage} if self._lineage else {}),
            # What produced this run, not just its 12-hex fingerprint: the
            # dumped spec plus a manifest of every local behavioural file. A
            # journal that only names a version hash cannot be forked without
            # the original folder, and cannot prove the folder never moved.
            harness=harness_snapshot(
                self._spec,
                self._base,
                include_files=self._spec.logging.snapshot_files,
            ),
        )
        self._started_at = started.timestamp
        self._events.emit(
            "run_started",
            {
                "input": self._run_input,
                "policy": loop.policy,
                "model": self._router.config.id,
                "history": self._history,
            },
        )

        halt = self._guardrail_halt(lambda g: g.before_run(self._state))
        if halt is not None:
            return self._finish("guardrail_halt", reason=halt)

        self._enter_initial_playbook()

        # Prior turns first, so the current input stays the newest message —
        # policies and compaction both rely on that position.
        self._context.seed_history(self._history)
        if not self._resume:
            self._context.add_user(self._run_input)
        try:
            self._policy.on_run_start(self)
        except GuardrailHalt as exc:
            return self._finish("guardrail_halt", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - surface as an error run, not a crash
            return self._finish("error", reason=f"policy failed: {type(exc).__name__}: {exc}")

        retries = 0
        last_verdicts: list[VerdictResult] = []
        while self._state.model_calls < loop.max_turns:
            # Turn boundary: the one point where outside control is consumed.
            # The state is coherent here — no model call or tool in flight.
            if self._control is not None:
                if self._control.stop_requested():
                    return self._finish(
                        "stopped",
                        output=self._state.output or "",
                        reason=self._control.stop_reason,
                    )
                for steer in self._control.drain_messages():
                    self._trace.emit("user_steer", content=steer)
                    self._context.add_user(
                        "[Operator message received while you were working — "
                        f"take it into account from here on]\n{steer}"
                    )
                for request in self._control.drain_model_switches():
                    self._switch_model(**request)
                for request in self._control.drain_playbook_switches():
                    self._switch_playbook_from_operator(**request)
            try:
                response = self.model_turn()
            except GuardrailHalt as exc:
                return self._finish("guardrail_halt", reason=str(exc))
            except Exception as exc:  # noqa: BLE001 - surface as an error run, not a crash
                return self._finish("error", reason=f"{type(exc).__name__}: {exc}")

            self._context.add_assistant(self.assistant_blocks(response))

            if response.tool_calls:
                try:
                    halt, terminate_output = self._dispatch_tools(response)
                except ToolAbort as exc:
                    return self._finish("error", reason=str(exc))
                if halt is not None:
                    return self._finish("guardrail_halt", reason=halt)
                self._state.tool_turns += 1
                if terminate_output is None:
                    continue
                # Every tool result in the batch asked to terminate: treat the
                # last result as the final output, skipping a model turn.
                output = terminate_output
            else:
                nudge = self._policy.wants_continue(self, response)
                if nudge is not None:
                    self._context.add_user(nudge)
                    self._state.policy_nudges += 1
                    continue
                # No tool calls -> the model is signalling completion.
                output = response.text

            output = self._transform_output(output)
            self._state.output = output

            block = self._on_output(output)
            if block is not None:
                if block.startswith("HALT:"):
                    return self._finish("guardrail_halt", reason=block[5:])
                self._context.add_user(
                    f"Your output was blocked ({block}). Produce a compliant result."
                )
                continue

            if loop.require_verification:
                verdicts = self._verify(output)
                last_verdicts = verdicts
                if all(v.passed for v in verdicts):
                    return self._finish("success", output=output, verdicts=verdicts)
                if (
                    self._spec.verify.on_fail.action == "retry_with_feedback"
                    and retries < self._spec.verify.on_fail.max_retries
                ):
                    retries += 1
                    self._state.verify_retries += 1
                    feedback = "\n".join(v.feedback for v in verdicts if not v.passed)
                    self._context.add_user(
                        f"Verification failed:\n{feedback}\nRevise your answer and try again."
                    )
                    continue
                return self._finish("verify_failed", output=output, verdicts=verdicts)

            return self._finish("success", output=output)

        return self._finish(
            "max_turns", output=self._state.output or "", verdicts=last_verdicts
        )

    # ------------------------------------------------------------------ #
    # Model routing
    # ------------------------------------------------------------------ #
    def _switch_model(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        reason: str = "",
        source: str = "operator",
    ) -> bool:
        """Move the executing model. Returns True if anything actually changed.

        Called at a turn boundary only — the same discipline as stop and steer,
        for the same reason: no model call and no tool is in flight, so the
        conversation is in a state another model can be handed.
        """
        previous = self._router.config
        try:
            switch = self._router.switch(
                model=model,
                provider=provider,
                turn=self._state.turns,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - a bad target must not kill the run
            self._trace.emit(
                "model_swap_failed",
                requested_model=model,
                requested_provider=provider,
                source=source,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        if switch is None:
            return False

        # Prior turns may carry blocks only the previous model can validate.
        # Strip them here, once, rather than hoping every provider ignores
        # what it does not recognise.
        dropped = 0
        if switch.provider != previous.provider or switch.model != previous.id:
            self._context.messages, dropped = portable_messages(self._context.messages)

        self._trace.emit(
            "model_swap",
            **{"from": f"{previous.provider}:{previous.id}"},
            to=switch.key,
            turn=switch.turn,
            reason=reason,
            source=source,
            blocks_dropped=dropped,
        )
        return True

    # ------------------------------------------------------------------ #
    def model_turn(self, *, phase: str = "act") -> ModelResponse:
        system, messages = self._context.assemble()
        tools = self._registry.anthropic_payload()
        try:
            return self._complete_model_call(system, messages, tools, phase)
        except ContextOverflowError as exc:
            # The provider's window overflowed even though the local estimate
            # fit. Compact hard once and retry the turn; a second overflow (or
            # nothing left to compact) surfaces as an error run.
            if not self._context.force_compact():
                raise
            self._trace.emit("context_overflow_recovery", phase=phase, error=str(exc))
            system, messages = self._context.assemble()
            return self._complete_model_call(system, messages, tools, phase)

    def _compaction_model_turn(
        self, system: str, messages: list[dict[str, Any]]
    ) -> ModelResponse:
        return self._complete_model_call(system, messages, [], "compaction")

    def _complete_model_call(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        phase: str,
    ) -> ModelResponse:
        input_tokens = self._router.provider.count_tokens(
            system=system, messages=messages, tools=tools
        )
        self._state.pending_cost_usd = self._router.provider.estimated_cost(
            Usage(input_tokens=input_tokens, output_tokens=self._router.config.max_tokens),
            self._router.config.id,
            self._router.config.provider,
        )
        halt = self._guardrail_halt(lambda g: g.before_model_call(self._state))
        if halt is not None:
            self._state.pending_cost_usd = 0.0
            raise GuardrailHalt(halt)
        if phase == "compaction":
            # An out-of-band request: a one-off summarisation prompt that is
            # not part of the conversation. It is recorded inline (it is small,
            # and it has no context events of its own) and flagged so the
            # journal fold skips it instead of mistaking it for history.
            self._trace.emit(
                "model_call",
                turn=self._state.turns,
                phase=phase,
                num_messages=len(messages),
                inline=True,
                system=system,
                messages=messages,
            )
        else:
            # The conversation itself is already journalled message by message;
            # the system prompt and tool payload are journalled only when they
            # change. So a model_call records what it *consumed*, not a copy of
            # it — see hiveloom.logging.journal for the fold that reads it back.
            system_hash = self._trace.emit_context_system(system)
            tools_hash = self._trace.emit_context_tools(tools)
            self._trace.emit(
                "model_call",
                turn=self._state.turns,
                phase=phase,
                num_messages=len(messages),
                context_head=self._trace.context_head,
                system_hash=system_hash,
                tools_hash=tools_hash,
                # The context meter. Both numbers are already known here —
                # `input_tokens` was just counted for the cost guardrail — and
                # recording them is what lets a reader see how close a call ran
                # to the budget without re-tokenizing the whole conversation.
                input_tokens=input_tokens,
                max_input_tokens=self._spec.context.max_input_tokens,
                # A checksum of what actually went on the wire. The fold
                # reconstructs the persisted conversation; a `context_assemble`
                # hook patches one request without persisting it, so this is
                # how a reader detects that the reconstruction is not the whole
                # story rather than silently believing it.
                messages_hash=payload_hash(messages),
            )
        self._events.emit("before_model_call", {"turn": self._state.turns, "phase": phase})
        # Request middleware: patches apply to this request only, and run
        # after guardrails so a hook can never widen what a guardrail vetoed.
        if self._events.has_handlers("before_provider_request"):
            for outcome in self._events.emit(
                "before_provider_request",
                {
                    "system": system,
                    "messages": messages,
                    "tools": tools,
                    "model": self._router.config.id,
                    "phase": phase,
                },
            ):
                patched = False
                if isinstance(outcome.get("system"), str):
                    system = outcome["system"]
                    patched = True
                if isinstance(outcome.get("messages"), list):
                    messages = outcome["messages"]
                    patched = True
                if isinstance(outcome.get("tools"), list):
                    tools = outcome["tools"]
                    patched = True
                if patched:
                    self._trace.emit(
                        "hook_triggered",
                        event="before_provider_request",
                        hook=outcome["_handler"],
                        action="patch_request",
                    )
        response = self._router.provider.complete(
            system=system,
            messages=messages,
            tools=tools,
            config=self._router.config,
        )
        self._state.model_calls += 1
        self._state.turns = self._state.model_calls
        self._usage = self._usage + response.usage
        estimated_cost = self._router.provider.estimated_cost(
            response.usage, self._router.config.id, self._router.config.provider
        )
        cost, cost_source = response.resolved_cost_usd(estimated_cost)
        self._state.cost_usd += cost
        self._state.pending_cost_usd = 0.0
        provider_call = {
            "turn": self._state.turns,
            "phase": phase,
            "provider": self._router.config.provider,
            "requested_model": self._router.config.id,
            "effective_model": response.model or None,
            "provider_request_id": response.provider_request_id or None,
            "usage": response.usage.model_dump(),
            "cost_usd": cost,
            "cost_source": cost_source,
            "billed_cost": response.billed_cost,
            "billed_currency": response.billed_currency or None,
        }
        self._provider_calls.append(provider_call)
        self._events.emit(
            "after_provider_response",
            {
                "phase": phase,
                "model": self._router.config.id,
                "stop_reason": response.stop_reason,
                "usage": response.usage.model_dump(),
                "cost_usd": cost,
                "cost_source": cost_source,
                "effective_model": response.model or None,
                "provider_request_id": response.provider_request_id or None,
                "billed_cost": response.billed_cost,
                "billed_currency": response.billed_currency or None,
                "provider_metadata": response.provider_metadata,
            },
        )
        self._trace.emit(
            "model_response",
            turn=self._state.turns,
            phase=phase,
            text=response.text,
            stop_reason=response.stop_reason,
            tool_calls=[c.name for c in response.tool_calls],
            usage=response.usage.model_dump(),
            cost_usd=cost,
            cost_source=cost_source,
            effective_model=response.model or None,
            provider_request_id=response.provider_request_id or None,
            billed_cost=response.billed_cost,
            billed_currency=response.billed_currency or None,
            billed_cost_usd=response.billed_cost_usd,
            provider_metadata=response.provider_metadata,
        )
        self._events.emit(
            "after_model_response",
            {
                "turn": self._state.turns,
                "text": response.text,
                "stop_reason": response.stop_reason,
                "tool_calls": [c.name for c in response.tool_calls],
            },
        )
        halt = self._guardrail_halt(
            lambda g, resp=response: g.after_model_response(self._state, resp)
        )
        if halt is not None:
            raise GuardrailHalt(halt)
        return response

    def _dispatch_tools(self, response: ModelResponse) -> tuple[str | None, str | None]:
        """Dispatch a turn's tool calls.

        Returns ``(halt_reason, terminate_output)``: a halt reason ends the run;
        ``terminate_output`` is set when every finalized result asked to
        terminate (its value is the last result's content).

        ``loop.tool_execution: parallel`` splits the per-call pipeline into
        phases: preflight (guardrails + hooks, source order), execute
        (concurrent), finalize (source order). Sequential mode keeps the
        original semantics — a halt during one call's finalize prevents later
        calls from executing at all.
        """
        if response.stop_reason == "max_tokens":
            # The response was cut off mid-emission, so the calls' argument
            # JSON is untrustworthy (typically missing fields). Executing them
            # would surface confusing errors — or worse, act on partial input.
            truncated = [
                {
                    "tool_use_id": call.id,
                    "content": (
                        f"Call to {call.name} not executed: your response hit the "
                        "max_tokens ceiling and the tool arguments arrived truncated. "
                        "Retry with a more compact call (shorter lists and texts, or "
                        "an aggregate form of the same request)."
                    ),
                    "is_error": True,
                }
                for call in response.tool_calls
            ]
            for call in response.tool_calls:
                self._trace.emit("tool_truncated", name=call.name, id=call.id)
            self._context.add_tool_results(truncated)
            return None, None
        if self._spec.loop.tool_execution == "parallel" and len(response.tool_calls) > 1:
            return self._dispatch_parallel(response)
        results: list[dict[str, Any]] = []
        dispatched: list[Any] = []
        for call in response.tool_calls:
            pre = self._preflight_call(call)
            if pre is not None:
                kind, reason = pre
                if kind == "halt":
                    return reason, None
                results.append(_blocked_result(call, reason))
                continue
            self._trace.emit("tool_call", name=call.name, input=call.input, id=call.id)
            result = self._execute_call(call)
            halt = self._finalize_call(call, result)
            if halt is not None:
                return halt, None
            dispatched.append(result)
            results.append(
                {"tool_use_id": call.id, "content": result.content, "is_error": result.is_error}
            )
        self._context.add_tool_results(results)
        return None, _terminate_output(dispatched, results)

    def _dispatch_parallel(self, response: ModelResponse) -> tuple[str | None, str | None]:
        plan: list[tuple[Any, str | None]] = []
        for call in response.tool_calls:
            pre = self._preflight_call(call)
            if pre is not None:
                kind, reason = pre
                if kind == "halt":
                    return reason, None
                plan.append((call, reason))
                continue
            self._trace.emit("tool_call", name=call.name, input=call.input, id=call.id)
            plan.append((call, None))

        runnable = [(i, call) for i, (call, blocked) in enumerate(plan) if blocked is None]
        executed: dict[int, Any] = {}
        abort: ToolAbort | None = None
        if runnable:
            with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
                futures = [(i, pool.submit(self._execute_call, call)) for i, call in runnable]
                for i, future in futures:
                    try:
                        executed[i] = future.result()
                    except ToolAbort as exc:
                        # Keep draining so no worker outlives the batch; the
                        # first abort in source order is the one reported.
                        abort = abort if abort is not None else exc
        if abort is not None:
            raise abort

        results: list[dict[str, Any]] = []
        dispatched: list[Any] = []
        for i, (call, blocked) in enumerate(plan):
            if blocked is not None:
                results.append(_blocked_result(call, blocked))
                continue
            result = executed[i]
            halt = self._finalize_call(call, result)
            if halt is not None:
                return halt, None
            dispatched.append(result)
            results.append(
                {"tool_use_id": call.id, "content": result.content, "is_error": result.is_error}
            )
        self._context.add_tool_results(results)
        return None, _terminate_output(dispatched, results)

    def _preflight_call(self, call: Any) -> tuple[str, str] | None:
        """Guardrails then hooks for one call. ``("halt", r)``/``("block", r)``/None."""
        for guardrail in self._guardrails:
            decision = guardrail.before_tool_call(self._state, call)
            if decision.kind in ("halt", "block"):
                self._trace.emit(
                    "guardrail_triggered",
                    hook="before_tool_call",
                    guardrail=guardrail.name,
                    kind=decision.kind,
                    reason=decision.reason,
                )
                return decision.kind, decision.reason

        for outcome in self._events.emit(
            "before_tool_call",
            {
                "name": call.name,
                "input": call.input,
                "turn": self._state.turns,
                "cost_usd": self._state.cost_usd,
            },
        ):
            if outcome.get("block"):
                reason = str(outcome.get("reason", f"blocked by hook {outcome['_handler']}"))
                self._trace.emit(
                    "hook_triggered",
                    event="before_tool_call",
                    hook=outcome["_handler"],
                    action="block",
                    reason=reason,
                )
                return "block", reason
            if isinstance(outcome.get("input"), dict):
                call.input = outcome["input"]
                self._trace.emit(
                    "hook_triggered",
                    event="before_tool_call",
                    hook=outcome["_handler"],
                    action="patch_input",
                )
        return None

    def _execute_call(self, call: Any) -> Any:
        """Dispatch one preflighted call (worker-thread safe: trace only)."""

        def on_update(progress: str, _call=call) -> None:
            self._trace.emit("tool_update", id=_call.id, name=_call.name, content=progress)

        run_context = self._run_context()
        result = self._registry.dispatch(call, on_update=on_update, run_context=run_context)
        if (
            result.is_error
            and result.retryable
            and self._spec.loop.on_tool_error == "retry_once"
        ):
            self._trace.emit("tool_retry", id=call.id, name=call.name)
            result = self._registry.dispatch(
                call, on_update=on_update, run_context=run_context
            )
        if result.is_error and self._spec.loop.on_tool_error == "abort":
            raise ToolAbort(f"tool '{call.name}' failed: {result.content}")
        return result

    def _finalize_call(self, call: Any, result: Any) -> str | None:
        """After-hooks and after-guardrails for one call. Returns a halt reason."""
        for outcome in self._events.emit(
            "after_tool_call",
            {
                "name": call.name,
                "input": call.input,
                "content": result.content,
                "is_error": result.is_error,
            },
        ):
            patched = False
            if isinstance(outcome.get("content"), str):
                result.content = outcome["content"]
                patched = True
            if isinstance(outcome.get("is_error"), bool):
                result.is_error = outcome["is_error"]
                patched = True
            if patched:
                self._trace.emit(
                    "hook_triggered",
                    event="after_tool_call",
                    hook=outcome["_handler"],
                    action="patch_result",
                )

        for guardrail in self._guardrails:
            decision = guardrail.after_tool_call(self._state, call, result)
            if decision.kind == "halt":
                self._trace.emit(
                    "guardrail_triggered",
                    hook="after_tool_call",
                    guardrail=guardrail.name,
                    kind="halt",
                    reason=decision.reason,
                )
                return decision.reason

        # Collect structured side-products before tracing, so the trace shows
        # exactly what the caller will receive.
        collected = [
            {"kind": artifact.kind, "data": artifact.data, "tool": call.name}
            for artifact in getattr(result, "artifacts", [])
        ]
        self._state.artifacts.extend(collected)

        self._trace.emit(
            "tool_result",
            id=call.id,
            name=call.name,
            content=result.content,
            is_error=result.is_error,
            artifacts=collected,
        )
        return None

    def _on_output(self, output: str) -> str | None:
        """Run on_output guardrails. Returns None (ok), a block reason, or 'HALT:<reason>'."""
        for guardrail in self._guardrails:
            decision = guardrail.on_output(self._state, output)
            if decision.kind == "halt":
                self._trace.emit(
                    "guardrail_triggered",
                    hook="on_output",
                    guardrail=guardrail.name,
                    kind="halt",
                    reason=decision.reason,
                )
                return f"HALT:{decision.reason}"
            if decision.kind == "block":
                self._trace.emit(
                    "guardrail_triggered",
                    hook="on_output",
                    guardrail=guardrail.name,
                    kind="block",
                    reason=decision.reason,
                )
                return decision.reason
        return None

    def _transform_output(self, output: str) -> str:
        """Apply explicit final-output hooks before safety checks and verification."""
        for outcome in self._events.emit("before_verification", {"output": output}):
            replacement = outcome.get("output")
            if isinstance(replacement, str) and replacement != output:
                output = replacement
                self._trace.emit(
                    "hook_triggered",
                    event="before_verification",
                    hook=outcome["_handler"],
                    action="patch_output",
                )
        return output

    # ------------------------------------------------------------------ #
    # Playbooks
    # ------------------------------------------------------------------ #
    def _hook_error(self, playbook: str, kind: str, exc: Exception) -> None:
        self._trace.emit(
            "hook_error",
            event=f"playbook_{kind}",
            hook=playbook,
            error=f"{type(exc).__name__}: {exc}",
        )

    def _enter_initial_playbook(self) -> None:
        if self._playbooks is None or not self._playbooks.names:
            return
        outcome = self._playbooks.enter_initial(
            run_context=self._run_context(), on_hook_error=self._hook_error
        )
        name = self._playbooks.current_name
        self._trace.emit(
            "playbook_switch", to=name, **{"from": None}, reason="run start",
            ok=outcome.ok, notes=outcome.notes,
        )
        self._apply_playbook_model(name)
        self._events.emit(
            "playbook_enter", {"playbook": name, "from": None, "reason": "run start"}
        )

    def _handle_switch_playbook(self, name: str, reason: str = "") -> ToolResult:
        """Back the switch_playbook tool. Refusals are tool errors, not halts."""
        assert self._playbooks is not None  # bound only when playbooks exist
        previous = self._playbooks.current_name
        outcome = self._playbooks.switch(
            name,
            run_context=self._run_context(),
            reason=reason,
            on_hook_error=self._hook_error,
        )
        self._trace.emit(
            "playbook_switch",
            to=name,
            **{"from": previous},
            reason=reason,
            ok=outcome.ok,
            notes=outcome.notes,
            refused_reason=outcome.reason,
        )
        if not outcome.ok:
            return ToolResult(content=outcome.reason, is_error=True, retryable=False)

        self._apply_playbook_model(name)
        self._events.emit(
            "playbook_exit", {"playbook": previous, "to": name, "reason": reason}
        )
        self._events.emit(
            "playbook_enter", {"playbook": name, "from": previous, "reason": reason}
        )
        active = ", ".join(sorted(self._registry.active_names()))
        message = [f"Now in playbook '{name}'. Active tools: {active}."]
        message += outcome.notes
        return ToolResult(content=" ".join(message))

    def _apply_playbook_model(self, name: str | None) -> None:
        """Put the run on the entered playbook's model, or back on the spec's.

        A playbook that declares no ``model`` restores the harness default, so
        leaving a mode always undoes what entering it did — a mode is a
        configuration, not a one-way door. The switch happens between turns,
        which is where the conversation is in a state another model can be
        handed.
        """
        if self._playbooks is None or name is None:
            return
        playbook = self._playbooks.get(name)
        if playbook is None:
            return
        self._switch_model(
            model=playbook.ref.model or self._spec.model.id,
            provider=playbook.ref.model_provider or self._spec.model.provider,
            reason=f"playbook '{name}'",
            source="playbook",
        )

    def _switch_playbook_from_operator(self, *, name: str, reason: str = "") -> bool:
        """Move the run into another playbook on an operator's instruction.

        Routed through the same :class:`PlaybookManager` the ``switch_playbook``
        tool uses, so the mode's ``on_enter``/``on_exit`` gates still run and
        may refuse. An operator switch is a request through the same door the
        model uses — a gate that exists to stop a premature exit should stop
        one that arrives over HTTP too.

        The model is told, as a user turn: it has just had its tools narrowed
        and its prompt fragment swapped, and a mode change it cannot see is a
        mode change it will misread.
        """
        if self._playbooks is None or not self._playbooks.names:
            self._trace.emit(
                "playbook_switch_failed",
                to=name,
                source="operator",
                reason=reason,
                refused_reason="this harness declares no playbooks",
            )
            return False

        previous = self._playbooks.current_name
        why = reason or "operator request"
        outcome = self._playbooks.switch(
            name,
            run_context=self._run_context(),
            reason=why,
            on_hook_error=self._hook_error,
        )
        self._trace.emit(
            "playbook_switch",
            to=name,
            **{"from": previous},
            reason=why,
            source="operator",
            ok=outcome.ok,
            notes=outcome.notes,
            refused_reason=outcome.reason,
        )
        if not outcome.ok:
            return False

        self._apply_playbook_model(name)
        self._events.emit(
            "playbook_exit", {"playbook": previous, "to": name, "reason": why}
        )
        self._events.emit(
            "playbook_enter", {"playbook": name, "from": previous, "reason": why}
        )
        active = ", ".join(sorted(self._registry.active_names()))
        note = [
            f"[Operator switched you into playbook '{name}' while you were "
            f"working. Active tools: {active}.]"
        ]
        note += outcome.notes
        self._context.add_user(" ".join(note))
        return True

    def _run_context(self, **extra: Any) -> dict[str, Any]:
        """The per-run dict handed to code tools and validators.

        ``context`` holds the caller's own values (a DSN, request-scoped
        state). It is nested rather than merged so a caller key can never
        shadow ``input``/``harness_dir``/``run_id``, and the same object is
        passed through — a tool may use it to accumulate state across calls
        within one run.
        """
        return {
            "input": self._run_input,
            "harness_dir": str(self._base),
            "run_id": self._run_id,
            "context": self._context_values,
            # A snapshot of what the run has produced so far. This is what
            # makes a playbook exit gate expressible ("you entered targeting
            # and proposed nothing") and lets a validator grade side-products,
            # not just the final text.
            "artifacts": list(self._state.artifacts),
            **extra,
        }

    def _active_verifiers(self) -> list[Verifier]:
        """Spec validators plus the current playbook's, if any.

        Mode validators are additive: a playbook grades what *it* is
        responsible for on top of the harness-wide contract, never instead of
        it — otherwise entering a mode could quietly lower the bar.
        """
        if self._playbooks is None or self._playbooks.current is None:
            return self._verifiers
        refs = self._playbooks.current.ref.validators
        if not refs:
            return self._verifiers
        from hiveloom.verify.builtin import build_verifiers_from_refs

        return [*self._verifiers, *build_verifiers_from_refs(refs, self._base)]

    def _verify(self, output: str) -> list[VerdictResult]:
        self._verification_attempts += 1
        run_context = self._run_context(
            output=output, playbook=self._playbooks.current_name if self._playbooks else None
        )
        verdicts: list[VerdictResult] = []
        for verifier in self._active_verifiers():
            verdict = verifier.validate(output, run_context)
            verdict.verifier = verdict.verifier or verifier.name
            self._trace.emit(
                "verification_result",
                verifier=verdict.verifier,
                passed=verdict.passed,
                feedback=verdict.feedback,
            )
            verdicts.append(verdict)
        self._events.emit(
            "verification", {"verdicts": [v.model_dump() for v in verdicts]}
        )
        return verdicts

    def _guardrail_halt(self, hook) -> str | None:
        for guardrail in self._guardrails:
            decision = hook(guardrail)
            if decision.kind == "halt":
                self._trace.emit(
                    "guardrail_triggered",
                    guardrail=guardrail.name,
                    kind="halt",
                    reason=decision.reason,
                )
                return decision.reason
        return None

    @staticmethod
    def assistant_blocks(response: ModelResponse) -> list[dict[str, Any]]:
        blocks = (
            list(response.content_blocks)
            if response.content_blocks
            else [{"type": "text", "text": response.text}]
        )
        if response.reasoning is not None:
            # Provider-neutral storage, provider-owned decoding. The current
            # adapter can replay its opaque JSON on the next tool turn; model
            # swaps drop this non-portable block in models.router.
            blocks.append({"type": "provider_reasoning", "data": response.reasoning})
        return blocks

    def _finish(
        self,
        status: str,
        *,
        output: str = "",
        reason: str = "",
        verdicts: list[VerdictResult] | None = None,
    ) -> RunResult:
        duration_seconds = self._state.elapsed_seconds()
        verification = self._verification_summary(status)
        models_used = [
            {"turn": s.turn, "model": s.model, "provider": s.provider, "reason": s.reason}
            for s in self._router.path
        ]
        effective_models = list(
            dict.fromkeys(
                call["effective_model"]
                for call in self._provider_calls
                if call.get("effective_model")
            )
        )
        cost_sources = {call["cost_source"] for call in self._provider_calls}
        if not cost_sources:
            cost_source = "none"
        elif len(cost_sources) == 1:
            cost_source = next(iter(cost_sources))
        else:
            cost_source = "mixed"
        requested = self._runtime_config.get("requested") or {}
        resolved = self._runtime_config.get("resolved") or {}
        requested_provider = requested.get("provider") or resolved.get("provider") or ""
        requested_model = requested.get("model") or resolved.get("model") or ""
        execution = RunExecutionEnvelope(
            run_id=self._run_id,
            status=status,
            harness_id=self._spec.id,
            harness_name=self._spec.name,
            schema_version=self._spec.schema_version,
            behavior_hash=self._harness_version_hash,
            execution_fingerprint=execution_fingerprint(
                behavior_hash=self._harness_version_hash,
                hiveloom_version=self._runtime_version,
                schema_version=self._spec.schema_version,
                runtime_config=self._runtime_config,
                input_value=self._run_input,
                models_used=models_used,
                effective_models=effective_models,
                lineage=self._lineage,
            ),
            hiveloom_version=self._runtime_version,
            requested_provider=requested_provider,
            requested_model=requested_model,
            resolved_provider=str(resolved.get("provider") or ""),
            resolved_model=str(resolved.get("model") or ""),
            effective_provider=(
                self._provider_calls[-1]["provider"] if self._provider_calls else None
            ),
            effective_model=next(
                (
                    call["effective_model"]
                    for call in reversed(self._provider_calls)
                    if call.get("effective_model")
                ),
                None,
            ),
            models_used=models_used,
            started_at=self._started_at,
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=round(duration_seconds * 1000),
            usage=self._usage,
            cost_usd=self._state.cost_usd,
            cost_source=cost_source,
            verification=verification,
            trace_path=str(self._trace.path),
        )
        self._trace.emit(
            "run_finished",
            status=status,
            reason=reason,
            turns=self._state.turns,
            cost_usd=self._state.cost_usd,
            duration_seconds=duration_seconds,
            execution=execution.model_dump(mode="json"),
            # The answer and the judgements on it. A journal that reports a
            # run's status but not what it produced is not a complete record
            # of the run it describes.
            output=output,
            verdicts=[v.model_dump() for v in (verdicts or [])],
            artifacts=self._state.artifacts,
            # Which model(s) actually executed. A run that changed models is
            # not a clean sample of "this harness at this version", and the
            # Hive must be able to say so rather than blend it into a bucket
            # with runs that did not.
            model_path=self._router.path_key(),
            models_used=models_used,
            provider_calls=self._provider_calls,
        )
        self._events.emit(
            "run_finished",
            {
                "status": status,
                "reason": reason,
                "turns": self._state.turns,
                "cost_usd": self._state.cost_usd,
            },
        )
        return RunResult(
            status=status,
            output=output,
            turns=self._state.turns,
            cost_usd=self._state.cost_usd,
            duration_seconds=duration_seconds,
            run_id=self._run_id,
            trace_path=str(self._trace.path),
            verdicts=verdicts or [],
            reason=reason,
            artifacts=list(self._state.artifacts),
            provider_calls=list(self._provider_calls),
            runtime_config=self._runtime_config,
            execution=execution,
        )

    def _verification_summary(self, status: str) -> VerificationSummary:
        if not self._spec.loop.require_verification or self._verification_attempts == 0:
            return VerificationSummary()
        return VerificationSummary(
            attempts=self._verification_attempts,
            first_pass_valid=status == "success" and self._state.verify_retries == 0,
            recovery_attempted=self._state.verify_retries > 0,
            recovered=status == "success" and self._state.verify_retries > 0,
            final_status="passed" if status == "success" else "failed",
        )
