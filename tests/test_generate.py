"""Tests for the generator (schema-derived meta-prompt + plan execution)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom.errors import HiveloomError
from hiveloom.generate.generator import (
    build_meta_prompt,
    generate,
    load_generated,
    parse_plan,
)
from hiveloom.generate.llm import FakeStrongModel, build_strong_model
from hiveloom.models.fake import FakeModelProvider, text_response
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
    assert load_generated(tmp_path / "h") == spec


def test_generate_resolves_default_model_for_sdk(tmp_path: Path, monkeypatch):
    model = FakeStrongModel([json.dumps(_plan())])
    seen: list[tuple[str | None, Path]] = []

    def build(model_id, base):
        seen.append((model_id, base))
        return model

    monkeypatch.setattr("hiveloom.generate.llm.build_strong_model", build)

    spec = generate("Count lines.", tmp_path / "h")

    assert spec.name == "line-counter"
    assert seen == [(None, tmp_path / "h")]


def test_generate_rejects_model_and_model_id_together(tmp_path: Path):
    with pytest.raises(HiveloomError, match="either model or model_id"):
        generate(
            "Count lines.",
            tmp_path / "h",
            FakeStrongModel([json.dumps(_plan())]),
            model_id="provider/model",
        )


def test_fake_strong_model_reports_exhaustion():
    model = FakeStrongModel([])

    with pytest.raises(RuntimeError, match="ran out of scripted responses"):
        model.generate(system="s", user="u")


def test_default_strong_model_requires_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(HiveloomError, match="ANTHROPIC_API_KEY is not set"):
        build_strong_model(None)


def test_strong_model_resolves_registered_provider(monkeypatch, tmp_path: Path):
    from hiveloom import ext

    provider = FakeModelProvider([text_response("generated plan")])
    monkeypatch.setattr(ext, "provider_names", lambda: ["local"])
    monkeypatch.setattr(ext, "build_provider", lambda name, base: provider)

    model = build_strong_model("local/model-id", tmp_path)

    assert model.generate(system="system", user="task") == "generated plan"


def test_strong_model_loads_harness_env(monkeypatch, tmp_path: Path):
    from hiveloom.generate import llm

    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=test-only\n")
    sentinel = FakeStrongModel([])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "ClaudeStrongModel", lambda model_id: sentinel)

    assert build_strong_model("claude-test", tmp_path) is sentinel


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
