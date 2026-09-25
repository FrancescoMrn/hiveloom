"""Tests for the incremental construction API (rollback, scaffolding, events)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from hiveloom import __version__ as hiveloom_version
from hiveloom import construct
from hiveloom.errors import HiveloomError, SpecError
from hiveloom.spec import loader
from hiveloom.spec.loader import load_spec, validate_harness


def test_init_creates_valid_skeleton(tmp_path: Path):
    directory = tmp_path / "h"
    spec = construct.init_harness(directory, name="my-h", task="Task.")
    assert spec.name == "my-h"
    assert (directory / "harness.yaml").exists()
    assert (directory / ".env.example").exists()
    assert (directory / "README.md").exists()
    assert (directory / ".gitignore").exists()
    # Freshly initialized harness is fully valid.
    validate_harness(directory)


def test_init_pins_the_runtime_in_pyproject(tmp_path: Path):
    """Dependencies are declared in PEP 621 metadata, pinned to this runtime."""
    directory = tmp_path / "h"
    construct.init_harness(directory, name="My Harness!", task='A "quoted" task.\nTwo lines.')
    data = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    # A harness name is free text; a distribution name is not.
    assert data["project"]["name"] == "my-harness"
    assert data["project"]["description"] == 'A "quoted" task. Two lines.'
    assert data["project"]["dependencies"] == [f"hiveloom=={hiveloom_version}"]
    # The folder is not a package: nothing is built, only the pins resolved.
    assert data["tool"]["uv"]["package"] is False


def test_init_refuses_to_clobber(harness_dir: Path):
    with pytest.raises(SpecError, match="already exists"):
        construct.init_harness(harness_dir, name="x", task="y")


def test_init_rolls_back_new_directory_on_failure(tmp_path: Path, monkeypatch):
    directory = tmp_path / "h"
    monkeypatch.setattr(
        construct, "_pkg_version", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        construct.init_harness(directory, name="my-h", task="Task.")

    assert not directory.exists()


def test_init_does_not_fail_when_trust_recording_fails(tmp_path: Path, monkeypatch):
    directory = tmp_path / "h"
    monkeypatch.setattr(
        construct.trust, "record_trust", lambda _path: (_ for _ in ()).throw(OSError())
    )

    construct.init_harness(directory, name="my-h", task="Task.")

    assert validate_harness(directory).name == "my-h"


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


# --------------------------------------------------------------------------- #
# set_field: list-index paths (defect 1)
# --------------------------------------------------------------------------- #
def test_set_field_indexes_into_an_existing_guardrail(harness_dir: Path):
    """`init` already injects the cost guardrail at guardrails.0 (the
    ``_ensure_cost_guardrail`` safety invariant), so this is the exact
    new-user path: `hiveloom set guardrails.0.value 0.05` on a fresh harness.

    Before the fix, a numeric segment was walked as a dict key, which
    replaced the whole `guardrails` list with `{"0": {"value": ...}}` and
    failed with "Input should be a valid list".
    """
    spec = load_spec(harness_dir)
    assert spec.guardrails[0].builtin == "max_cost_usd"

    construct.set_field(harness_dir, "guardrails.0.value", value="0.05")

    spec = load_spec(harness_dir)
    assert isinstance(spec.guardrails, list)
    assert spec.guardrails[0].value == 0.05


def test_set_field_indexes_into_an_mcp_server(harness_dir: Path):
    construct.add_mcp_server(harness_dir, name="echo", stdio_command="npx")
    construct.set_field(harness_dir, "mcp_servers.0.timeout_seconds", value="120")
    spec = load_spec(harness_dir)
    assert spec.mcp_servers[0].timeout_seconds == 120


def test_set_field_list_index_does_not_disturb_other_items(harness_dir: Path):
    construct.add_guardrail(harness_dir, builtin="max_wall_clock_seconds", value=300)
    construct.set_field(harness_dir, "guardrails.1.value", value="120")
    spec = load_spec(harness_dir)
    assert len(spec.guardrails) == 2
    assert spec.guardrails[0].builtin == "max_cost_usd"
    assert spec.guardrails[0].value == 1.0
    assert spec.guardrails[1].builtin == "max_wall_clock_seconds"
    assert spec.guardrails[1].value == 120


def test_set_field_list_index_out_of_range_names_the_length(harness_dir: Path):
    with pytest.raises(SpecError, match=r"list has 1 item"):
        construct.set_field(harness_dir, "guardrails.5.value", value="1")


def test_set_field_non_numeric_segment_into_a_list_raises(harness_dir: Path):
    with pytest.raises(SpecError, match="not a list index"):
        construct.set_field(harness_dir, "guardrails.value.foo", value="1")


def test_set_field_dotted_object_paths_still_autovivify(harness_dir: Path):
    """Existing behaviour of dotted object paths is unchanged: a missing
    intermediate mapping (here, simulating an older/hand-trimmed document
    that omits an optional section entirely) is still created on the way to
    the leaf, exactly as before this fix.
    """
    raw = loader.load_raw(harness_dir)
    del raw["context"]
    (harness_dir / "harness.yaml").write_text(yaml.dump(raw))

    construct.set_field(harness_dir, "context.max_input_tokens", value="5000")

    spec = load_spec(harness_dir)
    assert spec.context.max_input_tokens == 5000


# --------------------------------------------------------------------------- #
# set_field: string fields keep raw CLI text (defect 2)
# --------------------------------------------------------------------------- #
def test_set_field_keeps_raw_text_for_a_string_field(harness_dir: Path):
    text = 'Reply with {"words": N}: nothing else'
    construct.set_field(harness_dir, "system_prompt", value=text)
    spec = load_spec(harness_dir)
    assert spec.system_prompt == text


def test_set_field_keeps_raw_text_even_when_it_looks_like_a_yaml_mapping(harness_dir: Path):
    construct.set_field(harness_dir, "system_prompt", value="Answer: yes")
    spec = load_spec(harness_dir)
    assert spec.system_prompt == "Answer: yes"


def test_set_field_still_yaml_parses_non_string_fields(harness_dir: Path):
    """Non-string fields are unaffected: `"30"` still coerces to an int."""
    construct.set_field(harness_dir, "loop.max_turns", value="30")
    spec = load_spec(harness_dir)
    assert spec.loop.max_turns == 30
    assert isinstance(spec.loop.max_turns, int)


def test_set_field_string_coercion_failure_hints_at_file(harness_dir: Path):
    """A field this heuristic can't resolve to a plain `str` type (here,
    `tools.0.description` sits behind the ToolRef discriminated union) still
    YAML-parses the value; when that produces a validation failure, the error
    now points at `--file` instead of leaving the user to guess.
    """
    construct.add_tool(
        harness_dir, code="tools/x.py:my_tool", description="placeholder"
    )
    with pytest.raises(SpecError, match="valid string") as exc_info:
        construct.set_field(harness_dir, "tools.0.description", value="Answer: yes")
    assert "--file" in str(exc_info.value)


def test_set_model_switches_lab_in_one_commit(harness_dir: Path):
    """Provider and id must move together — the only way to change labs."""
    spec = construct.set_model(harness_dir, "openai/gpt-4.1-mini")
    assert (spec.model.provider, spec.model.id) == ("openai", "gpt-4.1-mini")
    reloaded = load_spec(harness_dir)
    assert (reloaded.model.provider, reloaded.model.id) == ("openai", "gpt-4.1-mini")


def test_set_model_keeps_slashes_in_aggregator_ids(harness_dir: Path):
    spec = construct.set_model(harness_dir, "openrouter/deepseek/deepseek-r1")
    assert spec.model.provider == "openrouter"
    assert spec.model.id == "deepseek/deepseek-r1"


def test_set_model_preserves_other_model_fields(harness_dir: Path):
    construct.set_field(harness_dir, "model.max_tokens", value="1024")
    construct.set_model(harness_dir, "openai/gpt-4o-mini")
    assert load_spec(harness_dir).model.max_tokens == 1024


@pytest.mark.parametrize("selector", ["gpt-4.1-mini", "/gpt-4.1-mini", "openai/"])
def test_set_model_requires_a_full_selector(harness_dir: Path, selector: str):
    with pytest.raises(SpecError, match="provider/model-id"):
        construct.set_model(harness_dir, selector)


def test_set_model_rolls_back_an_invalid_pair(harness_dir: Path):
    before = (harness_dir / "harness.yaml").read_text()
    with pytest.raises(SpecError):
        construct.set_model(harness_dir, "claude/claude-hiaku-4-5")  # typo
    assert (harness_dir / "harness.yaml").read_text() == before


def test_setting_provider_and_id_separately_still_fails(harness_dir: Path):
    """The reason set_model exists: neither single-field order can validate."""
    with pytest.raises(SpecError):
        construct.set_field(harness_dir, "model.provider", value="openai")
    with pytest.raises(SpecError):
        construct.set_field(harness_dir, "model.id", value="gpt-4.1-mini")


def test_add_builtin_tool(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="file_read")
    spec = load_spec(harness_dir)
    assert any(getattr(t, "builtin", None) == "file_read" for t in spec.tools)


def test_add_builtin_tool_takes_catalog_params(harness_dir: Path):
    """A shell allowlist has to be declarable without hand-editing the YAML."""
    construct.add_tool(harness_dir, builtin="shell", commands=["grep -c ERROR app.log"])
    spec = load_spec(harness_dir)
    shell = next(t for t in spec.tools if getattr(t, "builtin", None) == "shell")
    assert shell.params() == {"commands": ["grep -c ERROR app.log"]}


def test_add_builtin_tool_rejects_unknown_param(harness_dir: Path):
    before = (harness_dir / "harness.yaml").read_text()
    with pytest.raises(SpecError, match="has no parameter 'allowlist'"):
        construct.add_tool(harness_dir, builtin="shell", allowlist=["ls"])
    assert (harness_dir / "harness.yaml").read_text() == before


def test_add_builtin_tool_rejects_unbuildable_shell_rule(harness_dir: Path):
    """The rule the ShellTool would refuse must not reach a valid spec."""
    before = (harness_dir / "harness.yaml").read_text()
    with pytest.raises(SpecError, match="cannot allow arbitrary extra arguments"):
        construct.add_tool(
            harness_dir,
            builtin="shell",
            commands=[{"argv": ["cat"], "allow_extra_args": True}],
        )
    assert (harness_dir / "harness.yaml").read_text() == before


def test_params_need_a_builtin(harness_dir: Path):
    with pytest.raises(SpecError, match="only be declared for a builtin"):
        construct.add_tool(
            harness_dir, code="tools/x.py:go", description="Go.", commands=["ls"]
        )


def test_add_code_tool_scaffolds_stub(harness_dir: Path):
    construct.add_tool(
        harness_dir, code="tools/fetch.py:fetch", description="Fetch a thing."
    )
    stub = harness_dir / "tools" / "fetch.py"
    assert stub.exists()
    assert "def fetch(" in stub.read_text()
    validate_harness(harness_dir)


def test_add_code_tool_rolls_back_unexpected_hook_error(harness_dir: Path, monkeypatch):
    monkeypatch.setattr(
        construct, "resolve_hooks", lambda *_args: (_ for _ in ()).throw(OSError("boom"))
    )

    with pytest.raises(HiveloomError, match="could not add_tool"):
        construct.add_tool(harness_dir, code="tools/fetch.py:fetch", description="Fetch a thing.")

    assert not (harness_dir / "tools" / "fetch.py").exists()


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


def _guardrails(harness_dir: Path, builtin: str) -> list:
    return [g for g in load_spec(harness_dir).guardrails if getattr(g, "builtin", None) == builtin]


def test_add_singleton_guardrail_replaces_injected_default(harness_dir: Path):
    """The spec's default max_cost_usd (1.00) must not linger beside an explicit one."""
    assert [g.value for g in _guardrails(harness_dir, "max_cost_usd")] == [1.00]

    construct.add_guardrail(harness_dir, builtin="max_cost_usd", value=0.25)

    assert [g.value for g in _guardrails(harness_dir, "max_cost_usd")] == [0.25]


