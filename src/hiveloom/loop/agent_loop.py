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

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.context.manager import ContextManager
from hiveloom.events import EventBus
from hiveloom.guardrails.base import Guardrail, RunState
from hiveloom.logging.trace import TraceWriter
from hiveloom.loop.policies import LoopPolicy, build_policy
from hiveloom.models.provider import ModelConfig, ModelProvider, ModelResponse, Usage
from hiveloom.spec.schema import HarnessSpec
from hiveloom.tools.registry import ToolRegistry
from hiveloom.verify.base import VerdictResult, Verifier


class RunResult(BaseModel):
    """The outcome of a harness run."""

    status: str  # success | verify_failed | guardrail_halt | max_turns | error
    output: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    run_id: str = ""
    trace_path: str = ""
    verdicts: list[VerdictResult] = Field(default_factory=list)
    reason: str = ""


class GuardrailHalt(RuntimeError):
    """Internal signal used when a model-call guardrail prevents a request."""


class ToolAbort(RuntimeError):
    """Internal signal used when ``loop.on_tool_error`` is ``abort``."""


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
        self._events = events if events is not None else EventBus(trace=trace)
        self._policy = policy if policy is not None else build_policy(
            spec.loop.policy, {"steps": spec.loop.steps}
        )
        self._model_config = ModelConfig(
            id=spec.model.id,
            max_tokens=spec.model.max_tokens,
            temperature=spec.model.temperature,
            provider=spec.model.provider,
        )
        self._state = RunState(tool_names=set(registry.names()))
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
        self._trace.emit(
            "run_started",
            input=self._run_input,
            policy=loop.policy,
            model=self._model_config.id,
        )
        self._events.emit(
            "run_started",
            {"input": self._run_input, "policy": loop.policy, "model": self._model_config.id},
        )

        halt = self._guardrail_halt(lambda g: g.before_run(self._state))
        if halt is not None:
            return self._finish("guardrail_halt", reason=halt)

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
    def model_turn(self, *, phase: str = "act") -> ModelResponse:
        system, messages = self._context.assemble()
        return self._complete_model_call(
            system, messages, self._registry.anthropic_payload(), phase
        )

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
        input_tokens = self._provider.count_tokens(system=system, messages=messages, tools=tools)
        self._state.pending_cost_usd = self._provider.estimated_cost(
            Usage(input_tokens=input_tokens, output_tokens=self._model_config.max_tokens),
            self._model_config.id,
            self._model_config.provider,
        )
        halt = self._guardrail_halt(lambda g: g.before_model_call(self._state))
        if halt is not None:
            self._state.pending_cost_usd = 0.0
            raise GuardrailHalt(halt)
        self._trace.emit(
            "model_call",
            turn=self._state.turns,
            phase=phase,
            num_messages=len(messages),
            system=system,
            messages=messages,
            tools=tools,
        )
        self._events.emit("before_model_call", {"turn": self._state.turns, "phase": phase})
        response = self._provider.complete(
            system=system,
            messages=messages,
            tools=tools,
            config=self._model_config,
        )
        self._state.model_calls += 1
        self._state.turns = self._state.model_calls
        cost = self._provider.estimated_cost(
            response.usage, self._model_config.id, self._model_config.provider
        )
        self._state.cost_usd += cost
        self._state.pending_cost_usd = 0.0
        self._trace.emit(
            "model_response",
            turn=self._state.turns,
            phase=phase,
            text=response.text,
            stop_reason=response.stop_reason,
            tool_calls=[c.name for c in response.tool_calls],
            usage=response.usage.model_dump(),
            cost_usd=cost,
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
        """
        results: list[dict[str, Any]] = []
        dispatched: list[Any] = []
        for call in response.tool_calls:
            blocked_reason: str | None = None
            for guardrail in self._guardrails:
                decision = guardrail.before_tool_call(self._state, call)
                if decision.kind == "halt":
                    self._trace.emit(
                        "guardrail_triggered",
                        hook="before_tool_call",
                        guardrail=guardrail.name,
                        kind="halt",
                        reason=decision.reason,
                    )
                    return decision.reason, None
                if decision.kind == "block":
                    blocked_reason = decision.reason
                    self._trace.emit(
                        "guardrail_triggered",
                        hook="before_tool_call",
                        guardrail=guardrail.name,
                        kind="block",
                        reason=decision.reason,
                    )
                    break

            if blocked_reason is None:
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
                        blocked_reason = str(
                            outcome.get("reason", f"blocked by hook {outcome['_handler']}")
                        )
                        self._trace.emit(
                            "hook_triggered",
                            event="before_tool_call",
                            hook=outcome["_handler"],
                            action="block",
                            reason=blocked_reason,
                        )
                        break
                    if isinstance(outcome.get("input"), dict):
                        call.input = outcome["input"]
                        self._trace.emit(
                            "hook_triggered",
                            event="before_tool_call",
                            hook=outcome["_handler"],
                            action="patch_input",
                        )

            if blocked_reason is not None:
                results.append(
                    {
                        "tool_use_id": call.id,
                        "content": f"blocked: {blocked_reason}",
                        "is_error": True,
                    }
                )
                continue

            self._trace.emit("tool_call", name=call.name, input=call.input, id=call.id)

            def on_update(progress: str, _call=call) -> None:
                self._trace.emit("tool_update", id=_call.id, name=_call.name, content=progress)

            result = self._registry.dispatch(call, on_update=on_update)
            if result.is_error and self._spec.loop.on_tool_error == "retry_once":
                self._trace.emit("tool_retry", id=call.id, name=call.name)
                result = self._registry.dispatch(call, on_update=on_update)
            if result.is_error and self._spec.loop.on_tool_error == "abort":
                raise ToolAbort(f"tool '{call.name}' failed: {result.content}")

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
                    return decision.reason, None

            self._trace.emit(
                "tool_result",
                id=call.id,
                name=call.name,
                content=result.content,
                is_error=result.is_error,
            )
            dispatched.append(result)
            results.append(
                {"tool_use_id": call.id, "content": result.content, "is_error": result.is_error}
            )
        self._context.add_tool_results(results)

        terminate_output: str | None = None
        if dispatched and len(dispatched) == len(results) and all(
            r.terminate for r in dispatched
        ):
            terminate_output = dispatched[-1].content
        return None, terminate_output

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

    def _verify(self, output: str) -> list[VerdictResult]:
        run_context = {
            "input": self._run_input,
            "harness_dir": str(self._base),
            "output": output,
        }
        verdicts: list[VerdictResult] = []
        for verifier in self._verifiers:
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
        if response.content_blocks:
            return response.content_blocks
        return [{"type": "text", "text": response.text}]

    def _finish(
        self,
        status: str,
        *,
        output: str = "",
        reason: str = "",
        verdicts: list[VerdictResult] | None = None,
    ) -> RunResult:
        self._trace.emit(
            "run_finished",
            status=status,
            reason=reason,
            turns=self._state.turns,
            cost_usd=self._state.cost_usd,
            duration_seconds=self._state.elapsed_seconds(),
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
            duration_seconds=self._state.elapsed_seconds(),
            run_id=self._run_id,
            trace_path=str(self._trace.path),
            verdicts=verdicts or [],
            reason=reason,
        )
