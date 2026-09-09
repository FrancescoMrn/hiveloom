"""Tests for the builtin catalog."""

from __future__ import annotations

from hiveloom import catalog


def test_catalogs_have_expected_kinds():
    assert set(catalog.CATALOGS) == {
        "tools",
        "guardrails",
        "validators",
        "policies",
        "compaction",
        "hooks",
        "datasets",
        "scorers",
    }


def test_known_builtins_present():
    assert "file_read" in catalog.BUILTIN_TOOLS
    assert "max_cost_usd" in catalog.BUILTIN_GUARDRAILS
    assert "output_schema" in catalog.BUILTIN_VALIDATORS
    assert "react" in catalog.POLICIES
    assert "sequential_steps" in catalog.POLICIES


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


def test_shell_rules_are_validated_at_spec_time():
    """`hiveloom validate` must refuse what ShellTool would refuse to build."""
    entry = catalog.BUILTIN_TOOLS["shell"]
    assert catalog.validate_builtin_params(entry, {"commands": ["cat app.log"]}) == []

    problems = catalog.validate_builtin_params(
        entry, {"commands": [{"argv": ["cat"], "allow_extra_args": True}]}
    )
    assert problems and "cannot allow arbitrary extra arguments" in problems[0]
    assert "commands[0]" in problems[0]


def test_shell_rule_shape_problems_name_their_index():
    entry = catalog.BUILTIN_TOOLS["shell"]
    problems = catalog.validate_builtin_params(entry, {"commands": ["ls", 7]})
    assert problems == ["shell commands[1]: shell command rules must be strings or mappings"]


def test_parse_shell_rule_allows_extra_args_for_safe_binaries():
    assert catalog.parse_shell_rule({"argv": ["grep"], "allow_extra_args": True}) == (
        ["grep"],
        True,
    )
    assert catalog.parse_shell_rule("wc -l app.log") == (["wc", "-l", "app.log"], False)
