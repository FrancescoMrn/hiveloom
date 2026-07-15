"""Tests for the generator (schema-derived meta-prompt + plan execution)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom.errors import HiveloomError
from hiveloom.generate.generator import build_meta_prompt, generate, parse_plan
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.spec import annotate
from hiveloom.spec.loader import validate_harness


def _plan(**overrides) -> dict:
    plan = {
        "name": "line-counter",
        "task": "Count lines in a text file.",
        "steps": [
            {"op": "set", "path": "system_prompt", "value": "Output JSON."},
            {"op": "set", "path": "loop.max_turns", "value": 8},
            {"op": "add_tool", "builtin": "file_read"},
            {"op": "add_validator", "builtin": "regex_match", "pattern": "lines"},
        ],
    }
    plan.update(overrides)
    return plan


def test_meta_prompt_contains_current_json_schema():
    """§14 golden: the meta-prompt embeds the live JSON schema verbatim."""
    prompt = build_meta_prompt()
    assert json.dumps(annotate.json_schema(), indent=2) in prompt
    # And the builtin catalog is present.
    assert "file_read" in prompt and "max_cost_usd" in prompt


def test_generate_builds_valid_harness(tmp_path: Path):
    model = FakeStrongModel([json.dumps(_plan())])
    spec = generate("Count lines.", tmp_path / "h", model)
    assert spec.name == "line-counter"
    assert spec.loop.max_turns == 8
    assert any(getattr(t, "builtin", None) == "file_read" for t in spec.tools)
    validate_harness(tmp_path / "h")  # fully valid


def test_generate_repairs_after_bad_first_plan(tmp_path: Path):
    model = FakeStrongModel(["not json at all", json.dumps(_plan())])
    spec = generate("Count lines.", tmp_path / "h", model)
    assert spec.name == "line-counter"
    assert len(model.prompts) == 2  # one repair round used


def test_generate_gives_up_after_max_repairs(tmp_path: Path):
    model = FakeStrongModel(["bad", "still bad", "nope"])
    with pytest.raises(HiveloomError, match="generation failed"):
        generate("x", tmp_path / "h", model, max_repairs=2)
    assert len(model.prompts) == 3


def test_generate_scaffolds_code_hook(tmp_path: Path):
    plan = _plan(
        steps=[
            {"op": "set", "path": "system_prompt", "value": "sp"},
            {"op": "add_tool", "code": "tools/fetch.py:fetch", "description": "Fetch a thing."},
        ]
    )
    generate("x", tmp_path / "h", FakeStrongModel([json.dumps(plan)]))
    assert (tmp_path / "h" / "tools" / "fetch.py").exists()


def test_generate_output_schema_scaffolds_schema_file(tmp_path: Path):
    plan = _plan(
        steps=[
            {"op": "set", "path": "system_prompt", "value": "sp"},
            {"op": "add_validator", "builtin": "output_schema", "schema_file": "./schemas/o.json"},
        ]
    )
    generate("x", tmp_path / "h", FakeStrongModel([json.dumps(plan)]))
    assert (tmp_path / "h" / "schemas" / "o.json").exists()


def test_parse_plan_tolerates_code_fences():
    fenced = "```json\n" + json.dumps(_plan()) + "\n```"
    plan = parse_plan(fenced)
    assert plan["name"] == "line-counter"


def test_parse_plan_rejects_missing_fields():
    with pytest.raises(HiveloomError, match="name"):
        parse_plan(json.dumps({"task": "x"}))


def test_generate_scans_env_vars(tmp_path: Path):
    """Env vars referenced by generated code hooks are added to .env.example."""
    plan = _plan(
        steps=[
            {"op": "set", "path": "system_prompt", "value": "sp"},
            {"op": "add_tool", "code": "tools/fetch.py:fetch", "description": "Fetch."},
        ]
    )
    harness = tmp_path / "h"
    generate("x", harness, FakeStrongModel([json.dumps(plan)]))
    # Rewrite the scaffolded hook to reference an env var, then re-finalize.
    from hiveloom.generate.generator import _finalize

    (harness / "tools" / "fetch.py").write_text(
        "import os\n\ndef fetch(q: str) -> str:\n    return os.environ['ERP_API_KEY']\n"
    )
    found = _finalize(harness)
    assert "ERP_API_KEY" in found
    assert "ERP_API_KEY=" in (harness / ".env.example").read_text()