def test_add_singleton_guardrail_is_idempotent(harness_dir: Path):
    construct.add_guardrail(harness_dir, builtin="tool_allowlist")
    construct.add_guardrail(harness_dir, builtin="tool_allowlist")
    construct.add_guardrail(harness_dir, builtin="max_wall_clock_seconds", value=60)
    construct.add_guardrail(harness_dir, builtin="max_wall_clock_seconds", value=120)

    assert len(_guardrails(harness_dir, "tool_allowlist")) == 1
    assert [g.value for g in _guardrails(harness_dir, "max_wall_clock_seconds")] == [120]


def test_add_singleton_guardrail_keeps_position(harness_dir: Path):
    """Replacing must overwrite in place, not reorder the guardrail list."""
    construct.add_guardrail(harness_dir, builtin="tool_allowlist")
    before = [getattr(g, "builtin", None) for g in load_spec(harness_dir).guardrails]

    construct.add_guardrail(harness_dir, builtin="max_cost_usd", value=0.10)

    assert [getattr(g, "builtin", None) for g in load_spec(harness_dir).guardrails] == before


def test_add_singleton_guardrail_collapses_preexisting_duplicates(harness_dir: Path):
    """A spec written before replace semantics may already carry duplicates."""
    raw = loader.load_raw(harness_dir)
    raw["guardrails"] = [
        {"builtin": "max_cost_usd", "value": 1.00},
        {"builtin": "tool_allowlist"},
        {"builtin": "max_cost_usd", "value": 0.25},
    ]
    (harness_dir / "harness.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    construct.add_guardrail(harness_dir, builtin="max_cost_usd", value=0.10)

    assert [g.value for g in _guardrails(harness_dir, "max_cost_usd")] == [0.10]
    assert len(_guardrails(harness_dir, "tool_allowlist")) == 1


def test_add_composing_guardrail_keeps_distinct_patterns(harness_dir: Path):
    """regex_output_filter is a list of filters — distinct patterns must all survive."""
    construct.add_guardrail(harness_dir, builtin="regex_output_filter", pattern="sk-ant-")
    construct.add_guardrail(harness_dir, builtin="regex_output_filter", pattern="BEGIN PRIVATE KEY")

    patterns = [g.params()["pattern"] for g in _guardrails(harness_dir, "regex_output_filter")]
    assert patterns == ["sk-ant-", "BEGIN PRIVATE KEY"]


def test_add_composing_guardrail_collapses_exact_duplicate(harness_dir: Path):
    construct.add_guardrail(harness_dir, builtin="regex_output_filter", pattern="sk-ant-")
    construct.add_guardrail(harness_dir, builtin="regex_output_filter", pattern="sk-ant-")

    assert len(_guardrails(harness_dir, "regex_output_filter")) == 1


def test_find_guardrails_reports_raw_entries(harness_dir: Path):
    construct.add_guardrail(harness_dir, builtin="max_cost_usd", value=0.25)

    assert construct.find_guardrails(harness_dir, "max_cost_usd") == [
        {"builtin": "max_cost_usd", "value": 0.25}
    ]
    assert construct.find_guardrails(harness_dir, "no_network_write") == []


def test_remove_tool_by_name(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="file_read")
    construct.remove_item(harness_dir, "file_read")
    spec = load_spec(harness_dir)
    assert not any(getattr(t, "builtin", None) == "file_read" for t in spec.tools)


def test_remove_nonexistent_raises(harness_dir: Path):
    with pytest.raises(SpecError, match="nothing named"):
        construct.remove_item(harness_dir, "not-a-thing")


def test_remove_list_index_drops_one_item(harness_dir: Path):
    """`remove` shares the same dotted/indexed path walker as `set`."""
    construct.add_guardrail(harness_dir, builtin="max_wall_clock_seconds", value=300)
    spec = load_spec(harness_dir)
    assert len(spec.guardrails) == 2

    construct.remove_item(harness_dir, "guardrails.1")

    spec = load_spec(harness_dir)
    assert len(spec.guardrails) == 1
    assert spec.guardrails[0].builtin == "max_cost_usd"


def test_remove_list_index_out_of_range_raises(harness_dir: Path):
    with pytest.raises(SpecError, match=r"list has 1 item"):
        construct.remove_item(harness_dir, "guardrails.5")


# --------------------------------------------------------------------------- #
# add_mcp_server
# --------------------------------------------------------------------------- #
def test_add_mcp_server_stdio(harness_dir: Path):
    construct.add_mcp_server(
        harness_dir,
        name="echo",
        stdio_command="npx",
        stdio_args=["-y", "@foo/mcp"],
        stdio_env={"FOO": "bar"},
        stdio_env_from_host={"TOKEN": "HOST_TOKEN"},
        stdio_cwd="sub",
    )
    spec = load_spec(harness_dir)
    assert len(spec.mcp_servers) == 1
    server = spec.mcp_servers[0]
    assert server.name == "echo"
    assert server.transport == "stdio"
    assert server.command == "npx"
    assert server.args == ["-y", "@foo/mcp"]
    assert server.env == {"FOO": "bar"}
    assert server.env_from_host_env == {"TOKEN": "HOST_TOKEN"}
    assert server.cwd == "sub"


def test_add_mcp_server_http(harness_dir: Path):
    construct.add_mcp_server(
        harness_dir,
        name="jira",
        url="https://mcp.acme.com/mcp",
        headers={"X-Foo": "bar"},
        header_env={"Authorization": "TOKEN_VAR"},
        tools=["search_issues"],
        deferred=True,
    )
    spec = load_spec(harness_dir)
    server = spec.mcp_servers[0]
    assert server.transport == "http"
    assert server.url == "https://mcp.acme.com/mcp"
    assert server.headers == {"X-Foo": "bar"}
    assert server.header_env == {"Authorization": "TOKEN_VAR"}
    assert server.tools == ["search_issues"]
    assert server.deferred is True


def test_add_mcp_server_requires_exactly_one_of_command_or_url(harness_dir: Path):
    with pytest.raises(SpecError, match="--stdio-command"):
        construct.add_mcp_server(harness_dir, name="x")
    with pytest.raises(SpecError, match="--stdio-command"):
        construct.add_mcp_server(
            harness_dir, name="x", stdio_command="npx", url="https://x.invalid"
        )


def test_add_mcp_server_omits_empty_optionals(harness_dir: Path):
    construct.add_mcp_server(harness_dir, name="echo", stdio_command="npx")
    raw = loader.load_raw(harness_dir)
    entry = raw["mcp_servers"][0]
    # cwd/tools default to None on the model, so exclude_none drops them.
    assert "cwd" not in entry
    assert "tools" not in entry
    # env/env_from_host_env/deferred default to {}/{}/False (not None) on
    # Task 5's landed schema, so _commit's full-spec re-dump always writes
    # them explicitly regardless of what add_mcp_server itself omits here --
    # a partial divergence from the brief's "omit empty optional keys" ask,
    # documented in task-6-report.md.
    assert entry["env"] == {}
    assert entry["env_from_host_env"] == {}
    assert entry["deferred"] is False


def test_add_mcp_server_timeout_seconds(harness_dir: Path):
    """Defect 3: the default 30s timeout is too short for a peer harness run;
    `add mcp-server` needs a way to raise it."""
    construct.add_mcp_server(
        harness_dir, name="echo", stdio_command="npx", timeout_seconds=120.0
    )
    spec = load_spec(harness_dir)
    assert spec.mcp_servers[0].timeout_seconds == 120.0


def test_add_mcp_server_timeout_seconds_defaults_when_omitted(harness_dir: Path):
    construct.add_mcp_server(harness_dir, name="echo", stdio_command="npx")
    spec = load_spec(harness_dir)
    assert spec.mcp_servers[0].timeout_seconds == 30.0


def test_add_mcp_server_timeout_seconds_validated_through_schema(harness_dir: Path):
    """gt=0, le=600 on McpStdioServerRef/McpHttpServerRef -- not re-validated
    ad hoc in construct.add_mcp_server, just passed through to the schema."""
    with pytest.raises(SpecError):
        construct.add_mcp_server(
            harness_dir, name="echo", stdio_command="npx", timeout_seconds=700.0
        )
    with pytest.raises(SpecError):
        construct.add_mcp_server(
            harness_dir, name="echo", stdio_command="npx", timeout_seconds=0.0
        )


def test_add_mcp_server_round_trips_through_remove(harness_dir: Path):
    construct.add_mcp_server(harness_dir, name="echo", stdio_command="npx")
    spec = load_spec(harness_dir)
    assert any(s.name == "echo" for s in spec.mcp_servers)

    construct.remove_item(harness_dir, "echo")
    spec = load_spec(harness_dir)
    assert not spec.mcp_servers


def test_add_mcp_server_rolls_back_malformed_entry(harness_dir: Path):
    before = (harness_dir / "harness.yaml").read_text()
    with pytest.raises(SpecError, match="a-zA-Z0-9_-"):
        construct.add_mcp_server(harness_dir, name="bad name!", stdio_command="npx")
    assert (harness_dir / "harness.yaml").read_text() == before


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


def test_add_skill_quotes_a_description_that_is_not_plain_yaml(harness_dir):
    """A description with ": " once broke the scaffolded frontmatter and the command."""
    from hiveloom.skills import load_skills

    description = "House style: title length, key points: 3-5, and 'numbers' verbatim."
    spec = construct.add_skill(harness_dir, "house-style", description)
    assert spec.skills == ["house-style"]
    [skill] = load_skills(spec, harness_dir)
    assert skill.description == description
