"""Run-scoped notes: the model's own store, out of context and out of reach."""

from __future__ import annotations

import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom import fork as fork_mod
from hiveloom.context.notes import NotesError, NotesStore, NotesTool
from hiveloom.logging.journal import read_events
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.tools.registry import ToolError


def _store(tmp_path: Path, **overrides) -> NotesStore:
    return NotesStore(
        tmp_path / "spill",
        run_id=overrides.pop("run_id", "run_1"),
        max_notes=overrides.pop("max_notes", 4),
        max_note_bytes=overrides.pop("max_note_bytes", 1000),
        max_read_bytes=overrides.pop("max_read_bytes", 200),
        **overrides,
    )


def _harness(tmp_path: Path, **params) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="notes-harness", task="Produce a summary.")
    construct.add_tool(
        directory, builtin="notes", description="Keep run-scoped notes.", **params
    )
    construct.set_field(directory, "loop.require_verification", "false")
    return directory


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def test_a_note_is_written_and_read_back(tmp_path: Path):
    store = _store(tmp_path)
    record = store.write("findings", "the invoice total is 42\nand the date is wrong")

    assert record.total_bytes == len(record.content.encode("utf-8"))
    assert not record.replaced
    assert "invoice total is 42" in store.read("findings")
    assert store.names == ["findings"]


def test_writing_the_same_name_replaces_the_note(tmp_path: Path):
    store = _store(tmp_path)
    store.write("plan", "first draft")
    record = store.write("plan", "second draft")

    assert record.replaced
    assert store.names == ["plan"]
    assert "second draft" in store.read("plan")
    assert "first draft" not in store.read("plan")


def test_a_deleted_note_is_gone_from_the_run(tmp_path: Path):
    store = _store(tmp_path)
    store.write("scratch", "x" * 50)

    assert store.delete("scratch") is True
    assert store.delete("scratch") is False
    assert store.names == []
    with pytest.raises(NotesError, match="no note named"):
        store.read("scratch")


def test_a_read_is_ranged_like_a_spilled_result(tmp_path: Path):
    # A note may be larger than one result's inline budget, so reading it back
    # must not become a way to reintroduce more than that budget at once.
    store = _store(tmp_path, max_read_bytes=100)
    store.write("long", "y" * 500)

    body = store.read("long", offset=0)
    assert "bytes 0-100 of 500" in body
    assert "continue at offset 100" in body
    assert "past the end" in store.read("long", offset=9999)


def test_counts_and_sizes_are_bounded_with_an_actionable_error(tmp_path: Path):
    store = _store(tmp_path, max_notes=2, max_note_bytes=20)
    store.write("one", "a")
    store.write("two", "b")

    with pytest.raises(NotesError, match="allows 2 notes"):
        store.write("three", "c")
    # Replacing an existing note is always allowed — the cap is on how many.
    store.write("two", "b again")
    with pytest.raises(NotesError, match="allows 20 per note"):
        store.write("one", "z" * 21)


def test_capacity_holds_when_writes_run_in_parallel(tmp_path: Path):
    """`loop.tool_execution: parallel` runs a turn's tool calls at once, so the
    capacity check and the slot it claims have to be one step: writers past the
    last free slot must be refused, not all admitted."""
    store = _store(tmp_path, max_notes=2)
    writers = 8
    start = threading.Barrier(writers, timeout=10)

    def write(index: int) -> str:
        start.wait()
        try:
            store.write(f"note-{index}", f"body {index}")
        except NotesError as exc:
            return str(exc)
        return "stored"

    with ThreadPoolExecutor(max_workers=writers) as pool:
        outcomes = list(pool.map(write, range(writers)))

    assert outcomes.count("stored") == 2
    assert all("allows 2 notes" in o for o in outcomes if o != "stored")
    # What the map says is what is on disk and in the prompt index.
    assert len(store.names) == 2
    assert len(store.index()) == 2
    for name in store.names:
        assert "body" in store.read(name)


