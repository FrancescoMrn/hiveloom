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


def test_summary_level_omits_model_events(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "h", "abc", level="summary")
    trace.emit("model_call", context_head=0, system_hash="x")
    trace.emit("tool_call", name="file_read")
    trace.emit("run_finished", status="success")

    assert [event.type for event in trace.events] == ["tool_call", "run_finished"]


def test_pre_1_0_level_names_still_work(tmp_path: Path):
    """`harness.yaml` is a portable artifact; a rename must not break old ones."""
    trace = TraceWriter(tmp_path, "run_1", "h", "abc", level="tool_calls_only")
    trace.emit("model_call", context_head=0)
    trace.emit("tool_call", name="file_read")

    assert [event.type for event in trace.events] == ["tool_call"]

    spec = HarnessSpec.model_validate(
        {"name": "t", "description": "d", "system_prompt": "sp",
         "logging": {"level": "tool_calls_only"}}
    )
    assert spec.logging.level == "summary"


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


def test_force_compact_ignores_trigger_and_halves_budget():
    spec = _spec(
        max_input_tokens=100_000,  # trigger never fires on its own
        strategy="rolling",
        compaction={"trigger_at_pct": 80, "method": "truncate_oldest"},
    )
    cm = ContextManager(spec, FakeModelProvider([]), None)
    cm.add_user("TASK: pinned")
    for i in range(10):
        cm.add_user("filler message number " + str(i) + " with some length to it")

    assert cm.maybe_compact() is False
    assert cm.force_compact() is True
    assert len(cm.messages) < 11
    assert cm.messages[0]["content"].startswith("TASK:")


def test_force_compact_refuses_when_nothing_compactible():
    spec = _spec(strategy="rolling")
    cm = ContextManager(spec, FakeModelProvider([]), None)
    cm.add_user("TASK: pinned")
    cm.add_user("one more")
    assert cm.force_compact() is False


def test_force_compact_disabled_for_full_strategy():
    spec = _spec(strategy="full")
    cm = ContextManager(spec, FakeModelProvider([]), None)
    for i in range(5):
        cm.add_user("message " + str(i))
    assert cm.force_compact() is False


def test_summarize_compaction_prompts_for_structured_sections():
    from hiveloom.models.fake import text_response

    spec = _spec(
        max_input_tokens=40,
        strategy="rolling",
        compaction={"trigger_at_pct": 1, "method": "summarize"},
    )
    provider = FakeModelProvider([text_response("# Goal\n- summarize\n# Next steps\n- none")])
    cm = ContextManager(spec, provider, None)
    cm.add_user("TASK: pinned first message")
    for i in range(6):
        cm.add_user("filler message number " + str(i) + " with some length to it")

    assert cm.maybe_compact() is True

    prompt = provider.calls[0]["messages"][0]["content"]
    sections = ("# Goal", "# Progress", "# Key decisions", "# Next steps", "# Critical context")
    for section in sections:
        assert section in prompt
    assert any(
        "summary of earlier turns" in str(m.get("content", "")) for m in cm.messages
    )


def _tool_cycle(cm: ContextManager, call_id: str, filler: str = "") -> None:
    """One assistant tool_use plus the user tool_result answering it."""
    cm.add_assistant(
        [
            {"type": "text", "text": "working" + filler},
            {"type": "tool_use", "id": call_id, "name": "shell", "input": {"command": "ls"}},
        ]
    )
    cm.add_tool_results([{"tool_use_id": call_id, "content": "exit=0" + filler}])


def test_summarize_does_not_orphan_the_trailing_tool_result():
    """A retained tool_result whose tool_use was summarized away is a 400."""
    from hiveloom.models.fake import text_response

    spec = _spec(
        max_input_tokens=40,
        strategy="rolling",
        compaction={"trigger_at_pct": 1, "method": "summarize"},
    )
    provider = FakeModelProvider([text_response("# Goal\n- go\n# Next steps\n- none")])
    cm = ContextManager(spec, provider, None)
    cm.add_user("TASK: pinned first message")
    for index in range(4):
        _tool_cycle(cm, f"toolu_{index}", filler=" with enough length to force compaction")

    assert cm.maybe_compact() is True

    _assert_tool_blocks_paired(cm.messages)
    assert all(m.get("content") for m in cm.messages)


def test_truncate_oldest_does_not_orphan_a_tool_result():
    spec = _spec(
        max_input_tokens=40,
        strategy="rolling",
        compaction={"trigger_at_pct": 1, "method": "truncate_oldest"},
    )
    cm = ContextManager(spec, FakeModelProvider([]), None)
    cm.add_user("TASK: pinned first message")
    for index in range(6):
        _tool_cycle(cm, f"toolu_{index}", filler=" with enough length to force compaction")

    assert cm.maybe_compact() is True

    _assert_tool_blocks_paired(cm.messages)


def test_orphan_repair_keeps_the_answerable_half_of_a_mixed_message():
    from hiveloom.context.manager import _drop_orphan_tool_results

    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "kept", "name": "shell", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "kept", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "dropped", "content": "orphan"},
            ],
        },
    ]

    repaired = _drop_orphan_tool_results(messages)

    assert [b["tool_use_id"] for b in repaired[1]["content"]] == ["kept"]
    # The input is not mutated in place; callers may still hold it.
    assert len(messages[1]["content"]) == 2


def test_orphan_repair_drops_a_message_left_with_no_blocks():
    from hiveloom.context.manager import _drop_orphan_tool_results

    messages = [
        {"role": "user", "content": "task"},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "orphan"}],
        },
    ]

    assert _drop_orphan_tool_results(messages) == [{"role": "user", "content": "task"}]


def _assert_tool_blocks_paired(messages: list) -> None:
    seen: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                seen.add(block["id"])
            elif block.get("type") == "tool_result":
                assert block["tool_use_id"] in seen, f"orphaned tool_result: {block}"
