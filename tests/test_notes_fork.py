"""Run-scoped notes across ``hiveloom fork``: fork-point state, forks of forks,
resumes, and the store's own concurrency."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from hiveloom import construct, runner
from hiveloom import fork as fork_mod
from hiveloom.context import notes as notes_mod
from hiveloom.context.notes import NotesStore
from hiveloom.logging.journal import read_events
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response


def _harness(tmp_path: Path) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="notes-harness", task="Produce a summary.")
    construct.add_tool(directory, builtin="notes", description="Keep run-scoped notes.")
    construct.set_field(directory, "loop.require_verification", "false")
    return directory


def _note(action: str, name: str, call_id: str, content: str | None = None):
    args = {"action": action, "name": name}
    if content is not None:
        args["content"] = content
    return tool_response("notes", args, call_id=call_id)


def _inherited_dir(fork_dir: Path) -> Path:
    return fork_dir / ".hiveloom" / "traces" / "spill" / "notes-inherited"


def _resume(fork_dir: Path, parent_run_id: str, responses: list, manifest=None):
    record = fork_mod.load_fork(fork_dir)
    provider = FakeModelProvider(responses)
    result = runner.run_harness(
        fork_dir,
        resume_messages=fork_mod.load_fork_context(fork_dir),
        lineage={
            "parent_run_id": parent_run_id,
            "forked_at_seq": record["at_seq"],
            "notes_manifest": record["notes_manifest"] if manifest is None else manifest,
        },
        provider=provider,
        ingest=False,
    )
    return result, provider


def _inherited_event(trace_path) -> dict:
    return next(
        e["payload"] for e in read_events(trace_path) if e["type"] == "notes_inherited"
    )


def test_a_fork_carries_each_note_as_it_was_at_the_fork_point(tmp_path: Path):
    # Notes are rewritten in place, so the parent's file only ever holds the
    # latest version. The fork point's version comes from the journal.
    parent = runner.run_harness(
        _harness(tmp_path),
        "go",
        provider=FakeModelProvider(
            [
                _note("write", "a", "c1", "v1"),
                _note("write", "b", "c2", "bee"),
                _note("write", "a", "c3", "version two"),
                _note("delete", "b", "c4"),
                text_response("done"),
            ]
        ),
        literal_input=True,
        ingest=False,
    )
    points = fork_mod.fork_points(read_events(parent.trace_path))

    # After c2: a=v1 (since rewritten) and b (since deleted).
    early = fork_mod.create_fork(parent.trace_path, tmp_path / "early", at=points[2].seq)
    manifest = fork_mod.load_fork(early.directory)["notes_manifest"]
    assert early.warnings == []
    assert {m["name"]: m["sha256"] for m in manifest} == {
        "a": hashlib.sha256(b"v1").hexdigest(),
        "b": hashlib.sha256(b"bee").hexdigest(),
    }
    carried = _inherited_dir(early.directory)
    assert (carried / "a.txt").read_text(encoding="utf-8") == "v1"
    assert (carried / "b.txt").read_text(encoding="utf-8") == "bee"

    resumed, provider = _resume(early.directory, parent.run_id, [text_response("ok")])
    assert _inherited_event(resumed.trace_path)["names"] == ["a", "b"]
    assert "- a (2 bytes): v1" in provider.calls[0]["system"]


def test_a_fork_of_a_fork_keeps_the_notes_the_first_fork_inherited(tmp_path: Path):
    parent = runner.run_harness(
        _harness(tmp_path),
        "go",
        provider=FakeModelProvider(
            [_note("write", "kept", "c1", "from the parent"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )
    child_fork = fork_mod.create_fork(parent.trace_path, tmp_path / "child")
    child, _ = _resume(
        child_fork.directory,
        parent.run_id,
        [_note("write", "own", "c1", "from the child"), text_response("ok")],
    )
    event = _inherited_event(child.trace_path)
    # The grant carries what a later fork needs to rebuild it.
    assert event["notes"] == [
        {
            "name": "kept",
            "sha256": hashlib.sha256(b"from the parent").hexdigest(),
            "bytes": 15,
            "content": "from the parent",
        }
    ]

    grandchild = fork_mod.create_fork(child.trace_path, tmp_path / "grandchild")
    assert grandchild.warnings == []
    names = [m["name"] for m in fork_mod.load_fork(grandchild.directory)["notes_manifest"]]
    assert names == ["kept", "own"]
    carried = _inherited_dir(grandchild.directory)
    assert (carried / "kept.txt").read_text(encoding="utf-8") == "from the parent"


def test_an_old_names_only_grant_is_carried_from_the_lineage_digest(
    tmp_path: Path, monkeypatch
):
    # A journal written before `notes_inherited` carried content names the
    # grant only. The run's own `run_started.lineage` still binds each name to
    # a digest, and the fork's copy on disk is checked against it.
    parent = runner.run_harness(
        _harness(tmp_path),
        "go",
        provider=FakeModelProvider(
            [_note("write", "kept", "c1", "old style"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )
    child_fork = fork_mod.create_fork(parent.trace_path, tmp_path / "child")
    monkeypatch.setattr(NotesStore, "carried", lambda self, names: [])
    child, _ = _resume(child_fork.directory, parent.run_id, [text_response("ok")])
    assert _inherited_event(child.trace_path)["notes"] == []

    grandchild = fork_mod.create_fork(child.trace_path, tmp_path / "grandchild")
    assert grandchild.warnings == []
    assert (_inherited_dir(grandchild.directory) / "kept.txt").read_text(
        encoding="utf-8"
    ) == "old style"


def test_a_grant_with_no_digest_anywhere_is_reported_not_dropped(tmp_path: Path):
    events = [
        {"seq": 0, "type": "run_started", "payload": {}},
        {"seq": 1, "type": "notes_inherited", "payload": {"names": ["orphan"]}},
    ]
    held = fork_mod._notes_in_journal(events, 1)
    assert held["orphan"]["sha256"] is None

    trace = tmp_path / "traces" / "run.jsonl"
    manifest, warnings = fork_mod._carry_notes(
        trace, {"spec": "{}"}, tmp_path / "fork", held
    )
    assert manifest == []
    assert len(warnings) == 1 and "orphan" in warnings[0]


def test_journaled_content_that_does_not_match_its_digest_falls_back_to_the_file(
    tmp_path: Path,
):
    events = [
        {
            "seq": 1,
            "type": "note_written",
            "payload": {
                "name": "a",
                "bytes": 4,
                "sha256": hashlib.sha256(b"real").hexdigest(),
                "content": "fake",
            },
        }
    ]
    held = fork_mod._notes_in_journal(events, 1)
    assert held["a"]["raw"] is None

    spill = tmp_path / "traces" / "spill" / "run_1" / "notes"
    spill.mkdir(parents=True)
    (spill / "a.txt").write_bytes(b"real")
    manifest, warnings = fork_mod._carry_notes(
        tmp_path / "traces" / "run.jsonl", {"spec": "{}"}, tmp_path / "fork", held
    )
    assert [m["name"] for m in manifest] == ["a"] and warnings == []
    assert (_inherited_dir(tmp_path / "fork") / "a.txt").read_bytes() == b"real"


def test_deleting_an_inherited_note_keeps_the_forks_copy(tmp_path: Path):
    # The inherited file is shared by every resume of the fork; a resume that
    # deletes the note drops its grant, not the next resume's inheritance.
    root = tmp_path / "spill"
    inherited = root / "notes-inherited"
    inherited.mkdir(parents=True)
    (inherited / "a.txt").write_bytes(b"keep")
    manifest = [{"name": "a", "sha256": hashlib.sha256(b"keep").hexdigest(), "bytes": 4}]

    first = NotesStore(root, run_id="resume_1")
    assert first.inherit(manifest, first.inherited_dir) == ["a"]
    assert first.delete("a")
    assert first.names == []
    assert (inherited / "a.txt").exists()

    second = NotesStore(root, run_id="resume_2")
    assert second.inherit(manifest, second.inherited_dir) == ["a"]
    # Its own notes are still removed from disk.
    record = second.write("own", "x")
    second.delete("own")
    assert not record.path.exists()


def test_a_resume_granted_fewer_notes_than_its_manifest_says_so(tmp_path: Path):
    parent = runner.run_harness(
        _harness(tmp_path),
        "go",
        provider=FakeModelProvider(
            [
                _note("write", "a", "c1", "one"),
                _note("write", "b", "c2", "two"),
                text_response("done"),
            ]
        ),
        literal_input=True,
        ingest=False,
    )
    forked = fork_mod.create_fork(parent.trace_path, tmp_path / "fork")
    (_inherited_dir(forked.directory) / "b.txt").write_text("tampered", encoding="utf-8")

    resumed, _ = _resume(forked.directory, parent.run_id, [text_response("ok")])
    event = _inherited_event(resumed.trace_path)
    assert event["names"] == ["a"]
    assert event["missing"] == ["b"]

    # Nothing granted at all is still an event, not silence.
    for name in ("a", "b"):
        (_inherited_dir(forked.directory) / f"{name}.txt").unlink()
    again, _ = _resume(forked.directory, parent.run_id, [text_response("ok")])
    event = _inherited_event(again.trace_path)
    assert event["names"] == [] and event["missing"] == ["a", "b"]


def test_parallel_rewrites_of_one_name_leave_a_readable_note(tmp_path: Path, monkeypatch):
    # Force the interleaving that used to break: A moves its file first but
    # records last, so the file held A's bytes while the map expected B's.
    events: list[tuple[str, str]] = []
    store = NotesStore(
        tmp_path / "spill",
        run_id="run_1",
        journal=lambda event, **payload: events.append((event, payload.get("content"))),
    )
    real_replace = os.replace

    class _SlowOs:
        def __getattr__(self, attr):
            return getattr(os, attr)

        @staticmethod
        def replace(src, dst):
            if threading.current_thread().name == "A":
                time.sleep(0.2)
                real_replace(src, dst)
            else:
                real_replace(src, dst)
                time.sleep(0.3)

    monkeypatch.setattr(notes_mod, "os", _SlowOs())
    first = threading.Thread(target=store.write, args=("x", "from A"), name="A")
    second = threading.Thread(target=store.write, args=("x", "from B, longer"), name="B")
    first.start()
    time.sleep(0.02)
    second.start()
    first.join()
    second.join()

    on_disk = (tmp_path / "spill" / "run_1" / "notes" / "x.txt").read_text(encoding="utf-8")
    assert on_disk in store.read("x")
    # The journal's last word on the name is what the store holds.
    assert events[-1] == ("note_written", on_disk)
