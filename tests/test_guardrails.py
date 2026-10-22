"""Tests for guardrails and their hook points."""

from __future__ import annotations

from pathlib import Path

from hiveloom.guardrails.base import Decision, RunState
from hiveloom.guardrails.builtin import (
    CodeGuardrail,
    MaxCostGuardrail,
    MaxTurnsHardCapGuardrail,
    MaxWallClockGuardrail,
    NoNetworkWriteGuardrail,
    RegexOutputFilterGuardrail,
    ToolAllowlistGuardrail,
    build_guardrails,
)
from hiveloom.models.provider import ModelResponse, ToolCall, Usage
from hiveloom.spec.schema import HarnessSpec
from hiveloom.tools.builtin import HttpGetTool
from hiveloom.tools.registry import ToolRegistry


def test_max_cost_halts_after_response():
    g = MaxCostGuardrail(0.5)
    state = RunState(cost_usd=0.6)
    assert g.after_model_response(state, ModelResponse(usage=Usage())).kind == "halt"


def test_max_cost_allows_under_limit():
    g = MaxCostGuardrail(0.5)
    assert g.after_model_response(RunState(cost_usd=0.1), ModelResponse()).kind == "allow"


def test_max_cost_blocks_a_call_that_would_overspend():
    g = MaxCostGuardrail(0.5)
    state = RunState(cost_usd=0.4, pending_cost_usd=0.2)
    assert g.before_model_call(state).kind == "halt"


def test_tool_allowlist_blocks_unknown():
    g = ToolAllowlistGuardrail()
    state = RunState(tool_names={"file_read"})
    assert g.before_tool_call(state, ToolCall(id="1", name="evil", input={})).kind == "block"
    assert g.before_tool_call(state, ToolCall(id="1", name="file_read", input={})).kind == "allow"


def test_no_network_write_blocks_network_write_tool(tmp_path: Path):
    registry = ToolRegistry()
    tool = HttpGetTool(tmp_path)
    tool.tags = ["network", "write"]  # simulate a network+write tool
    registry.register(tool)
    g = NoNetworkWriteGuardrail(registry)
    decision = g.before_tool_call(RunState(), ToolCall(id="1", name="http_get", input={}))
    assert decision.kind == "block"


def test_regex_output_filter_blocks_match():
    g = RegexOutputFilterGuardrail(r"api[_-]?key")
    assert g.on_output(RunState(), "here is my api_key=x").kind == "block"
    assert g.on_output(RunState(), "clean output").kind == "allow"


def test_code_guardrail_interprets_dict():
    g = CodeGuardrail(lambda ctx: {"decision": "block", "reason": "nope"}, name="c")
    assert g.on_output(RunState(), "x").kind == "block"


def test_max_cost_exposes_limit():
    g = MaxCostGuardrail(0.5)
    assert g.limit == 0.5


def test_max_cost_before_model_call_halts_when_reached():
    g = MaxCostGuardrail(0.5)
    state = RunState(cost_usd=0.5)
    decision = g.before_model_call(state)
    assert decision.kind == "halt"


def test_max_cost_before_model_call_allows_under_limit():
    g = MaxCostGuardrail(0.5)
    state = RunState(cost_usd=0.1, pending_cost_usd=0.1)
    assert g.before_model_call(state).kind == "allow"


def test_max_wall_clock_halts_past_limit():
    g = MaxWallClockGuardrail(10)
    state = RunState()
    state.started_at -= 100  # backdate the start so elapsed exceeds the cap
    assert g.before_model_call(state).kind == "halt"


def test_max_wall_clock_allows_under_limit():
    g = MaxWallClockGuardrail(10)
    assert g.before_model_call(RunState()).kind == "allow"


def test_max_turns_hard_cap_halts_when_reached():
    g = MaxTurnsHardCapGuardrail(3)
    assert g.before_model_call(RunState(model_calls=3)).kind == "halt"


def test_max_turns_hard_cap_allows_under_limit():
    g = MaxTurnsHardCapGuardrail(3)
    assert g.before_model_call(RunState(model_calls=2)).kind == "allow"


def test_no_network_write_allows_non_network_write_tools(tmp_path: Path):
    registry = ToolRegistry()
    # A registered network-only tool and a registered write-only tool each miss
    # one tag, so the guardrail must allow them.
    network_only = HttpGetTool(tmp_path)
    network_only.name = "net"
    network_only.tags = ["network"]
    registry.register(network_only)
    write_only = HttpGetTool(tmp_path)
    write_only.name = "write"
    write_only.tags = ["write"]
    registry.register(write_only)
    g = NoNetworkWriteGuardrail(registry)
    net = g.before_tool_call(RunState(), ToolCall(id="1", name="net", input={}))
    write = g.before_tool_call(RunState(), ToolCall(id="1", name="write", input={}))
    assert net.kind == "allow"
    assert write.kind == "allow"


def test_code_guardrail_passes_through_decision():
    verdict = Decision("block", "via decision")
    g = CodeGuardrail(lambda ctx: verdict, name="c")
    # The hook's Decision must be returned as-is, not reconstructed.
    assert g.on_output(RunState(), "x") is verdict


def test_code_guardrail_interprets_halt_dict():
    g = CodeGuardrail(lambda ctx: {"decision": "halt", "reason": "stop"}, name="c")
    decision = g.on_output(RunState(), "x")
    assert decision.kind == "halt"
    assert "stop" in decision.reason


def test_code_guardrail_uses_default_reasons():
    g_block = CodeGuardrail(lambda ctx: {"decision": "block"}, name="c")
    assert g_block.on_output(RunState(), "x").reason == "blocked by code guardrail"
    g_halt = CodeGuardrail(lambda ctx: {"decision": "halt"}, name="c")
    assert g_halt.on_output(RunState(), "x").reason == "halted by code guardrail"


def test_code_guardrail_allows_unknown_or_allow():
    g = CodeGuardrail(lambda ctx: {"decision": "maybe"}, name="c")
    assert g.on_output(RunState(), "x").kind == "allow"


def test_build_guardrails_builds_code_hook_from_file_base(tmp_path: Path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "check.py").write_text(
        "def gate(ctx):\n    return {'decision': 'allow', 'reason': 'ok'}\n"
    )
    # Pass a file as base_dir: build_guardrails takes its parent as the base,
    # so the code hook resolves relative to tmp_path.
    manifest = tmp_path / "harness.yaml"
    manifest.write_text("")
    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "guardrails": [{"code": "hooks/check.py:gate"}],
        }
    )
    guardrails = build_guardrails(spec, ToolRegistry(), manifest)
    # Code hook built from the parent base, plus the injected cost guardrail.
    assert any(isinstance(g, CodeGuardrail) for g in guardrails)
    assert any(isinstance(g, MaxCostGuardrail) for g in guardrails)


def test_build_guardrails_includes_injected_cost(tmp_path: Path):
    spec = HarnessSpec.model_validate({"name": "t", "description": "d", "system_prompt": "sp"})
    registry = ToolRegistry()
    guardrails = build_guardrails(spec, registry, tmp_path)
    assert any(isinstance(g, MaxCostGuardrail) for g in guardrails)
