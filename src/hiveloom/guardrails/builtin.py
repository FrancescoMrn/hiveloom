"""Builtin guardrails and the factory that builds them from a spec.

Guardrails are frozen from evolution by design and enforced by the evolver.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hiveloom import ext
from hiveloom.guardrails.base import Allow, Block, Decision, Guardrail, Halt, RunState
from hiveloom.models.provider import ModelResponse, ToolCall
from hiveloom.spec.loader import import_hook
from hiveloom.spec.schema import (
    BuiltinGuardrailRef,
    CodeGuardrailRef,
    HarnessSpec,
)
from hiveloom.tools.registry import ToolRegistry


class MaxCostGuardrail(Guardrail):
    name = "max_cost_usd"

    def __init__(self, value: float):
        self._limit = float(value)

    def before_model_call(self, state: RunState) -> Decision:
        if state.cost_usd >= self._limit:
            return Halt(f"cost ${state.cost_usd:.4f} reached limit ${self._limit:.2f}")
        if state.cost_usd + state.pending_cost_usd > self._limit:
            return Halt(
                f"estimated next call (${state.pending_cost_usd:.4f}) would exceed "
                f"cost limit ${self._limit:.2f}"
            )
        return Allow()

    def after_model_response(self, state: RunState, response: ModelResponse) -> Decision:
        if state.cost_usd > self._limit:
            return Halt(f"cost ${state.cost_usd:.4f} exceeded limit ${self._limit:.2f}")
        return Allow()


class MaxWallClockGuardrail(Guardrail):
    name = "max_wall_clock_seconds"

    def __init__(self, value: int):
        self._limit = float(value)

    def before_model_call(self, state: RunState) -> Decision:
        if state.elapsed_seconds() > self._limit:
            return Halt(f"wall clock exceeded {self._limit:.0f}s")
        return Allow()


class MaxTurnsHardCapGuardrail(Guardrail):
    name = "max_turns_hard_cap"

    def __init__(self, value: int):
        self._limit = int(value)

    def before_model_call(self, state: RunState) -> Decision:
        if state.model_calls >= self._limit:
            return Halt(f"turns reached hard cap {self._limit}")
        return Allow()


class ToolAllowlistGuardrail(Guardrail):
    name = "tool_allowlist"

    def before_tool_call(self, state: RunState, call: ToolCall) -> Decision:
        if call.name not in state.tool_names:
            return Block(f"tool '{call.name}' is not a registered tool")
        return Allow()


class NoNetworkWriteGuardrail(Guardrail):
    name = "no_network_write"

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def before_tool_call(self, state: RunState, call: ToolCall) -> Decision:
        tool = self._registry.get(call.name)
        if tool is not None and "network" in tool.tags and "write" in tool.tags:
            return Block(f"tool '{call.name}' is tagged network+write and is blocked")
        return Allow()


class RegexOutputFilterGuardrail(Guardrail):
    name = "regex_output_filter"

    def __init__(self, pattern: str):
        self._pattern = re.compile(pattern)

    def on_output(self, state: RunState, output: str) -> Decision:
        if self._pattern.search(output):
            return Block(f"output blocked by filter /{self._pattern.pattern}/")
        return Allow()


class CodeGuardrail(Guardrail):
    """Wraps a user code-hook guardrail, invoked on final output."""

    def __init__(self, func, name: str):
        self._func = func
        self.name = name

    def on_output(self, state: RunState, output: str) -> Decision:
        verdict = self._func(
            {"output": output, "cost_usd": state.cost_usd, "turns": state.turns}
        )
        return _decision_from_hook(verdict)


def _decision_from_hook(verdict: Any) -> Decision:
    if isinstance(verdict, Decision):
        return verdict
    if isinstance(verdict, dict):
        decision = str(verdict.get("decision", "allow")).lower()
        reason = str(verdict.get("reason", ""))
        if decision == "block":
            return Block(reason or "blocked by code guardrail")
        if decision == "halt":
            return Halt(reason or "halted by code guardrail")
    return Allow()


def build_guardrails(
    spec: HarnessSpec, registry: ToolRegistry, base_dir: str | Path
) -> list[Guardrail]:
    """Instantiate guardrails (builtins + code hooks) from a spec."""
    base = Path(base_dir)
    if base.is_file():
        base = base.parent

    guardrails: list[Guardrail] = []
    for ref in spec.guardrails:
        if isinstance(ref, BuiltinGuardrailRef):
            guardrails.append(_make_builtin(ref, registry))
        elif isinstance(ref, CodeGuardrailRef):
            func = import_hook(ref.code, base)
            _, func_name = ref.code.rsplit(":", 1)
            guardrails.append(CodeGuardrail(func, name=func_name))
    return guardrails


def _make_builtin(ref: BuiltinGuardrailRef, registry: ToolRegistry) -> Guardrail:
    return ext.build(
        "guardrails", ref.builtin, ref.params(), ext.BuildContext(tool_registry=registry)
    )


def _register_factories() -> None:
    ext.register_builtin_factory(
        "guardrails", "max_cost_usd", lambda p, _c: MaxCostGuardrail(p["value"])
    )
    ext.register_builtin_factory(
        "guardrails", "max_wall_clock_seconds", lambda p, _c: MaxWallClockGuardrail(p["value"])
    )
    ext.register_builtin_factory(
        "guardrails", "max_turns_hard_cap", lambda p, _c: MaxTurnsHardCapGuardrail(p["value"])
    )
    ext.register_builtin_factory(
        "guardrails", "tool_allowlist", lambda _p, _c: ToolAllowlistGuardrail()
    )
    ext.register_builtin_factory(
        "guardrails", "no_network_write", lambda _p, ctx: NoNetworkWriteGuardrail(ctx.tool_registry)
    )
    ext.register_builtin_factory(
        "guardrails", "regex_output_filter", lambda p, _c: RegexOutputFilterGuardrail(p["pattern"])
    )


_register_factories()
