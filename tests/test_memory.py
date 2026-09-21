"""Tests for durable harness memory: the spec section, its budgets, the
rendered system-prompt section, and the operator CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from hiveloom import construct, runner
from hiveloom.cli import app
from hiveloom.context.manager import ContextManager
from hiveloom.errors import ExitCode, SpecError
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import HarnessSpec

cli_runner = CliRunner()


def _entry(entry_id: str = "iso-dates", **overrides) -> dict:
    entry = {
        "id": entry_id,
        "kind": "rule",
        "title": "Dates in ISO 8601",
        "content": "Emit dates as YYYY-MM-DD; the validators reject locale formats.",
    }
    entry.update(overrides)
    return entry


def _spec(**memory) -> HarnessSpec:
    return HarnessSpec.model_validate(
        {"name": "t", "description": "d", "system_prompt": "sp", "memory": memory}
    )


def _json(result) -> dict:
    return json.loads(result.stdout)


# --------------------------------------------------------------------------- #
# Schema: shape and budgets
# --------------------------------------------------------------------------- #
def test_memory_defaults_to_an_empty_enabled_store():
    memory = _spec().memory
    assert memory.enabled is True
    assert (memory.max_entries, memory.max_entry_chars, memory.prompt_budget_chars) == (
        24,
        600,
        6000,
    )
    assert memory.entries == []
    assert memory.render() == ""


def test_memory_entry_ids_must_be_unique():
    with pytest.raises(ValidationError, match="duplicate memory entry id 'iso-dates'"):
        _spec(entries=[_entry(), _entry()])


def test_memory_refuses_more_entries_than_max_entries():
    entries = [_entry(f"lesson-{index}") for index in range(3)]
    with pytest.raises(ValidationError, match="memory holds 3 entries"):
        _spec(max_entries=2, entries=entries)


def test_memory_entry_content_is_bounded_and_names_the_entry():
    with pytest.raises(ValidationError, match="memory entry 'iso-dates' content is"):
        _spec(max_entry_chars=10, entries=[_entry()])


def test_memory_prompt_budget_bounds_the_rendered_section():
    entries = [_entry(f"lesson-{index}", content="x" * 400) for index in range(4)]
    with pytest.raises(ValidationError, match="rendered memory section is"):
        _spec(prompt_budget_chars=500, entries=entries)


def test_memory_entry_kind_is_an_enum():
    with pytest.raises(ValidationError):
        _spec(entries=[_entry(kind="hunch")])


def test_memory_entry_id_must_be_a_slug():
    with pytest.raises(ValidationError):
        _spec(entries=[_entry("Not A Slug")])


def test_memory_created_at_must_be_a_timestamp():
    with pytest.raises(ValidationError, match="ISO 8601"):
        _spec(entries=[_entry(created_at="last tuesday")])


def test_memory_budgets_have_hard_caps_an_operator_cannot_raise():
    for field, value in (
        ("max_entries", 201),
        ("max_entry_chars", 4001),
        ("prompt_budget_chars", 40_001),
    ):
        with pytest.raises(ValidationError):
            _spec(**{field: value})


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_is_one_terse_bullet_per_entry_in_declaration_order():
    spec = _spec(
        entries=[
            _entry("iso-dates"),
            _entry("nulls", kind="fact", title="Nulls", content="Missing\n  values\tare null."),
        ]
    )

    lines = spec.memory.render().splitlines()

    assert lines[0] == "# Memory"
    assert lines[2].startswith("- [rule] Dates in ISO 8601: Emit dates as YYYY-MM-DD")
    # Whitespace collapses: one entry is always exactly one line of prompt.
    assert lines[3] == "- [fact] Nulls: Missing values are null."


def test_system_prompt_carries_the_memory_section_after_the_skills_index():
    spec = _spec(entries=[_entry()])
    manager = ContextManager(spec, FakeModelProvider([]))

    system = manager.system()

    assert system.startswith("sp")
    assert "# Memory" in system
    assert "Emit dates as YYYY-MM-DD" in system


def test_disabled_memory_keeps_the_entries_but_shows_the_model_none():
    spec = _spec(enabled=False, entries=[_entry()])
    manager = ContextManager(spec, FakeModelProvider([]))

    assert spec.memory.entries  # still in the spec, still reviewable
    assert "# Memory" not in manager.system()


def test_memory_reaches_the_journalled_system_prompt(harness_dir: Path):
    """`trace --verify` and `fork` reproduce what the model saw, so the section
    has to be inside the journalled `context_system` payload, not bolted on."""
    construct.add_memory_entry(
        harness_dir, kind="rule", title="Dates in ISO 8601", content="Emit YYYY-MM-DD."
    )
    result = runner.run_harness(
        harness_dir, "do the thing", provider=FakeModelProvider([text_response("done")])
    )

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    system = next(e for e in events if e["type"] == "context_system")["payload"]["system"]
    assert "# Memory" in system
    assert "- [rule] Dates in ISO 8601: Emit YYYY-MM-DD." in system


# --------------------------------------------------------------------------- #
# Construct: the builder-side write path
# --------------------------------------------------------------------------- #
def test_add_memory_entry_slugs_the_title_and_stamps_the_time(harness_dir: Path):
    spec = construct.add_memory_entry(
        harness_dir,
        kind="rule",
        title="Dates in ISO 8601",
        content="Emit YYYY-MM-DD.",
        source="operator",
        evidence="4 runs failed date_format",
    )

    entry = spec.memory.entries[0]
    assert entry.id == "dates-in-iso-8601"
    assert (entry.source, entry.evidence) == ("operator", "4 runs failed date_format")
    assert entry.created_at is not None
    assert load_spec(harness_dir).memory.entries[0].id == "dates-in-iso-8601"


def test_add_memory_entry_refuses_a_duplicate_id(harness_dir: Path):
    construct.add_memory_entry(harness_dir, kind="fact", title="Nulls", content="Null is null.")
    with pytest.raises(SpecError, match="already exists"):
        construct.add_memory_entry(
            harness_dir, kind="fact", title="Nulls", content="Something else."
        )


def test_add_memory_entry_over_budget_rolls_back(harness_dir: Path):
    construct.set_value(harness_dir, "memory.max_entries", 1)
    construct.add_memory_entry(harness_dir, kind="fact", title="First", content="One.")
    before = (harness_dir / "harness.yaml").read_text()

    with pytest.raises(Exception, match="memory.max_entries"):
        construct.add_memory_entry(harness_dir, kind="fact", title="Second", content="Two.")

    assert (harness_dir / "harness.yaml").read_text() == before


def test_forget_memory_entry_removes_only_that_entry(harness_dir: Path):
    construct.add_memory_entry(harness_dir, kind="fact", title="First", content="One.")
    construct.add_memory_entry(harness_dir, kind="fact", title="Second", content="Two.")

    spec = construct.forget_memory_entry(harness_dir, "first")

    assert [entry.id for entry in spec.memory.entries] == ["second"]
    with pytest.raises(SpecError, match="no memory entry with id 'first'"):
        construct.forget_memory_entry(harness_dir, "first")


def test_a_bare_remove_never_reaches_memory(harness_dir: Path):
    """`hiveloom remove <name>` sweeps the list sections by builtin/code/name;
    a lesson is addressed by id through `memory forget` alone."""
    construct.add_memory_entry(harness_dir, kind="fact", title="Nulls", content="Null is null.")

    with pytest.raises(SpecError, match="nothing named or located at 'nulls'"):
        construct.remove_item(harness_dir, "nulls")


def test_memory_slug_refuses_a_title_it_cannot_slug():
    with pytest.raises(SpecError, match="pass an explicit id"):
        construct.memory_slug("!!!")


def test_an_untouched_harness_keeps_its_exact_yaml(harness_dir: Path):
    """An all-default memory section is omitted on rewrite, so a harness that
    has learned nothing keeps its version hash and its fitness bucket."""
    before = (harness_dir / "harness.yaml").read_text()
    construct.set_value(harness_dir, "loop.max_turns", 21)
    after = (harness_dir / "harness.yaml").read_text()

    assert "memory:" not in before
    assert "memory:" not in after

    construct.add_memory_entry(harness_dir, kind="fact", title="Nulls", content="Null is null.")
    assert "memory:" in (harness_dir / "harness.yaml").read_text()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_memory_cli_add_list_show_forget_round_trip(harness_dir: Path):
    directory = str(harness_dir)

    result = cli_runner.invoke(
        app,
        [
            "memory", "add", directory,
            "--kind", "rule",
            "--title", "Dates in ISO 8601",
            "--content", "Emit YYYY-MM-DD.",
            "--evidence", "4 runs failed date_format",
            "--json",
        ],
    )
    assert result.exit_code == ExitCode.OK
    payload = _json(result)
    assert payload["ref"] == "dates-in-iso-8601"
    assert payload["entry"]["kind"] == "rule"
    assert payload["entry"]["source"] == "operator"

    result = cli_runner.invoke(app, ["memory", "list", directory, "--json"])
    assert result.exit_code == ExitCode.OK
    listing = _json(result)
    assert listing["count"] == 1
    assert listing["enabled"] is True
    assert listing["max_entries"] == 24
    assert 0 < listing["rendered_chars"] <= listing["prompt_budget_chars"]
    assert [e["id"] for e in listing["entries"]] == ["dates-in-iso-8601"]

    result = cli_runner.invoke(
        app, ["memory", "show", directory, "dates-in-iso-8601", "--json"]
    )
    assert result.exit_code == ExitCode.OK
    assert _json(result)["evidence"] == "4 runs failed date_format"

    result = cli_runner.invoke(
        app, ["memory", "forget", directory, "dates-in-iso-8601", "--json"]
    )
    assert result.exit_code == ExitCode.OK
    assert _json(result)["count"] == 0
    assert load_spec(harness_dir).memory.entries == []


def test_memory_cli_show_unknown_id_is_a_spec_error(harness_dir: Path):
    result = cli_runner.invoke(app, ["memory", "show", str(harness_dir), "nope", "--json"])
    assert result.exit_code == ExitCode.SPEC_ERROR
    assert _json(result)["ok"] is False


def test_memory_cli_add_over_budget_exits_3_and_writes_nothing(harness_dir: Path):
    directory = str(harness_dir)
    cli_runner.invoke(app, ["set", "memory.prompt_budget_chars", "200", "--dir", directory])
    before = (harness_dir / "harness.yaml").read_text()

    result = cli_runner.invoke(
        app,
        ["memory", "add", directory, "--kind", "fact", "--title", "Long",
         "--content", "x" * 400, "--json"],
    )

    assert result.exit_code == ExitCode.SPEC_ERROR
    assert "prompt_budget_chars" in _json(result)["error"]
    assert (harness_dir / "harness.yaml").read_text() == before


def test_operator_may_still_set_a_frozen_budget(harness_dir: Path):
    """Frozen means frozen from *evolution*: `set` is the sanctioned local path."""
    result = cli_runner.invoke(
        app, ["set", "memory.max_entries", "10", "--dir", str(harness_dir), "--json"]
    )

    assert result.exit_code == ExitCode.OK
    assert load_spec(harness_dir).memory.max_entries == 10
