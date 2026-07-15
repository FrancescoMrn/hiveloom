"""Tests for verifiers."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom.spec.schema import HarnessSpec
from hiveloom.verify.base import VerdictResult
from hiveloom.verify.builtin import (
    CodeVerifier,
    CommandSucceedsVerifier,
    FileExistsVerifier,
    OutputSchemaVerifier,
    RegexMatchVerifier,
    build_verifiers,
)


def _schema(tmp_path: Path) -> Path:
    p = tmp_path / "schema.json"
    p.write_text(
        json.dumps(
            {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
        )
    )
    return p


def test_output_schema_pass_and_fail(tmp_path: Path):
    _schema(tmp_path)
    v = OutputSchemaVerifier("schema.json", tmp_path)
    assert v.validate('{"a": "x"}', {}).passed
    bad = v.validate('{"b": 1}', {})
    assert not bad.passed and "schema" in bad.feedback


def test_output_schema_invalid_json(tmp_path: Path):
    _schema(tmp_path)
    v = OutputSchemaVerifier("schema.json", tmp_path)
    result = v.validate("not json", {})
    assert not result.passed and "JSON" in result.feedback


def test_regex_match():
    v = RegexMatchVerifier(r"\bDONE\b")
    assert v.validate("all DONE here", {}).passed
    assert not v.validate("nope", {}).passed


def test_file_exists(tmp_path: Path):
    (tmp_path / "out.txt").write_text("x")
    assert FileExistsVerifier("out.txt", tmp_path).validate("", {}).passed
    assert not FileExistsVerifier("missing.txt", tmp_path).validate("", {}).passed


def test_command_succeeds(tmp_path: Path):
    assert CommandSucceedsVerifier("true", tmp_path).validate("", {}).passed
    assert not CommandSucceedsVerifier("false", tmp_path).validate("", {}).passed


def test_code_verifier_dict_and_verdict():
    v = CodeVerifier(lambda out, ctx: {"passed": False, "feedback": "bad"}, name="c")
    result = v.validate("x", {})
    assert not result.passed and result.feedback == "bad" and result.verifier == "c"

    v2 = CodeVerifier(lambda out, ctx: VerdictResult(passed=True), name="c2")
    assert v2.validate("x", {}).passed


def test_build_verifiers_from_spec(tmp_path: Path):
    _schema(tmp_path)
    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "verify": {"validators": [{"builtin": "output_schema", "schema_file": "schema.json"}]},
        }
    )
    verifiers = build_verifiers(spec, tmp_path)
    assert len(verifiers) == 1 and verifiers[0].name == "output_schema"
