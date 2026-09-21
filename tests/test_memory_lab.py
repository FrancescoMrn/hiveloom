"""The memory-lab demo, end to end and offline.

The harness ships its own scripted provider, so this is the one place the
three memory layers are exercised together through the CLI exactly as a user
runs them: a spilled read narrowed in place, a note that outlives compaction
and crosses a fork, a derived object handed to ``file_write`` by handle, and a
memory entry that reaches ``harness.yaml`` only through an applied proposal.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.logging.journal import read_events

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_LAB = REPO_ROOT / "harnesses" / "memory-lab"
TASK = "Investigate data/service.log and report the three facts."
EXPECTED = {
    "dominant_failure_code": "POOL_EXHAUSTED",
    "error_count": 84,
    "build_digest": "8F2C-77A1-DE30",
}


@pytest.fixture()
def lab(tmp_path: Path) -> Path:
    target = tmp_path / "memory-lab"
    shutil.copytree(MEMORY_LAB, target, ignore=shutil.ignore_patterns(".hiveloom", "out", ".venv"))
    return target


def _invoke(*args: str) -> dict:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def _events(trace_path: str) -> list[dict]:
    return read_events(trace_path)


def test_the_walkthrough_answers_from_l2_state(lab: Path):
    run = _invoke("run", str(lab), "--input-text", TASK)
    assert run["status"] == "success"
    assert json.loads(run["output"]) == EXPECTED

    events = _events(run["trace_path"])
    spilled = [e["payload"] for e in events if e["type"] == "tool_spilled"]
    assert [s["name"] for s in spilled] == ["file_read", "transform_result"]
    # The derived object records where it came from and how.
    assert spilled[1]["derived_from"] == spilled[0]["handle"]
    assert spilled[1]["op"] == "grep"

    calls = [e["payload"] for e in events if e["type"] == "tool_call"]
    ops = [c["input"]["op"] for c in calls if c["name"] == "transform_result"]
    assert ops == ["count", "grep", "tail", "grep"]

    # file_write received the derived handle; the journal keeps the handle and
    # the file got the bytes.
    write = next(c for c in calls if c["name"] == "file_write")
    assert write["input"]["content"] == spilled[1]["handle"]
    written = (lab / "out" / "error-context.txt").read_text(encoding="utf-8")
    assert len(written.encode("utf-8")) == spilled[1]["bytes"]
    assert "84 matching line(s)" in written

    assert [e["payload"]["name"] for e in events if e["type"] == "note_written"] == ["findings"]
    systems = [e["payload"]["system"] for e in events if e["type"] == "context_system"]
    assert all("# Memory" in s for s in systems)
    assert "# Notes" not in systems[0]
    assert "- findings (80 bytes): error_count=84" in systems[-1]


def test_a_resumed_fork_inherits_the_note_through_the_cli(lab: Path):
    run = _invoke("run", str(lab), "--input-text", TASK)
    points = _invoke("fork", run["run_id"], "--list")["fork_points"]
    written_at = next(
        e["seq"] for e in _events(run["trace_path"]) if e["type"] == "note_written"
    )
    after_note = next(p["seq"] for p in points if p["seq"] > written_at)

    fork = _invoke("fork", run["run_id"], "--at", str(after_note), "--name", "probe")
    record = (Path(fork["directory"]) / "fork.yaml").read_text(encoding="utf-8")
    assert "notes_manifest:" in record and "name: findings" in record

    resumed = _invoke("run", fork["directory"], "--resume")
    assert resumed["status"] == "success"
    assert json.loads(resumed["output"]) == EXPECTED
    events = _events(resumed["trace_path"])
    inherited = next(e for e in events if e["type"] == "notes_inherited")
    assert inherited["payload"]["names"] == ["findings"]
    reads = [
        e["payload"]
        for e in events
        if e["type"] == "tool_result" and e["payload"].get("name") == "notes"
    ]
    assert reads and not reads[0]["is_error"]
    system = next(e["payload"]["system"] for e in events if e["type"] == "context_system")
    assert "- findings (80 bytes)" in system


def test_an_applied_proposal_is_the_only_way_memory_grows(lab: Path):
    before = _invoke("memory", "list", str(lab))
    assert [e["id"] for e in before["entries"]] == ["never-guess-an-omitted-byte"]

    proposed = _invoke(
        "evolve",
        str(lab),
        "--propose",
        "--model",
        "memory_lab/qa-evolver",
        "--note",
        "The build digest is only ever on the last SUMMARY line.",
    )
    assert proposed["status"] == "pending"
    change = proposed["gate"]["accepted"][0]
    assert change["path"] == "memory.entries.+"
    assert change["value"]["id"] == "digest-is-in-the-tail"
    # Queued, not applied.
    assert [e["id"] for e in _invoke("memory", "list", str(lab))["entries"]] == [
        "never-guess-an-omitted-byte"
    ]

    applied = _invoke("proposals", "apply", str(lab), proposed["id"], "--yes")
    result = applied.get("apply_result") or applied
    assert result["changed"] is True
    assert result["new_version_hash"] != result["old_version_hash"]
    assert [e["id"] for e in _invoke("memory", "list", str(lab))["entries"]] == [
        "never-guess-an-omitted-byte",
        "digest-is-in-the-tail",
    ]

    run = _invoke("run", str(lab), "--input-text", TASK)
    system = next(
        e["payload"]["system"] for e in _events(run["trace_path"]) if e["type"] == "context_system"
    )
    assert "[fact] The build digest is on the final SUMMARY line" in system


def test_the_executor_can_only_queue_a_lesson(lab: Path):
    run = _invoke("run", str(lab), "--input-text", TASK)
    assert run["status"] == "success"

    proposed = next(
        e["payload"] for e in _events(run["trace_path"]) if e["type"] == "memory_proposed"
    )
    assert proposed["outcome"] == "queued"
    assert proposed["id"] == "count-before-naming-a-code"

    queued = _invoke("proposals", "list", str(lab))["proposals"]
    executor = [p for p in queued if p["trigger"] == "executor"]
    assert [p["status"] for p in executor] == ["pending"]
    assert executor[0]["id"] == proposed["proposal_id"]
    assert executor[0]["proposal"]["yaml_changes"][0]["path"] == "memory.entries.+"

    # The run changed nothing; the spec is exactly what was checked in.
    assert [e["id"] for e in _invoke("memory", "list", str(lab))["entries"]] == [
        "never-guess-an-omitted-byte"
    ]
    assert (lab / "harness.yaml").read_bytes() == (MEMORY_LAB / "harness.yaml").read_bytes()

    # A second run proposes the same lesson and finds it already queued.
    again = _invoke("run", str(lab), "--input-text", TASK)
    repeat = next(
        e["payload"] for e in _events(again["trace_path"]) if e["type"] == "memory_proposed"
    )
    assert repeat["outcome"] == "already_pending"
    assert len([p for p in _invoke("proposals", "list", str(lab))["proposals"]
                if p["trigger"] == "executor"]) == 1
