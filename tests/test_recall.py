"""Opt-in model retrieval of this harness's own prior runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.tools.builtin import RecallRunsTool
from hiveloom.tools.registry import ToolError


def _harness(tmp_path: Path, name: str = "recall-harness", **params: str) -> Path:
    directory = tmp_path / name
    construct.init_harness(directory, name=name, task="Summarize the input.")
    construct.set_field(directory, "loop.require_verification", "false")
    construct.add_tool(directory, builtin="recall_runs")
    for path, value in params.items():
        construct.set_field(directory, f"tools.0.{path}", value)
    return directory


def _harness_key(harness: Path) -> str:
    """What the Hive keys a harness's evidence on: its stable id."""
    from hiveloom.spec.loader import harness_path, load_spec

    return load_spec(harness_path(harness)).id


def _ingested_run(harness: Path, text: str, output: str) -> str:
    result = runner.run_harness(
        harness,
        text,
        provider=FakeModelProvider([text_response(output)]),
        literal_input=True,
    )
    return result.run_id


def _recall_text(provider: FakeModelProvider, call_index: int = 1) -> str:
    blocks = provider.calls[call_index]["messages"][-1]["content"]
    return "".join(b["content"] for b in blocks if b["type"] == "tool_result")


# --------------------------------------------------------------------------- #
# The query
# --------------------------------------------------------------------------- #
def test_recall_returns_prior_successes_newest_first(tmp_path: Path):
    harness = _harness(tmp_path)
    _ingested_run(harness, "first task", "FIRST ANSWER")
    second = _ingested_run(harness, "second task", "SECOND ANSWER")

    with Hive() as hive:
        runs = hive.recall(_harness_key(harness), limit=5)

    assert [r["output"] for r in runs] == ["SECOND ANSWER", "FIRST ANSWER"]
    assert runs[0]["run_id"] == second


def test_recall_excludes_the_asking_run(tmp_path: Path):
    harness = _harness(tmp_path)
    only = _ingested_run(harness, "task", "ANSWER")

    with Hive() as hive:
        assert hive.recall(_harness_key(harness), exclude_run_id=only) == []


def test_recall_carries_the_feedback_that_rejected_a_failure(tmp_path: Path):
    harness = tmp_path / "strict"
    construct.init_harness(harness, name="strict", task="Emit JSON.")
    construct.add_validator(harness, builtin="regex_match", pattern=r"^\{.*\}$")
    construct.add_tool(harness, builtin="recall_runs")
    runner.run_harness(
        harness,
        "task",
        provider=FakeModelProvider([text_response("not json at all")] * 4),
        literal_input=True,
    )

    with Hive() as hive:
        failures = hive.recall(_harness_key(harness), status="failed")

    assert failures, "the failed run was recalled"
    assert failures[0]["status"] == "verify_failed"
    assert any(v["verifier"] == "regex_match" for v in failures[0]["failed_verifications"])


def test_recall_never_crosses_harnesses(tmp_path: Path):
    mine = _harness(tmp_path, "mine")
    theirs = _harness(tmp_path, "theirs")
    _ingested_run(mine, "my task", "MY ANSWER")
    _ingested_run(theirs, "their task", "THEIR ANSWER")

    with Hive() as hive:
        recalled = hive.recall(_harness_key(mine), limit=10)

    assert [r["output"] for r in recalled] == ["MY ANSWER"]


def test_recall_can_be_scoped_to_the_running_version(tmp_path: Path):
    harness = _harness(tmp_path)
    _ingested_run(harness, "old version task", "OLD ANSWER")
    construct.set_field(harness, "loop.max_turns", "9")  # a new version hash
    _ingested_run(harness, "new version task", "NEW ANSWER")

    with Hive() as hive:
        rows = hive.recall(_harness_key(harness), limit=10)
        versions = {r["harness_version_hash"] for r in rows}
        newest = rows[0]["harness_version_hash"]
        scoped = hive.recall(_harness_key(harness), version=newest, limit=10)

    assert len(versions) == 2
    assert [r["output"] for r in scoped] == ["NEW ANSWER"]


