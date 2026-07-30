"""Tests for schema-derived annotation helpers (template, explain)."""

from __future__ import annotations

import yaml

from hiveloom.spec import annotate
from hiveloom.spec.loader import dump_spec, spec_from_dict
from hiveloom.spec.schema import HarnessSpec


def test_annotated_template_round_trips_through_loader():
    """§14 golden: `hiveloom schema --annotated` must load through the loader."""
    template = annotate.annotated_template()
    data = yaml.safe_load(template)
    spec = spec_from_dict(data, source="<template>")
    assert isinstance(spec, HarnessSpec)
    # And re-dumping is stable.
    assert dump_spec(spec) == dump_spec(spec_from_dict(yaml.safe_load(dump_spec(spec))))


def test_annotated_template_has_comments_for_every_section():
    template = annotate.annotated_template()
    for section in (
        "model:",
        "system_prompt:",
        "tools:",
        "context:",
        "guardrails:",
        "loop:",
        "verify:",
        "logging:",
        "evolution:",
    ):
        assert section in template


def test_json_schema_is_object():
    schema = annotate.json_schema()
    assert schema["type"] == "object"
    assert "properties" in schema


def test_explain_scalar_field():
    info = annotate.explain("loop.max_turns")
    assert info["path"] == "loop.max_turns"
    assert "int" in info["type"]
    assert info["default"] == 20


def test_explain_object_field_lists_subfields():
    info = annotate.explain("context.compaction")
    assert "fields" in info
    assert "trigger_at_pct" in info["fields"]


def test_explain_evolution_auto_propose_lists_subfields():
    """The generic model walker picks up nested models with no special-casing
    needed — same mechanism `context.compaction` already exercises above."""
    info = annotate.explain("evolution.auto_propose")
    assert "fields" in info
    assert set(info["fields"]) == {"enabled", "min_failures", "cooldown_hours", "model"}


def test_annotated_template_surfaces_auto_propose():
    template = annotate.annotated_template()
    assert "auto_propose:" in template
    assert "min_failures:" in template
    assert "cooldown_hours:" in template


def test_explain_literal_choices():
    info = annotate.explain("loop.on_tool_error")
    assert set(info["choices"]) == {"retry_once", "surface_to_model", "abort"}


def test_explain_policy_is_open_string():
    # Policies are catalog entries now, not a closed Literal.
    info = annotate.explain("loop.policy")
    assert info["type"] == "str"
    assert "choices" not in info


def test_explain_loop_steps_surfaces_field():
    info = annotate.explain("loop.steps")
    assert info["type"] == "list[str]"
    assert info["default"] == []


def test_explain_unknown_field_raises():
    try:
        annotate.explain("loop.nope")
    except KeyError as exc:
        assert "unknown field" in str(exc)
    else:
        raise AssertionError("expected KeyError")
