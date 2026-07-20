"""Tests for the context manager and trace writer."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom.context.manager import ContextManager
from hiveloom.logging.trace import TraceWriter, spec_version_hash
from hiveloom.models.fake import FakeModelProvider
from hiveloom.spec.schema import HarnessSpec


def _spec(**context) -> HarnessSpec:
    return HarnessSpec.model_validate(
        {"name": "t", "description": "d", "system_prompt": "sp", "context": context}
    )


def test_tool_result_truncation():
    spec = _spec()
    cm = ContextManager(spec, FakeModelProvider([]), tool_result_max_chars=10)
    cm.add_tool_results([{"tool_use_id": "1", "content": "x" * 100}])
    block = cm.messages[0]["content"][0]
    assert "truncated" in block["content"]
    assert len(block["content"]) < 100


def test_default_tool_result_cap_scales_with_context_budget():
    spec = _spec(max_input_tokens=20)
    cm = ContextManager(spec, FakeModelProvider([]))

    cm.add_tool_results([{"tool_use_id": "1", "content": "x" * 100}])

    assert cm.messages[0]["content"][0]["content"].startswith("x" * 20)


def test_truncate_oldest_keeps_task_pinned():
    spec = _spec(
        max_input_tokens=40,
        strategy="rolling",
        compaction={"trigger_at_pct": 1, "method": "truncate_oldest"},
    )
    cm = ContextManager(spec, FakeModelProvider([]), None)
    cm.add_user("TASK: the pinned first message")
    for i in range(10):
        cm.add_user("filler message number " + str(i) + " with some length to it")
    cm.maybe_compact()
    # The first (task) message is always retained.
    assert cm.messages[0]["content"].startswith("TASK:")
    assert len(cm.messages) < 11


def test_compaction_emits_trace_event(tmp_path: Path):
    spec = _spec(max_input_tokens=20, compaction={"trigger_at_pct": 1, "method": "truncate_oldest"})
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    cm = ContextManager(spec, FakeModelProvider([]), trace)
    cm.add_user("first task message pinned")
    for i in range(6):
        cm.add_user("some filler content here " + str(i))
    assert cm.maybe_compact() is True
    assert any(e.type == "context_compaction" for e in trace.events)


def test_full_context_strategy_never_compacts():
    spec = _spec(
        max_input_tokens=20,
        strategy="full",
        compaction={"trigger_at_pct": 1, "method": "truncate_oldest"},
    )
    cm = ContextManager(spec, FakeModelProvider([]))
    for i in range(6):
        cm.add_user(f"large message {i} " * 20)

    assert not cm.maybe_compact()
    assert len(cm.messages) == 6


def test_context_pinned_controls_whether_task_statement_is_retained():
    spec = _spec(
        max_input_tokens=20,
        pinned=[],
        compaction={"trigger_at_pct": 1, "method": "truncate_oldest"},
    )
    cm = ContextManager(spec, FakeModelProvider([]))
    cm.add_user("TASK: may be dropped")
    for i in range(4):
        cm.add_user(f"filler {i} " * 20)

    assert cm.maybe_compact()
    assert not cm.messages[0]["content"].startswith("TASK:")


def test_summary_context_strategy_uses_summary_even_with_truncate_configured():
    spec = _spec(
        max_input_tokens=20,
        strategy="summary",
        compaction={"trigger_at_pct": 1, "method": "truncate_oldest"},
    )
    cm = ContextManager(spec, FakeModelProvider([]))
    cm.add_user("TASK")
    for i in range(3):
        cm.add_user(f"filler {i} " * 20)

    assert cm.maybe_compact()
    assert "[summary of earlier turns]" in cm.messages[1]["content"]


def test_trace_writer_redacts_and_persists(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "h", "abc", redact_patterns=["api[_-]?key"])
    trace.emit("model_call", text="my api_key is secret")
    lines = (tmp_path / "run_1.jsonl").read_text().splitlines()
    event = json.loads(lines[0])
    assert "[REDACTED]" in event["payload"]["text"]
    assert "secret" in event["payload"]["text"]  # only the pattern is scrubbed


def test_trace_seq_increments(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "h", "abc")
    trace.emit("run_started")
    trace.emit("run_finished")
    assert [e.seq for e in trace.events] == [0, 1]


def test_tool_calls_only_trace_omits_model_events(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "h", "abc", level="tool_calls_only")
    trace.emit("model_call", messages=[{"role": "user", "content": "secret"}])
    trace.emit("tool_call", name="file_read")
    trace.emit("run_finished", status="success")

    assert [event.type for event in trace.events] == ["tool_call", "run_finished"]


def test_version_hash_stable_and_short():
    spec = HarnessSpec.model_validate({"name": "t", "description": "d", "system_prompt": "sp"})
    h1 = spec_version_hash(spec)
    h2 = spec_version_hash(spec)
    assert h1 == h2 and len(h1) == 12


def test_version_hash_changes_when_referenced_code_changes(tmp_path: Path):
    hook = tmp_path / "validators" / "check.py"
    hook.parent.mkdir()
    hook.write_text("def validate(output, context): return {'passed': True}\n")
    spec = HarnessSpec(
        name="t",
        description="d",
        system_prompt="s",
        verify={"validators": [{"code": "validators/check.py:validate"}]},
    )
    before = spec_version_hash(spec, tmp_path)
    hook.write_text("def validate(output, context): return {'passed': False}\n")
    after = spec_version_hash(spec, tmp_path)
    assert before != after
