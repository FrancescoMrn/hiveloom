"""Tests for the YAML loader, round-trip dumping, and code-hook resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveloom.errors import SpecError
from hiveloom.spec.loader import (
    atomic_write_text,
    dump_spec,
    load_spec,
    resolve_hooks,
    validate_harness,
)
from hiveloom.spec.schema import HarnessSpec

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"


def test_example_harness_validates():
    spec = validate_harness(EXAMPLE_HARNESS)
    assert spec.name == "example-summarizer"


def test_dump_load_round_trip_is_stable():
    spec = load_spec(EXAMPLE_HARNESS)
    dumped = dump_spec(spec)
    reloaded = HarnessSpec.model_validate_json(reloaded_json(dumped))
    assert dump_spec(reloaded) == dumped


def test_atomic_write_replaces_contents_without_leaving_temp_files(tmp_path: Path):
    target = tmp_path / "harness.yaml"
    target.write_text("old")

    atomic_write_text(target, "new")

    assert target.read_text() == "new"
    assert list(tmp_path.glob(".harness.yaml.*")) == []


def reloaded_json(dumped_yaml: str) -> str:
    import json

    import yaml

    return json.dumps(yaml.safe_load(dumped_yaml))


def test_missing_file_raises_spec_error(tmp_path: Path):
    with pytest.raises(SpecError, match="no harness spec"):
        load_spec(tmp_path)


def test_invalid_yaml_raises_spec_error(tmp_path: Path):
    (tmp_path / "harness.yaml").write_text("name: [unclosed\n")
    with pytest.raises(SpecError, match="could not parse YAML"):
        load_spec(tmp_path)


def test_validation_error_is_actionable(tmp_path: Path):
    (tmp_path / "harness.yaml").write_text("description: d\nsystem_prompt: sp\n")
    with pytest.raises(SpecError, match="name"):
        load_spec(tmp_path)


def test_resolve_hooks_missing_file(tmp_path: Path):
    (tmp_path / "harness.yaml").write_text(
        "name: t\ndescription: d\nsystem_prompt: sp\n"
        "tools:\n- code: tools/missing.py:go\n  description: x\n"
    )
    spec = load_spec(tmp_path)
    with pytest.raises(SpecError, match="not found"):
        resolve_hooks(spec, tmp_path)


def test_resolve_hooks_bad_validator_signature(tmp_path: Path):
    (tmp_path / "validators").mkdir()
    (tmp_path / "validators" / "v.py").write_text("def validate(only_one):\n    return None\n")
    (tmp_path / "harness.yaml").write_text(
        "name: t\ndescription: d\nsystem_prompt: sp\n"
        "verify:\n  validators:\n  - code: validators/v.py:validate\n"
    )
    spec = load_spec(tmp_path)
    with pytest.raises(SpecError, match="run_output, run_context"):
        resolve_hooks(spec, tmp_path)


def test_resolve_hooks_good_validator_signature(tmp_path: Path):
    (tmp_path / "validators").mkdir()
    (tmp_path / "validators" / "v.py").write_text(
        "def validate(run_output, run_context):\n    return {'passed': True}\n"
    )
    (tmp_path / "harness.yaml").write_text(
        "name: t\ndescription: d\nsystem_prompt: sp\n"
        "verify:\n  validators:\n  - code: validators/v.py:validate\n"
    )
    spec = load_spec(tmp_path)
    resolve_hooks(spec, tmp_path)  # must not raise
