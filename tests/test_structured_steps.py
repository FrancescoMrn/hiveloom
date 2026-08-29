"""Declarative tool and call enforcement for structured sequential steps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.errors import SpecError
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import HarnessSpec, LoopConfig


def _events(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _harness(tmp_path: Path) -> Path:
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="structured-steps", task="Run three phases.")
    construct.add_tool(harness, builtin="file_read")
    construct.add_tool(harness, builtin="file_write")
    construct.set_value(harness, "loop.require_verification", False)
    (harness / "deal.txt").write_text("synthetic deal", encoding="utf-8")
    return harness


def _three_steps() -> list[dict]:
    return [
        {
            "id": "read",
            "instruction": "Read the deal.",
            "tools": ["file_read"],
            "require_tool_calls": ["file_read"],
            "max_model_calls": 2,
            "max_tool_calls": 1,
        },
        {
            "id": "search",
            "instruction": "Search and verify candidates.",
            "tools": ["file_write"],
            "require_tool_calls": ["file_write"],
            "max_model_calls": 2,
            "max_tool_calls": 1,
        },
        {
            "id": "answer",
            "instruction": "Produce the final answer.",
            "tools": [],
            "max_model_calls": 1,
        },
    ]


def _configure(harness: Path, steps: list[dict]) -> None:
    construct.set_value(harness, "loop.steps", steps)
    construct.set_value(harness, "loop.policy", "sequential_steps")


def test_three_phase_workflow_enforces_tools_and_indexes_step_receipts(tmp_path: Path):
    harness = _harness(tmp_path)
    _configure(harness, _three_steps())
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "deal.txt"}, call_id="read-1"),
            tool_response(
                "file_write",
                {"path": "verified.txt", "content": "candidate-1"},
                call_id="search-1",
            ),
            text_response('{"selected": ["candidate-1"]}'),
        ]
    )

    result = runner.run_harness(harness, "match", provider=provider)

    assert result.status == "success"
    assert result.turns == 3
    assert (harness / "verified.txt").read_text(encoding="utf-8") == "candidate-1"
    assert [[tool["name"] for tool in call["tools"]] for call in provider.calls] == [
        ["file_read"],
        ["file_write"],
        [],
    ]
    assert [step.status for step in result.steps] == ["completed"] * 3
    assert [step.model_calls for step in result.steps] == [1, 1, 1]
    assert [step.tool_calls for step in result.steps] == [1, 1, 0]
    assert [step["id"] for step in runner.run_result_payload(result)["steps"]] == [
        "read",
        "search",
        "answer",
    ]

    events = _events(result.trace_path)
    assert [event["type"] for event in events].count("step_started") == 3
    assert [event["type"] for event in events].count("step_completed") == 3
    with Hive() as hive:
        indexed = hive.get_run(result.run_id)
    assert indexed is not None
    assert [step["id"] for step in indexed["steps"]] == ["read", "search", "answer"]
    assert indexed["steps"][1]["completed_required_tool_calls"] == ["file_write"]


def test_hidden_tool_call_is_blocked_before_dispatch(tmp_path: Path):
    harness = _harness(tmp_path)
    steps = [_three_steps()[0], _three_steps()[2]]
    _configure(harness, steps)
    provider = FakeModelProvider(
        [
            tool_response(
                "file_write",
                {"path": "forbidden.txt", "content": "must not exist"},
                call_id="hidden",
            ),
            tool_response("file_read", {"path": "deal.txt"}, call_id="allowed"),
            text_response("done"),
        ]
    )

    result = runner.run_harness(harness, "match", provider=provider)

    assert result.status == "success"
    assert not (harness / "forbidden.txt").exists()
    hidden = [
        event
        for event in _events(result.trace_path)
        if event["type"] == "step_violation"
    ]
    assert hidden[0]["payload"]["kind"] == "hidden_tool"
    assert result.steps[0].tool_calls == 1


def test_missing_required_call_exhausts_model_limit_without_an_extra_provider_call(
    tmp_path: Path,
):
    harness = _harness(tmp_path)
    step = _three_steps()[0]
    step["max_model_calls"] = 1
    _configure(harness, [step, _three_steps()[2]])
    provider = FakeModelProvider([text_response("I skipped the tool")])

    result = runner.run_harness(harness, "match", provider=provider)

    assert result.status == "step_failed"
    assert len(provider.calls) == 1
    assert result.steps[0].status == "failed"
    assert any("missing_required_tool_calls" in item for item in result.steps[0].violations)
    assert any("model_call_limit" in item for item in result.steps[0].violations)


def test_tool_call_limit_stops_before_the_excess_call_executes(tmp_path: Path):
    harness = _harness(tmp_path)
    step = {
        "id": "write-once",
        "instruction": "Write at most once.",
        "tools": ["file_write"],
        "max_tool_calls": 1,
    }
    _configure(harness, [step])
    provider = FakeModelProvider(
        [
            tool_response(
                "file_write", {"path": "one.txt", "content": "one"}, call_id="one"
            ),
            tool_response(
                "file_write", {"path": "two.txt", "content": "two"}, call_id="two"
            ),
        ]
    )

    result = runner.run_harness(harness, "write", provider=provider)

    assert result.status == "step_failed"
    assert (harness / "one.txt").exists()
    assert not (harness / "two.txt").exists()
    assert result.steps[0].tool_calls == 1
    assert any("tool_call_limit" in item for item in result.steps[0].violations)


def test_verification_retry_does_not_complete_the_final_step_twice(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_validator(harness, builtin="regex_match", pattern="^good$")
    construct.set_value(harness, "loop.require_verification", True)
    _configure(
        harness,
        [
            {
                "id": "answer",
                "instruction": "Answer and revise if verification fails.",
                "tools": [],
                "max_model_calls": 2,
            }
        ],
    )
    provider = FakeModelProvider([text_response("bad"), text_response("good")])

    result = runner.run_harness(harness, "answer", provider=provider)

    assert result.status == "success"
    assert result.steps[0].model_calls == 2
    assert [event["type"] for event in _events(result.trace_path)].count(
        "step_completed"
    ) == 1


def test_dry_run_explains_each_step_effective_tools_and_limits(tmp_path: Path):
    harness = _harness(tmp_path)
    _configure(harness, _three_steps())

    plan = runner.dry_run(harness, "match")

    assert [step["id"] for step in plan["steps"]] == ["read", "search", "answer"]
    assert [step["tools"] for step in plan["steps"]] == [
        ["file_read"],
        ["file_write"],
        [],
    ]
    assert plan["steps"][0]["max_tool_calls"] == 1
    assert [tool["name"] for tool in plan["tools"]] == ["file_read"]


def test_invalid_structured_step_tool_rolls_back_construct_change(tmp_path: Path):
    harness = _harness(tmp_path)
    before = (harness / "harness.yaml").read_bytes()

    with pytest.raises(SpecError, match="unknown tool"):
        construct.set_value(
            harness,
            "loop.steps",
            [{"id": "bad", "instruction": "Bad.", "tools": ["invented"]}],
        )

    assert (harness / "harness.yaml").read_bytes() == before
    assert load_spec(harness).loop.steps == []


def test_structured_step_schema_rejects_ambiguous_constraints(tmp_path: Path):
    with pytest.raises(ValueError, match="must also appear"):
        LoopConfig(
            policy="sequential_steps",
            steps=[
                {
                    "id": "bad-required",
                    "instruction": "Bad.",
                    "tools": [],
                    "require_tool_calls": ["file_read"],
                }
            ],
        )
    with pytest.raises(ValueError, match="ids must be unique"):
        LoopConfig(
            policy="sequential_steps",
            steps=[
                {"id": "same", "instruction": "One."},
                {"id": "same", "instruction": "Two."},
            ],
        )

    harness = _harness(tmp_path)
    raw = load_spec(harness).model_dump(mode="json", exclude_none=True)
    raw["tools"][0]["deferred"] = True
    raw["loop"] = {
        "policy": "sequential_steps",
        "steps": [
            {
                "id": "deferred",
                "instruction": "Read.",
                "tools": ["file_read"],
                "require_tool_calls": ["file_read"],
            }
        ],
    }
    with pytest.raises(ValueError, match="requires deferred tool"):
        HarnessSpec.model_validate(raw)
