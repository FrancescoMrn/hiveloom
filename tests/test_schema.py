"""Tests for the harness spec schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hiveloom.spec.schema import (
    ALWAYS_FROZEN,
    BuiltinGuardrailRef,
    BuiltinToolRef,
    CodeToolRef,
    HarnessSpec,
    McpHttpServerRef,
    McpStdioServerRef,
)


def _minimal(**overrides) -> dict:
    data = {
        "name": "t",
        "description": "d",
        "system_prompt": "sp",
    }
    data.update(overrides)
    return data


def _cost_guardrails(spec) -> list[BuiltinGuardrailRef]:
    return [
        g
        for g in spec.guardrails
        if isinstance(g, BuiltinGuardrailRef) and g.builtin == "max_cost_usd"
    ]


def test_minimal_spec_applies_defaults():
    spec = HarnessSpec.model_validate(_minimal())
    assert spec.model.id == "claude-haiku-4-5"
    assert spec.loop.policy == "react"
    assert spec.context.compaction.trigger_at_pct == 80


def test_cost_guardrail_injected_when_missing():
    spec = HarnessSpec.model_validate(_minimal())
    cost = _cost_guardrails(spec)
    assert len(cost) == 1
    assert cost[0].params()["value"] == 1.00


def test_cost_guardrail_not_duplicated_when_present():
    spec = HarnessSpec.model_validate(
        _minimal(guardrails=[{"builtin": "max_cost_usd", "value": 0.5}])
    )
    cost = _cost_guardrails(spec)
    assert len(cost) == 1
    assert cost[0].params()["value"] == 0.5


def test_tool_ref_discrimination():
    spec = HarnessSpec.model_validate(
        _minimal(
            tools=[
                {"builtin": "file_read"},
                {"code": "tools/x.py:go", "description": "hi"},
            ]
        )
    )
    assert isinstance(spec.tools[0], BuiltinToolRef)
    assert isinstance(spec.tools[1], CodeToolRef)


def test_unknown_builtin_tool_rejected():
    with pytest.raises(ValidationError, match="unknown tool builtin"):
        HarnessSpec.model_validate(_minimal(tools=[{"builtin": "nope"}]))


def test_builtin_param_unknown_rejected():
    with pytest.raises(ValidationError, match="no parameter"):
        HarnessSpec.model_validate(
            _minimal(guardrails=[{"builtin": "tool_allowlist", "value": 1}])
        )


def test_mcp_server_ref_discrimination():
    # The union dispatches on the literal `transport` tag, so it must be
    # explicit in raw data even though the field itself has a default (that
    # default only helps direct construction of the concrete class).
    spec = HarnessSpec.model_validate(
        _minimal(
            mcp_servers=[
                {"name": "s1", "transport": "stdio", "command": "prog"},
                {"name": "s2", "transport": "http", "url": "https://example.invalid/mcp"},
            ]
        )
    )
    assert isinstance(spec.mcp_servers[0], McpStdioServerRef)
    assert spec.mcp_servers[0].transport == "stdio"
    assert isinstance(spec.mcp_servers[1], McpHttpServerRef)
    assert spec.mcp_servers[1].transport == "http"


def test_mcp_server_name_rejects_unsafe_charset():
    with pytest.raises(ValidationError, match=r"a-zA-Z0-9_-"):
        HarnessSpec.model_validate(
            _minimal(mcp_servers=[{"name": "bad name!", "transport": "stdio", "command": "x"}])
        )


def test_mcp_server_names_must_be_unique():
    with pytest.raises(ValidationError, match="duplicate"):
        HarnessSpec.model_validate(
            _minimal(
                mcp_servers=[
                    {"name": "dup", "transport": "stdio", "command": "a"},
                    {"name": "dup", "transport": "http", "url": "https://example.invalid"},
                ]
            )
        )


def test_mcp_servers_extra_fields_forbidden():
    with pytest.raises(ValidationError, match="not_a_field"):
        HarnessSpec.model_validate(
            _minimal(
                mcp_servers=[
                    {"name": "s", "transport": "stdio", "command": "x", "not_a_field": 1}
                ]
            )
        )


def test_mcp_servers_is_always_frozen():
    assert "mcp_servers" in ALWAYS_FROZEN


def test_builtin_required_param_missing_rejected():
    with pytest.raises(ValidationError, match="requires parameter"):
        HarnessSpec.model_validate(
            _minimal(verify={"validators": [{"builtin": "output_schema"}]})
        )


def test_code_tool_requires_valid_format():
    with pytest.raises(ValidationError, match="code hook"):
        HarnessSpec.model_validate(
            _minimal(tools=[{"code": "no-colon-here", "description": "x"}])
        )


def test_extra_top_level_field_forbidden():
    with pytest.raises(ValidationError):
        HarnessSpec.model_validate(_minimal(unexpected="boom"))


def test_max_turns_must_be_positive():
    with pytest.raises(ValidationError):
        HarnessSpec.model_validate(_minimal(loop={"max_turns": 0}))