def test_a_parallel_rewrite_never_counts_against_capacity(tmp_path: Path):
    store = _store(tmp_path, max_notes=2)
    store.write("plan", "first draft")
    store.write("findings", "first finding")
    start = threading.Barrier(8, timeout=10)

    def rewrite(index: int) -> None:
        start.wait()
        store.write("plan" if index % 2 else "findings", f"redraft {index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(rewrite, range(8)))

    assert store.names == ["findings", "plan"]
    assert "redraft" in store.read("plan")


def test_an_unusable_name_is_refused(tmp_path: Path):
    store = _store(tmp_path)
    for name in ["../escape", "Findings", "with space", "", "-leading", "x" * 65]:
        with pytest.raises(NotesError, match="not a usable note name"):
            store.write(name, "x")


def test_redaction_applies_before_the_write(tmp_path: Path):
    store = _store(tmp_path, redact=lambda text: text.replace("sk-live", "[REDACTED]"))
    record = store.write("leak", "token=sk-live-abcdef")

    assert "sk-live" not in record.path.read_text(encoding="utf-8")
    assert "sk-live" not in store.read("leak")


def test_a_note_is_private_to_the_run_that_wrote_it(tmp_path: Path):
    writer = _store(tmp_path, run_id="run_one")
    record = writer.write("shared", "z" * 50)

    other = _store(tmp_path, run_id="run_two")  # same root, different run
    assert other.names == []
    with pytest.raises(NotesError, match="no note named"):
        other.read("shared")

    # Authority is a hash-bound grant, and only against the real file.
    manifest = {"name": "shared", "sha256": record.sha256, "bytes": record.total_bytes}
    source = tmp_path / "spill" / "run_one" / "notes"
    assert other.inherit([{"name": "shared"}], source) == []
    assert other.inherit([manifest], tmp_path / "nowhere") == []
    assert other.inherit([manifest], source) == ["shared"]
    assert "z" in other.read("shared")


def test_an_inherited_note_is_rechecked_after_authorization(tmp_path: Path):
    writer = _store(tmp_path, run_id="run_one")
    record = writer.write("shared", "z" * 50)
    heir = _store(tmp_path, run_id="run_two")
    source = tmp_path / "spill" / "run_one" / "notes"
    heir.inherit(
        [{"name": "shared", "sha256": record.sha256, "bytes": record.total_bytes}], source
    )

    (source / "shared.txt").write_text("substituted", encoding="utf-8")
    with pytest.raises(NotesError, match="no longer matches"):
        heir.read("shared")


def test_notes_are_written_0600_in_a_0700_directory(tmp_path: Path):
    store = _store(tmp_path)
    record = store.write("private", "x" * 50)

    assert stat.S_IMODE(record.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.path.parent.stat().st_mode) == 0o700


def test_a_write_that_cannot_be_stored_is_a_tool_error_not_a_crash(tmp_path: Path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    store = NotesStore(blocker / "spill", run_id="run_1")

    with pytest.raises(NotesError, match="could not store note"):
        store.write("anything", "x")
    # The slot the write claimed goes back: a failure costs no capacity, and
    # the name it reserved resolves to nothing.
    assert store.names == []


def test_every_change_is_journalled_with_its_content(tmp_path: Path):
    events: list[tuple] = []
    store = _store(tmp_path, journal=lambda kind, **payload: events.append((kind, payload)))
    store.write("kept", "the answer is 42")
    store.delete("kept")

    assert events[0][0] == "note_written"
    assert events[0][1]["content"] == "the answer is 42"
    assert events[0][1]["bytes"] == 16
    assert events[1] == ("note_deleted", {"name": "kept"})


def test_journalling_never_fails_a_stored_note(tmp_path: Path):
    def broken(_kind, **_payload):
        raise RuntimeError("journal is unavailable")

    store = _store(tmp_path, journal=broken)
    assert store.write("kept", "still stored").total_bytes == 12


# --------------------------------------------------------------------------- #
# The prompt index
# --------------------------------------------------------------------------- #
def test_the_index_lists_names_sizes_and_first_lines_sorted(tmp_path: Path):
    store = _store(tmp_path)
    store.write("zeta", "last alphabetically\nbut written first")
    store.write("alpha", "first line here\nsecond line")

    index = store.index_text()
    assert index.startswith("# Notes")
    assert index.index("alpha") < index.index("zeta")
    assert "- alpha (27 bytes): first line here" in index
    assert "second line" not in index


def test_an_empty_store_renders_no_section(tmp_path: Path):
    assert _store(tmp_path).index_text() is None


def test_a_long_first_line_is_truncated_in_the_index(tmp_path: Path):
    store = _store(tmp_path)
    store.write("wide", "w" * 400)
    line = store.index_text().splitlines()[-1]

    assert line.endswith("…")
    assert len(line) < 160


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def test_the_tool_is_unusable_until_it_is_bound(tmp_path: Path):
    with pytest.raises(ToolError, match="not available in this context"):
        NotesTool().run(action="write", name="x", content="y")


def test_the_tool_reports_actions_and_refuses_unknown_ones(tmp_path: Path):
    tool = NotesTool(max_notes=3)
    tool.bind(_store(tmp_path, max_notes=3))

    assert "wrote note 'a'" in tool.run(action="write", name="a", content="hello")
    assert "1 of 3 notes in use" in tool.run(action="write", name="a", content="hello again")
    assert "hello again" in tool.run(action="read", name="a")
    assert "deleted note 'a'" in tool.run(action="delete", name="a")
    assert "no note named 'a'" in tool.run(action="delete", name="a")
    with pytest.raises(ToolError, match="unknown action"):
        tool.run(action="append", name="a", content="x")
    with pytest.raises(ToolError, match='needs "content"'):
        tool.run(action="write", name="a")


def test_the_spec_limits_reach_the_tool(tmp_path: Path):
    from hiveloom.spec.schema import HarnessSpec
    from hiveloom.tools.registry import build_registry

    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "tools": [{"builtin": "notes", "max_notes": 2, "max_note_bytes": 128}],
        }
    )
    tool = build_registry(spec, tmp_path).get("notes")

    assert (tool.max_notes, tool.max_note_bytes) == (2, 128)
    # Hard caps bound whatever the spec asked for.
    assert NotesTool(max_notes=99_999, max_note_bytes=99_999_999).max_notes == 256