def test_recall_matches_a_query_against_task_and_output(tmp_path: Path):
    harness = _harness(tmp_path)
    _ingested_run(harness, "invoice for acme", "TOTAL 42")
    _ingested_run(harness, "invoice for globex", "TOTAL 7")

    with Hive() as hive:
        by_task = hive.recall(_harness_key(harness), query="globex", limit=10)
        by_output = hive.recall(_harness_key(harness), query="total 42", limit=10)

    assert [r["output"] for r in by_task] == ["TOTAL 7"]
    assert [r["task"] for r in by_output] == ["invoice for acme"]


def test_an_unknown_status_filter_is_refused(tmp_path: Path):
    with Hive() as hive, pytest.raises(ValueError, match="unknown status filter"):
        hive.recall("k", status="pending")


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def test_the_tool_scopes_to_the_harness_in_the_run_context(tmp_path: Path):
    mine = _harness(tmp_path, "mine")
    theirs = _harness(tmp_path, "theirs")
    _ingested_run(mine, "my task", "MY ANSWER")
    _ingested_run(theirs, "their task", "THEIR ANSWER")

    tool = RecallRunsTool()
    rendered = tool.run(
        run_context={"harness_id": _harness_key(mine), "harness_name": "mine"}
    )

    assert "MY ANSWER" in rendered
    assert "THEIR ANSWER" not in rendered
    # There is no parameter through which a model could ask for another harness.
    assert set(tool.input_schema["properties"]) == {"status", "query", "limit"}


def test_the_tool_says_so_when_there_is_no_history(tmp_path: Path):
    harness = _harness(tmp_path)
    rendered = RecallRunsTool().run(
        run_context={"harness_id": _harness_key(harness), "harness_name": "recall-harness"}
    )
    assert "no earlier success runs" in rendered


def test_include_output_false_withholds_past_outputs(tmp_path: Path):
    harness = _harness(tmp_path)
    _ingested_run(harness, "task", "SENSITIVE ANSWER")
    context = {"harness_id": _harness_key(harness), "harness_name": "recall-harness"}

    assert "SENSITIVE ANSWER" in RecallRunsTool().run(run_context=context)
    assert "SENSITIVE ANSWER" not in RecallRunsTool(include_output=False).run(
        run_context=context
    )


def test_the_tool_caps_the_limit_the_model_asks_for(tmp_path: Path):
    harness = _harness(tmp_path)
    for i in range(4):
        _ingested_run(harness, f"task {i}", f"ANSWER {i}")
    context = {"harness_id": _harness_key(harness), "harness_name": "recall-harness"}

    rendered = RecallRunsTool(limit=2).run(limit=99, run_context=context)
    assert rendered.count("task: ") == 2


def test_the_tool_refuses_a_call_it_cannot_scope():
    with pytest.raises(ToolError, match="no harness identity"):
        RecallRunsTool().run(run_context={})


def test_an_invalid_scope_is_refused_at_build_time():
    with pytest.raises(ToolError, match="scope must be"):
        RecallRunsTool(scope="everything")


# --------------------------------------------------------------------------- #
# In a run
# --------------------------------------------------------------------------- #
def test_a_run_can_read_the_harnesss_own_earlier_run(tmp_path: Path):
    harness = _harness(tmp_path)
    _ingested_run(harness, "summarize the Q1 report", "Q1 GREW 12%")

    provider = FakeModelProvider(
        [
            tool_response("recall_runs", {"status": "success"}, call_id="c1"),
            text_response("Q2 GREW 9%"),
        ]
    )
    result = runner.run_harness(
        harness, "summarize the Q2 report", provider=provider, literal_input=True
    )

    assert result.status == "success"
    recalled = _recall_text(provider)
    assert "Q1 GREW 12%" in recalled
    assert "summarize the Q1 report" in recalled


def test_the_tool_is_absent_unless_the_spec_declares_it(tmp_path: Path):
    directory = tmp_path / "plain"
    construct.init_harness(directory, name="plain", task="Do a thing.")
    construct.set_field(directory, "loop.require_verification", "false")

    provider = FakeModelProvider([text_response("done")])
    runner.run_harness(directory, "go", provider=provider, literal_input=True, ingest=False)

    assert "recall_runs" not in {t["name"] for t in provider.calls[0]["tools"]}
