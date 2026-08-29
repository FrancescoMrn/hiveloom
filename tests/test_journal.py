"""Tests for the run journal: progressive context events and the fold."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hiveloom import runner
from hiveloom.context.manager import ContextManager
from hiveloom.logging.journal import (
    fold_events,
    read_events,
    state_at,
    state_at_model_call,
    verify_chain,
)
from hiveloom.logging.trace import TraceWriter, payload_hash
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.schema import HarnessSpec

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def make_harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 20)
    return target


def _spec(**context) -> HarnessSpec:
    return HarnessSpec.model_validate(
        {"name": "t", "description": "d", "system_prompt": "sp", "context": context}
    )


def _events(trace: TraceWriter) -> list[dict]:
    return read_events(trace.path)


# --------------------------------------------------------------------------- #
# The fold
# --------------------------------------------------------------------------- #
def test_appends_fold_back_into_the_message_list(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    cm = ContextManager(_spec(), FakeModelProvider([]), trace)

    cm.add_user("one")
    cm.add_assistant([{"type": "text", "text": "two"}])
    cm.add_user("three")

    assert fold_events(_events(trace)).messages == cm.messages


def test_seeded_history_is_journalled_message_by_message(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    cm = ContextManager(_spec(), FakeModelProvider([]), trace)

    cm.seed_history(
        [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
        ]
    )
    cm.add_user("now")

    events = _events(trace)
    assert [e["type"] for e in events] == ["context_append"] * 3
    assert fold_events(events).messages == cm.messages


def test_compaction_replaces_rather_than_extends_the_fold(tmp_path: Path):
    spec = _spec(
        max_input_tokens=20, compaction={"trigger_at_pct": 1, "method": "truncate_oldest"}
    )
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    cm = ContextManager(spec, FakeModelProvider([]), trace)
    cm.add_user("first task message pinned")
    for i in range(6):
        cm.add_user(f"some filler content here {i}")

    assert cm.maybe_compact()

    events = _events(trace)
    assert any(e["type"] == "context_compaction" for e in events)
    # The fold must land on the *compacted* list, not the pre-compaction one.
    folded = fold_events(events)
    assert folded.messages == cm.messages
    assert len(folded.messages) < 7


def test_state_at_reconstructs_an_earlier_point(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    cm = ContextManager(_spec(), FakeModelProvider([]), trace)
    cm.add_user("one")
    cm.add_user("two")
    cm.add_user("three")

    events = _events(trace)
    at_first = state_at(events, events[0]["seq"])

    assert [m["content"] for m in at_first.messages] == ["one"]
    assert [m["content"] for m in state_at(events).messages] == ["one", "two", "three"]


def test_system_and_tools_are_journalled_only_when_they_change(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")

    assert trace.emit_context_system("sp") == trace.emit_context_system("sp")
    trace.emit_context_tools([{"name": "a"}])
    trace.emit_context_tools([{"name": "a"}])
    trace.emit_context_system("changed")

    assert [e["type"] for e in _events(trace)] == [
        "context_system",
        "context_tools",
        "context_system",
    ]
    state = fold_events(_events(trace))
    assert state.system == "changed"
    assert state.tools == [{"name": "a"}]


def test_context_head_tracks_the_last_context_event(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    assert trace.context_head == -1

    trace.emit("run_started")
    assert trace.context_head == -1

    event = trace.emit("context_append", index=0, message={"role": "user", "content": "x"})
    assert trace.context_head == event.seq

    trace.emit("model_response", text="hi")
    assert trace.context_head == event.seq


# --------------------------------------------------------------------------- #
# Fold hazards
# --------------------------------------------------------------------------- #
def test_inline_compaction_call_does_not_disturb_the_fold():
    """The summarisation turn is out-of-band and must not become history."""
    events = [
        {
            "seq": 0,
            "type": "context_append",
            "payload": {"message": {"role": "user", "content": "real"}},
        },
        {
            "seq": 1,
            "type": "model_call",
            "payload": {
                "phase": "compaction",
                "inline": True,
                "system": "You compress agent transcripts",
                "messages": [{"role": "user", "content": "summarize this"}],
            },
        },
    ]
    assert [m["content"] for m in fold_events(events).messages] == ["real"]
    assert fold_events(events).system == ""


def test_pre_1_0_snapshot_traces_still_fold():
    """A 0.x model_call carried the whole request; treat it as a replace."""
    events = [
        {
            "seq": 0,
            "type": "model_call",
            "payload": {
                "system": "old sp",
                "messages": [{"role": "user", "content": "old"}],
                "tools": [{"name": "t"}],
            },
        }
    ]
    state = fold_events(events)
    assert state.system == "old sp"
    assert [m["content"] for m in state.messages] == ["old"]
    assert state.tools == [{"name": "t"}]


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def test_a_real_run_folds_back_to_what_the_provider_received(tmp_path: Path):
    harness = make_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(harness, "task", provider=provider, ingest=False)

    events = read_events(result.trace_path)
    calls = [e for e in events if e["type"] == "model_call"]
    assert len(calls) == len(provider.calls)

    for event, sent in zip(calls, provider.calls, strict=True):
        state = state_at_model_call(events, event["seq"])
        assert state.messages == sent["messages"]
        assert state.system == sent["system"]
        assert payload_hash(state.messages) == event["payload"]["messages_hash"]


def test_the_journal_no_longer_re_snapshots_the_conversation(tmp_path: Path):
    harness = make_harness(tmp_path)
    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "notes.txt"}), text_response(_VALID_SUMMARY)]
    )
    result = runner.run_harness(harness, "task", provider=provider, ingest=False)

    for line in Path(result.trace_path).read_text().splitlines():
        event = json.loads(line)
        if event["type"] == "model_call" and not event["payload"].get("inline"):
            assert "messages" not in event["payload"]
            assert "system" not in event["payload"]
            assert "tools" not in event["payload"]


# --------------------------------------------------------------------------- #
# Integrity (1.2)
# --------------------------------------------------------------------------- #
def test_a_written_journal_is_chained_and_intact(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    trace.emit("run_started", input="x")
    trace.emit("context_append", index=0, message={"role": "user", "content": "hi"})
    trace.emit("run_finished", status="success")

    chain = verify_chain(trace.path)
    assert chain.ok and chain.chained and chain.checked == 3
    assert "intact" in chain.summary()


def test_first_event_chains_to_genesis(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    trace.emit("run_started")
    assert json.loads(trace.path.read_text().splitlines()[0])["prev"] == ""


def test_editing_a_line_breaks_the_chain(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    for i in range(4):
        trace.emit("context_append", index=i, message={"role": "user", "content": str(i)})

    lines = trace.path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["message"]["content"] = "forged"
    lines[1] = json.dumps(tampered)
    trace.path.write_text("\n".join(lines) + "\n")

    chain = verify_chain(trace.path)
    assert not chain.ok
    # Line 1 is rewritten, so line 2 is the first whose `prev` no longer matches.
    assert chain.broken_at == 2
    assert "BROKEN" in chain.summary()


def test_removing_a_line_breaks_the_chain(tmp_path: Path):
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    for i in range(4):
        trace.emit("context_append", index=i, message={"role": "user", "content": str(i)})

    lines = trace.path.read_text().splitlines()
    del lines[2]
    trace.path.write_text("\n".join(lines) + "\n")

    chain = verify_chain(trace.path)
    assert not chain.ok and chain.broken_at == 2


def test_truncating_the_tail_is_not_a_break(tmp_path: Path):
    """Append-only means a prefix is always valid — an interrupted run is not tampering."""
    trace = TraceWriter(tmp_path, "run_1", "t", "hash")
    for i in range(4):
        trace.emit("context_append", index=i, message={"role": "user", "content": str(i)})

    lines = trace.path.read_text().splitlines()
    trace.path.write_text("\n".join(lines[:2]) + "\n")

    assert verify_chain(trace.path).ok


def test_pre_1_0_traces_report_unchained_not_broken(tmp_path: Path):
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps({"run_id": "r", "seq": 0, "type": "run_started", "payload": {}}) + "\n"
    )
    chain = verify_chain(path)
    assert chain.ok and not chain.chained
    assert "unchained" in chain.summary()


def test_dropped_events_do_not_enter_the_chain(tmp_path: Path):
    """`level` filtering skips writes; the chain must cover written lines only."""
    trace = TraceWriter(tmp_path, "run_1", "t", "hash", level="summary")
    trace.emit("run_started")
    trace.emit("model_response", text="dropped")
    trace.emit("tool_call", name="file_read")
    trace.emit("run_finished", status="success")

    chain = verify_chain(trace.path)
    assert chain.ok and chain.checked == 3


# --------------------------------------------------------------------------- #
# Self-description (1.3) and completeness (1.4)
# --------------------------------------------------------------------------- #
def test_run_started_carries_the_harness_that_produced_the_run(tmp_path: Path):
    harness = make_harness(tmp_path)
    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
    result = runner.run_harness(harness, "notes.txt", provider=provider, ingest=False)

    started = read_events(result.trace_path)[0]
    snapshot = started["payload"]["harness"]

    assert started["type"] == "run_started"
    assert "name: example-summarizer" in snapshot["spec"]
    assert snapshot["version_hash"] == started["harness_version_hash"]
    # The manifest covers the validator and the output schema — the behavioural
    # files, i.e. exactly what the version hash fingerprints.
    assert "validators/check_summary.py" in snapshot["files"]
    assert all(len(h) == 64 for h in snapshot["files"].values())
    assert "contents" not in snapshot  # snapshot_files is off by default


def test_snapshot_files_inlines_the_bodies(tmp_path: Path):
    harness = make_harness(tmp_path)
    spec_path = harness / "harness.yaml"
    spec_path.write_text(
        spec_path.read_text() + "\nlogging:\n  snapshot_files: true\n"
    )
    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
    result = runner.run_harness(harness, "notes.txt", provider=provider, ingest=False)

    snapshot = read_events(result.trace_path)[0]["payload"]["harness"]
    contents = snapshot["contents"]
    assert "validators/check_summary.py" in contents
    assert contents["validators/check_summary.py"] == (
        harness / "validators" / "check_summary.py"
    ).read_text()


def test_harness_snapshot_respects_the_byte_budget(tmp_path: Path):
    from hiveloom.logging import trace as trace_mod
    from hiveloom.spec.loader import load_spec

    harness = make_harness(tmp_path)
    (harness / "validators" / "check_summary.py").write_text(
        "# " + "x" * (trace_mod.SNAPSHOT_BYTE_BUDGET + 10) + "\n"
    )
    spec = load_spec(harness / "harness.yaml")

    snapshot = trace_mod.harness_snapshot(spec, harness, include_files=True)

    assert "validators/check_summary.py" in snapshot["skipped"]
    assert "validators/check_summary.py" in snapshot["files"]  # hashed regardless


def test_run_finished_records_the_answer_and_the_verdicts(tmp_path: Path):
    harness = make_harness(tmp_path)
    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
    result = runner.run_harness(harness, "notes.txt", provider=provider, ingest=False)

    finished = read_events(result.trace_path)[-1]
    payload = finished["payload"]

    assert finished["type"] == "run_finished"
    assert payload["status"] == "success"
    assert payload["output"] == result.output
    assert payload["verdicts"] and all(v["passed"] for v in payload["verdicts"])


def test_run_finished_records_a_failing_verdict_too(tmp_path: Path):
    harness = make_harness(tmp_path)
    provider = FakeModelProvider([text_response("not json")])
    result = runner.run_harness(harness, "notes.txt", provider=provider, ingest=False)

    payload = read_events(result.trace_path)[-1]["payload"]
    assert payload["status"] == "verify_failed"
    # The journal's record of the answer must be the answer the caller got.
    assert payload["output"] == result.output
    assert any(not v["passed"] for v in payload["verdicts"])


# --------------------------------------------------------------------------- #
# Context meter and file deliverables (workbench support)
# --------------------------------------------------------------------------- #
def test_every_model_call_carries_a_context_meter(tmp_path: Path):
    """A UI must not have to re-tokenize the conversation to draw a budget bar."""
    harness = make_harness(tmp_path)
    result = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        ingest=False,
    )

    calls = [
        e for e in read_events(result.trace_path)
        if e["type"] == "model_call" and not e["payload"].get("inline")
    ]
    assert calls
    for call in calls:
        payload = call["payload"]
        assert payload["input_tokens"] > 0
        assert payload["max_input_tokens"] > 0


def test_file_write_declares_the_file_it_produced(tmp_path: Path):
    """A written file is a deliverable; the result string is not machine-readable."""
    from hiveloom.tools.builtin import FileWriteTool

    tool = FileWriteTool(tmp_path)
    result = tool.run(path="out.txt", content="hello")

    [artifact] = result.artifacts
    assert artifact.kind == "file"
    assert artifact.data["path"] == "out.txt"
    assert artifact.data["action"] == "created"
    assert artifact.data["bytes"] == 5
    assert artifact.data["previous"] is None
    assert len(artifact.data["sha256"]) == 64


def test_overwriting_records_what_it_replaced(tmp_path: Path):
    """Before/after hashes are the one thing the journal could not otherwise say."""
    from hiveloom.tools.builtin import FileWriteTool

    tool = FileWriteTool(tmp_path)
    first = tool.run(path="out.txt", content="before")
    second = tool.run(path="out.txt", content="after")

    data = second.artifacts[0].data
    assert data["action"] == "modified"
    assert data["previous"]["sha256"] == first.artifacts[0].data["sha256"]
    assert data["sha256"] != data["previous"]["sha256"]
    assert data["unchanged"] is False


def test_a_rewrite_with_identical_content_is_marked_unchanged(tmp_path: Path):
    from hiveloom.tools.builtin import FileWriteTool

    tool = FileWriteTool(tmp_path)
    tool.run(path="out.txt", content="same")
    again = tool.run(path="out.txt", content="same")

    assert again.artifacts[0].data["unchanged"] is True


def test_the_file_artifact_reaches_the_run_result(tmp_path: Path):
    """The caller reads artifacts off the run, not out of the model's prose."""
    from hiveloom import construct

    directory = tmp_path / "writer"
    construct.init_harness(directory, name="writer", task="Write a file.")
    construct.add_tool(directory, builtin="file_write")

    result = runner.run_harness(
        directory,
        "go",
        provider=FakeModelProvider(
            [
                tool_response("file_write", {"path": "out.md", "content": "hi"}, call_id="w1"),
                text_response("done"),
            ]
        ),
        ingest=False,
    )

    files = [a for a in result.artifacts if a["kind"] == "file"]
    assert [a["data"]["path"] for a in files] == ["out.md"]
    assert files[0]["tool"] == "file_write"
