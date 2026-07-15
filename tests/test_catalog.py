"""Tests for the builtin catalog."""

from __future__ import annotations

from hiveloom import catalog


def test_catalogs_have_expected_kinds():
    assert set(catalog.CATALOGS) == {
        "tools", "guardrails", "validators", "policies", "compaction", "hooks",
    }


def test_known_builtins_present():
    assert "file_read" in catalog.BUILTIN_TOOLS
    assert "max_cost_usd" in catalog.BUILTIN_GUARDRAILS
    assert "output_schema" in catalog.BUILTIN_VALIDATORS
    assert "react" in catalog.POLICIES


def test_validate_params_accepts_valid():
    entry = catalog.BUILTIN_GUARDRAILS["max_cost_usd"]
    assert catalog.validate_builtin_params(entry, {"value": 0.5}) == []


def test_validate_params_rejects_unknown():
    entry = catalog.BUILTIN_GUARDRAILS["tool_allowlist"]
    problems = catalog.validate_builtin_params(entry, {"value": 1})
    assert problems and "no parameter" in problems[0]


def test_validate_params_rejects_missing_required():
    entry = catalog.BUILTIN_VALIDATORS["output_schema"]
    problems = catalog.validate_builtin_params(entry, {})
    assert problems and "requires parameter" in problems[0]


def test_validate_params_rejects_bad_type():
    entry = catalog.BUILTIN_GUARDRAILS["max_wall_clock_seconds"]
    problems = catalog.validate_builtin_params(entry, {"value": "soon"})
    assert problems and "must be int" in problems[0]


def test_bool_not_accepted_as_int():
    entry = catalog.BUILTIN_GUARDRAILS["max_wall_clock_seconds"]
    problems = catalog.validate_builtin_params(entry, {"value": True})
    assert problems
