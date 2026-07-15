"""Tests for the incremental construction API (rollback, scaffolding, events)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct
from hiveloom.errors import SpecError
from hiveloom.spec.loader import load_spec, validate_harness


def test_init_creates_valid_skeleton(tmp_path: Path):
    directory = tmp_path / "h"
    spec = construct.init_harness(directory, name="my-h", task="Task.")
    assert spec.name == "my-h"
    assert (directory / "harness.yaml").exists()
    assert (directory / ".env.example").exists()
    assert (directory / "requirements.txt").exists()
    assert (directory / "README.md").exists()
    assert (directory / ".gitignore").exists()
    # Freshly initialized harness is fully valid.
    validate_harness(directory)


def test_init_refuses_to_clobber(harness_dir: Path):
    with pytest.raises(SpecError, match="already exists"):
        construct.init_harness(harness_dir, name="x", task="y")


def test_set_field_coerces_scalar(harness_dir: Path):
    construct.set_field(harness_dir, "loop.max_turns", value="30")
    spec = load_spec(harness_dir)
    assert spec.loop.max_turns == 30
    assert isinstance(spec.loop.max_turns, int)


def test_set_field_from_file(harness_dir: Path, tmp_path: Path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("You are helpful.\nBe concise.\n")
    construct.set_field(harness_dir, "system_prompt", file=prompt)
    spec = load_spec(harness_dir)
    assert "Be concise." in spec.system_prompt


def test_set_invalid_rolls_back(harness_dir: Path):
    before = (harness_dir / "harness.yaml").read_text()
    with pytest.raises(SpecError):
        construct.set_field(harness_dir, "loop.max_turns", value="0")
    assert (harness_dir / "harness.yaml").read_text() == before


def test_add_builtin_tool(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="file_read")
    spec = load_spec(harness_dir)
    assert any(getattr(t, "builtin", None) == "file_read" for t in spec.tools)


def test_add_code_tool_scaffolds_stub(harness_dir: Path):
    construct.add_tool(
        harness_dir, code="tools/fetch.py:fetch", description="Fetch a thing."
    )
    stub = harness_dir / "tools" / "fetch.py"
    assert stub.exists()
    assert "def fetch(" in stub.read_text()
    validate_harness(harness_dir)


def test_add_code_tool_requires_description(harness_dir: Path):
    with pytest.raises(SpecError, match="requires --description"):
        construct.add_tool(harness_dir, code="tools/x.py:go")
    assert not (harness_dir / "tools" / "x.py").exists()


def test_add_unknown_builtin_rolls_back_and_no_stub(harness_dir: Path):
    before = (harness_dir / "harness.yaml").read_text()
    with pytest.raises(SpecError, match="unknown"):
        construct.add_tool(harness_dir, builtin="does_not_exist")
    assert (harness_dir / "harness.yaml").read_text() == before


def test_add_validator_scaffolds_stub(harness_dir: Path):
    construct.add_validator(harness_dir, code="validators/check.py:validate")
    stub = harness_dir / "validators" / "check.py"
    assert stub.exists()
    assert "def validate(run_output, run_context)" in stub.read_text()


def test_add_builtin_validator_with_params(harness_dir: Path):
    construct.add_validator(
        harness_dir, builtin="output_schema", schema_file="./schemas/output.json"
    )
    spec = load_spec(harness_dir)
    assert spec.verify.validators[-1].params()["schema_file"] == "./schemas/output.json"


def test_add_guardrail(harness_dir: Path):
    construct.add_guardrail(harness_dir, builtin="max_wall_clock_seconds", value=120)
    spec = load_spec(harness_dir)
    assert any(
        getattr(g, "builtin", None) == "max_wall_clock_seconds" for g in spec.guardrails
    )


def test_remove_tool_by_name(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="file_read")
    construct.remove_item(harness_dir, "file_read")
    spec = load_spec(harness_dir)
    assert not any(getattr(t, "builtin", None) == "file_read" for t in spec.tools)


def test_remove_nonexistent_raises(harness_dir: Path):
    with pytest.raises(SpecError, match="nothing named"):
        construct.remove_item(harness_dir, "not-a-thing")


def test_construction_events_logged(harness_dir: Path):
    construct.set_field(harness_dir, "loop.max_turns", value="15")
    log = harness_dir / ".hiveloom" / "traces" / "construction.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    commands = [e["command"] for e in events]
    assert "init" in commands
    assert "set" in commands
    assert all(e["type"] == "construction_event" for e in events)


def test_failed_construction_logged_as_error(harness_dir: Path):
    with pytest.raises(SpecError):
        construct.set_field(harness_dir, "loop.max_turns", value="0")
    log = harness_dir / ".hiveloom" / "traces" / "construction.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(e["outcome"] == "error" for e in events)
