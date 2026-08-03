"""Tests for the incremental construction API (rollback, scaffolding, events)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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
    assert (directory / "requirements.txt").exists()
    assert (directory / "README.md").exists()
    assert (directory / ".gitignore").exists()
    # Freshly initialized harness is fully valid.
    validate_harness(directory)


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
