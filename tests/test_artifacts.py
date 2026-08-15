"""Structured artifacts: tool side-products that reach the caller, not the model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.tools.registry import Artifact, ToolResult

CHART_TOOL = '''
from hiveloom.tools import Artifact, ToolResult, tool


@tool(description="Render a chart for the caller's UI.")
def render_chart(title: str) -> ToolResult:
    return ToolResult(
        content=f"Chart {title!r} registered - comment on it, do not repeat the numbers.",
        artifacts=[Artifact(kind="chart", data={"title": title, "values": [1, 2, 3]})],
    )
'''

PLAIN_TOOL = '''
from hiveloom.tools import tool


@tool(description="A tool with no artifacts.")
def plain(value: str) -> str:
    return f"got {value}"
'''


def _harness_with(tmp_path: Path, filename: str, source: str, func: str) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="artifact-harness", task="Chart things.")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / filename).write_text(source)
    construct.add_tool(
        directory, code=f"tools/{filename}:{func}", description=f"The {func} tool."
    )
    return directory


# --------------------------------------------------------------------------- #
# The model/caller split
# --------------------------------------------------------------------------- #
def test_artifacts_reach_the_result_while_the_model_reads_only_content(tmp_path: Path):
    harness = _harness_with(tmp_path, "chart.py", CHART_TOOL, "render_chart")
    provider = FakeModelProvider(
        [
            tool_response("render_chart", {"title": "AUM per segmento"}, call_id="c1"),
            text_response("Ecco la distribuzione."),
        ]
    )
    result = runner.run_harness(harness, "chart it", provider=provider, literal_input=True)

    assert result.status == "success"
    assert result.artifacts == [
        {
            "kind": "chart",
            "data": {"title": "AUM per segmento", "values": [1, 2, 3]},
            "tool": "render_chart",
        }
    ]
    # The model saw the prose result, never the structured payload.
    tool_result_message = provider.calls[1]["messages"][-1]
    rendered = json.dumps(tool_result_message["content"])
    assert "registered" in rendered
    assert "values" not in rendered


def test_artifacts_of_filters_by_kind(tmp_path: Path):
    harness = _harness_with(tmp_path, "chart.py", CHART_TOOL, "render_chart")
    provider = FakeModelProvider(
        [
            tool_response("render_chart", {"title": "uno"}, call_id="c1"),
            tool_response("render_chart", {"title": "due"}, call_id="c2"),
            text_response("fatto"),
        ]
    )
    result = runner.run_harness(harness, "chart it", provider=provider, literal_input=True)

    charts = result.artifacts_of("chart")
    assert [c["title"] for c in charts] == ["uno", "due"]
    assert result.artifacts_of("proposal") == []


def test_tools_without_artifacts_produce_none(tmp_path: Path):
    harness = _harness_with(tmp_path, "plain.py", PLAIN_TOOL, "plain")
    provider = FakeModelProvider(
        [tool_response("plain", {"value": "x"}, call_id="c1"), text_response("ok")]
    )
    result = runner.run_harness(harness, "go", provider=provider, literal_input=True)
    assert result.artifacts == []


# --------------------------------------------------------------------------- #
# Trace + failure paths
# --------------------------------------------------------------------------- #
def test_trace_records_artifacts_on_the_tool_result_event(tmp_path: Path):
    harness = _harness_with(tmp_path, "chart.py", CHART_TOOL, "render_chart")
    provider = FakeModelProvider(
        [tool_response("render_chart", {"title": "T"}, call_id="c1"), text_response("ok")]
    )
    result = runner.run_harness(harness, "chart it", provider=provider, literal_input=True)

    event = next(
        json.loads(line)
        for line in Path(result.trace_path).read_text().splitlines()
        if json.loads(line)["type"] == "tool_result"
    )
    assert event["payload"]["artifacts"][0]["kind"] == "chart"


def test_artifacts_survive_a_run_that_does_not_succeed(tmp_path: Path):
    """A turn that produced something before stalling still hands it back."""
    harness = _harness_with(tmp_path, "chart.py", CHART_TOOL, "render_chart")
    construct.set_value(harness, "loop.max_turns", 2)
    provider = FakeModelProvider(
        [
            tool_response("render_chart", {"title": "T"}, call_id="c1"),
            tool_response("render_chart", {"title": "U"}, call_id="c2"),
        ]
    )
    result = runner.run_harness(harness, "chart it", provider=provider, literal_input=True)

    assert result.status == "max_turns"
    assert [a["data"]["title"] for a in result.artifacts] == ["T", "U"]


def test_run_result_payload_carries_artifacts(tmp_path: Path):
    harness = _harness_with(tmp_path, "chart.py", CHART_TOOL, "render_chart")
    provider = FakeModelProvider(
        [tool_response("render_chart", {"title": "T"}, call_id="c1"), text_response("ok")]
    )
    result = runner.run_harness(harness, "chart it", provider=provider, literal_input=True)
    assert runner.run_result_payload(result)["artifacts"] == result.artifacts


# --------------------------------------------------------------------------- #
# Guardrail visibility + validation
# --------------------------------------------------------------------------- #
def test_guardrails_can_see_collected_artifacts(tmp_path: Path):
    from hiveloom.guardrails.base import RunState

    state = RunState()
    state.artifacts.append({"kind": "proposal", "data": {"n": 2}, "tool": "propose"})
    state.artifacts.append({"kind": "chart", "data": {"title": "T"}, "tool": "chart"})
    assert state.artifacts_of("proposal") == [{"n": 2}]


def test_artifact_kind_must_not_be_empty():
    with pytest.raises(ValidationError, match="must not be empty"):
        Artifact(kind="   ", data={})


def test_tool_result_defaults_to_no_artifacts():
    assert ToolResult(content="x").artifacts == []
