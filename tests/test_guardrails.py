"""Tests for guardrails and their hook points."""

from __future__ import annotations

from pathlib import Path

from hiveloom.guardrails.base import RunState
from hiveloom.guardrails.builtin import (
    CodeGuardrail,
    MaxCostGuardrail,
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


def test_build_guardrails_includes_injected_cost(tmp_path: Path):
    spec = HarnessSpec.model_validate({"name": "t", "description": "d", "system_prompt": "sp"})
    registry = ToolRegistry()
    guardrails = build_guardrails(spec, registry, tmp_path)
    assert any(isinstance(g, MaxCostGuardrail) for g in guardrails)
