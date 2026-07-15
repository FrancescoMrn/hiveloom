"""Tests for the harness spec schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hiveloom.spec.schema import (
    BuiltinGuardrailRef,
    BuiltinToolRef,
    CodeToolRef,
    HarnessSpec,
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
