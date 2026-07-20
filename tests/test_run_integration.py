"""End-to-end integration tests of `run` on the example harness (fake provider)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hiveloom import runner
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def _make_harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 20)
    return target


def _event_types(trace_path: str) -> list[str]:
    return [json.loads(line)["type"] for line in Path(trace_path).read_text().splitlines()]


def test_full_run_success_emits_ordered_events(tmp_path: Path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "success"
    assert result.output == _VALID_SUMMARY
    assert result.cost_usd > 0

    events = _event_types(result.trace_path)
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    assert "tool_call" in events and "tool_result" in events
    assert events.count("verification_result") == 2  # output_schema + code validator
    model_call = next(
        json.loads(line)
        for line in Path(result.trace_path).read_text().splitlines()
        if json.loads(line)["type"] == "model_call"
    )
    assert model_call["payload"]["messages"]
    assert "system" in model_call["payload"]


def test_verify_failure_triggers_retry_with_feedback(tmp_path: Path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            text_response("not valid json at all"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "success"
    # First attempt fails verification, second passes.
    verifications = [
        json.loads(line)
        for line in Path(result.trace_path).read_text().splitlines()
        if json.loads(line)["type"] == "verification_result"
    ]
    assert any(v["payload"]["passed"] is False for v in verifications)
    assert any(v["payload"]["passed"] is True for v in verifications)


def test_guardrail_halts_on_cost(tmp_path: Path):
    harness = _make_harness(tmp_path)
    # One response with huge output tokens: 100k out * $5/1M = $0.50 > the 0.25 limit.
    provider = FakeModelProvider([text_response(_VALID_SUMMARY, output_tokens=100_000)])
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "guardrail_halt"
    assert "cost" in result.reason
    assert "guardrail_triggered" in _event_types(result.trace_path)


def test_tool_allowlist_blocks_unregistered_tool(tmp_path: Path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("evil_tool", {}, call_id="c1"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "notes.txt", provider=provider)

    assert result.status == "success"  # blocked tool is surfaced, run recovers
    events = _event_types(result.trace_path)
    assert "guardrail_triggered" in events


def test_dry_run_uses_no_provider(tmp_path: Path):
    harness = _make_harness(tmp_path)
    info = runner.dry_run(harness, "notes.txt")
    assert info["name"] == "example-summarizer"
    assert "file_read" in [t["name"] for t in info["tools"]]
    assert info["estimated_input_tokens"] > 0