# --------------------------------------------------------------------------- #
# In a run
# --------------------------------------------------------------------------- #
def test_a_note_written_in_a_run_reaches_the_next_system_prompt(tmp_path: Path):
    harness = _harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response(
                "notes",
                {"action": "write", "name": "findings", "content": "the total is 42"},
                call_id="c1",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    assert "# Notes" not in provider.calls[0]["system"]
    assert "- findings (15 bytes): the total is 42" in provider.calls[1]["system"]
    # The result confirms the write rather than echoing the note back: the
    # index is what a stored note costs on later turns, not its body.
    blocks = provider.calls[1]["messages"][-1]["content"]
    assert "the total is 42" not in "".join(
        b["content"] for b in blocks if b["type"] == "tool_result"
    )

    stored = Path(result.trace_path).parent / "spill" / result.run_id / "notes"
    assert (stored / "findings.txt").read_text(encoding="utf-8") == "the total is 42"
    written = next(e for e in read_events(result.trace_path) if e["type"] == "note_written")
    assert written["payload"]["content"] == "the total is 42"


def test_a_note_read_is_not_spilled_again(tmp_path: Path):
    harness = _harness(tmp_path, max_note_bytes=200_000)
    construct.set_field(harness, "context.tool_results.max_inline_bytes", "8000")
    body = "N" * 40_000
    provider = FakeModelProvider(
        [
            tool_response(
                "notes", {"action": "write", "name": "big", "content": body}, call_id="c1"
            ),
            tool_response(
                "notes", {"action": "read", "name": "big", "limit": 999_999}, call_id="c2"
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    blocks = provider.calls[2]["messages"][-1]["content"]
    read_back = "".join(b["content"] for b in blocks if b["type"] == "tool_result")
    assert "[hiveloom spill]" not in read_back
    # ...and the read is bounded by the same budget a spill would have been.
    assert len(read_back.encode("utf-8")) < 12_000


def test_the_notes_tool_is_opt_in(tmp_path: Path):
    directory = tmp_path / "plain"
    construct.init_harness(directory, name="plain", task="Do a thing.")
    construct.set_field(directory, "loop.require_verification", "false")
    provider = FakeModelProvider([text_response("done")])
    runner.run_harness(
        directory, "go", provider=provider, literal_input=True, ingest=False
    )

    assert "notes" not in {t["name"] for t in provider.calls[0]["tools"]}
    assert "# Notes" not in provider.calls[0]["system"]


def test_a_fork_carries_the_notes_its_parent_held(tmp_path: Path):
    harness = _harness(tmp_path)
    parent = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [
                tool_response(
                    "notes",
                    {"action": "write", "name": "carried", "content": "keep me"},
                    call_id="c1",
                ),
                tool_response(
                    "notes",
                    {"action": "write", "name": "dropped", "content": "forget me"},
                    call_id="c2",
                ),
                tool_response(
                    "notes", {"action": "delete", "name": "dropped"}, call_id="c3"
                ),
                text_response("done"),
            ]
        ),
        literal_input=True,
        ingest=False,
    )

    forked = fork_mod.create_fork(parent.trace_path, tmp_path / "fork")
    record = fork_mod.load_fork(forked.directory)
    # Replayed from the journal: a note written and then deleted is not a note
    # the fork inherits.
    assert [item["name"] for item in record["notes_manifest"]] == ["carried"]
    inherited = forked.directory / ".hiveloom" / "traces" / "spill" / "notes-inherited"
    assert (inherited / "carried.txt").read_text(encoding="utf-8") == "keep me"
    assert not (inherited / "dropped.txt").exists()

    provider = FakeModelProvider([text_response("done")])
    resumed = runner.run_harness(
        forked.directory,
        resume_messages=fork_mod.load_fork_context(forked.directory),
        lineage={
            "parent_run_id": parent.run_id,
            "forked_at_seq": forked.at_seq,
            "notes_manifest": record["notes_manifest"],
        },
        provider=provider,
        ingest=False,
    )

    assert resumed.status == "success"
    carried = next(
        e for e in read_events(resumed.trace_path) if e["type"] == "notes_inherited"
    )
    assert carried["payload"]["names"] == ["carried"]
    assert "- carried (7 bytes): keep me" in provider.calls[0]["system"]


def test_an_unchained_journal_cannot_authorize_note_inheritance(tmp_path: Path):
    import json

    harness = _harness(tmp_path)
    parent = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [
                tool_response(
                    "notes",
                    {"action": "write", "name": "carried", "content": "keep me"},
                    call_id="c1",
                ),
                text_response("done"),
            ]
        ),
        literal_input=True,
        ingest=False,
    )
    trace_path = Path(parent.trace_path)
    lines = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        event.pop("prev", None)
        lines.append(json.dumps(event))
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    forked = fork_mod.create_fork(parent.trace_path, tmp_path / "unchained-fork")
    assert fork_mod.load_fork(forked.directory)["notes_manifest"] == []


def test_the_note_index_goes_through_the_egress_filter(tmp_path: Path):
    # The index is system-prompt text like any other, so a credential written
    # into a note must not reach the provider by that route either.
    harness = _harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response(
                "notes",
                {
                    "action": "write",
                    "name": "creds",
                    "content": "aws_access_key_id=AKIAQRSTUVWXYZ012345",
                },
                call_id="c1",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    assert "# Notes" in provider.calls[1]["system"]
    assert "AKIAQRSTUVWXYZ012345" not in provider.calls[1]["system"]
    assert "[REDACTED]" in provider.calls[1]["system"]
    assert any(
        e["type"] == "provider_egress_redacted" for e in read_events(result.trace_path)
    )
