"""Research-safe execution: classification, resolution, deny and replay at run time."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.research.charter import Execution
from hiveloom.research.execution import ReplayStore, ResearchToolPolicy, classify, resolve
from hiveloom.spec.loader import load_spec


def _events(trace_path: str) -> list[dict]:
    return [json.loads(line) for line in Path(trace_path).read_text().splitlines()]


def test_read_only_tools_are_allowed_and_file_writes_sandboxed_by_default(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="file_read")
    construct.add_tool(harness_dir, builtin="file_write")
    policy = resolve(load_spec(harness_dir), Execution())
    assert policy.errors == []
    assert policy.tools == {"file_read": "allow", "file_write": "sandbox"}


def test_effectful_tools_must_be_classified_before_a_program_starts(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="http_get", hosts=["example.com"])
    spec = load_spec(harness_dir)
    assert [c.default for c in classify(spec) if c.name == "http_get"] == [None]
    unclassified = resolve(spec, Execution())
    assert len(unclassified.errors) == 1 and "http_get" in unclassified.errors[0]
    declared = resolve(spec, Execution(tools={"http_get": "replay"}))
    assert declared.errors == [] and declared.tools["http_get"] == "replay"


def test_sandbox_is_refused_where_effects_could_leave_the_copy(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="http_get", hosts=["example.com"])
    policy = resolve(load_spec(harness_dir), Execution(tools={"http_get": "sandbox"}))
    assert any("sandbox applies to" in error for error in policy.errors)


def test_a_policy_naming_an_unknown_tool_is_an_error(harness_dir: Path):
    policy = resolve(load_spec(harness_dir), Execution(tools={"nope": "deny"}))
    assert any("no such tool" in error for error in policy.errors)


def test_a_denied_tool_is_absent_from_the_run(harness_dir: Path):
    construct.add_tool(harness_dir, builtin="file_read")
    policy = resolve(load_spec(harness_dir), Execution(tools={"file_read": "deny"}))
    result = runner.run_harness(
        harness_dir, "read notes.txt",
        provider=FakeModelProvider([
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response("done"),
        ]),
        tool_policy=ResearchToolPolicy(policy),
    )
    [tool_result] = [e for e in _events(result.trace_path) if e["type"] == "tool_result"]
    assert tool_result["payload"]["is_error"]
    assert "unknown tool" in tool_result["payload"]["content"]


def test_a_replayed_tool_serves_recordings_and_never_runs_live(harness_dir: Path, tmp_path):
    construct.add_tool(harness_dir, builtin="file_read")
    (harness_dir / "notes.txt").write_text("the real content")
    live = runner.run_harness(
        harness_dir, "read notes.txt",
        provider=FakeModelProvider([
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response("done"),
        ]),
    )
    store = ReplayStore(tmp_path / "replay.jsonl")
    assert store.build([live.trace_path], {"file_read"}) == 1
    (harness_dir / "notes.txt").write_text("CHANGED ON DISK")

    policy = resolve(load_spec(harness_dir), Execution(tools={"file_read": "replay"}))
    replayed = runner.run_harness(
        harness_dir, "read notes.txt",
        provider=FakeModelProvider([
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            tool_response("file_read", {"path": "other.txt"}, call_id="c2"),
            text_response("done"),
        ]),
        tool_policy=ResearchToolPolicy(policy, store),
    )
    results = [e["payload"] for e in _events(replayed.trace_path) if e["type"] == "tool_result"]
    assert "the real content" in results[0]["content"] and not results[0]["is_error"]
    assert results[1]["is_error"] and "no recorded result" in results[1]["content"]
