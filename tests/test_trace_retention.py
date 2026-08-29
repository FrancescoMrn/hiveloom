"""Structured trace redaction and deletion-safe raw-journal retention."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import construct, runner
from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError
from hiveloom.logging.hive import Hive
from hiveloom.logging.retention import (
    TRACE_ROOT_MARKER,
    apply_trace_retention,
    plan_trace_retention,
    prune_trace_root,
)
from hiveloom.logging.trace import TraceWriter
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import dump_spec, load_spec
from hiveloom.spec.schema import LoggingConfig, RetentionConfig

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"
_VALID = json.dumps({"title": "T", "summary": "short.", "key_points": ["a"]})
cli = CliRunner()


def _journal(
    root: Path,
    run_id: str,
    *,
    body: str = "x",
    modified_at: datetime | None = None,
) -> Path:
    writer = TraceWriter(root, run_id, "h", "v1", harness_id="hl-retention")
    writer.emit("run_started", input="synthetic")
    writer.emit("model_response", text=body)
    writer.emit(
        "run_finished",
        status="success",
        turns=1,
        cost_usd=0.0,
        duration_seconds=0.1,
    )
    if modified_at is not None:
        timestamp = modified_at.timestamp()
        os.utime(writer.path, (timestamp, timestamp))
    return writer.path


def test_structured_redaction_precedes_persistence_streaming_and_hive(tmp_path: Path):
    streamed = []
    writer = TraceWriter(
        tmp_path / "traces",
        "run_redacted",
        "h",
        "v1",
        harness_id="hl-redacted",
        redact_keys=["email", "api_key"],
        redact_paths=["input.result.candidates[*].cv_text"],
        redact_patterns=[r"\+39\s*\d+"],
        on_event=streamed.append,
    )
    payload = {
        "email": "person@example.test",
        "nested": {"API_KEY": "secret-token"},
        "result": {
            "candidates": [
                {"talent_id": "t1", "cv_text": "private CV evidence"},
                {"talent_id": "t2", "cv_text": "second private CV"},
            ]
        },
        "phone": "+39 1234567",
    }
    writer.emit("run_started", input=payload)
    writer.emit("run_finished", status="success")

    raw = writer.path.read_text(encoding="utf-8")
    persisted = [json.loads(line) for line in raw.splitlines()]
    assert payload["email"] == "person@example.test", "redaction must not mutate caller data"
    for private in (
        "person@example.test",
        "secret-token",
        "private CV evidence",
        "second private CV",
        "+39 1234567",
    ):
        assert private not in raw
        assert private not in json.dumps([event.model_dump() for event in streamed])
    redacted = persisted[0]["payload"]["input"]
    assert redacted["email"] == "[REDACTED]"
    assert redacted["nested"]["API_KEY"] == "[REDACTED]"
    assert [item["cv_text"] for item in redacted["result"]["candidates"]] == [
        "[REDACTED]",
        "[REDACTED]",
    ]

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(writer.path)
        stored = json.dumps(hive.get_run("run_redacted"))
    assert "person@example.test" not in stored
    assert "private CV evidence" not in stored


def test_logging_schema_accepts_legacy_patterns_and_rejects_bad_rules():
    legacy = LoggingConfig(redact=["api[_-]?key"])
    assert legacy.redact.patterns == ["api[_-]?key"]
    assert "redact:\n  - api[_-]?key" in dump_spec(
        load_spec(EXAMPLE_HARNESS)
    )

    with pytest.raises(ValueError, match="invalid redaction path"):
        LoggingConfig(redact={"paths": ["result..cv_text"]})
    with pytest.raises(ValueError, match="invalid redaction regex"):
        LoggingConfig(redact={"patterns": ["["]})
    with pytest.raises(ValueError, match="must set"):
        RetentionConfig()
    with pytest.raises(ValueError, match="dedicated trace_dir"):
        LoggingConfig(trace_dir=".", retention={"days": 1})


def test_retention_dry_run_combines_age_count_and_bytes_without_mutation(tmp_path: Path):
    root = tmp_path / "traces"
    now = datetime(2026, 8, 29, tzinfo=UTC)
    oldest = _journal(root, "run_old", modified_at=now - timedelta(days=40))
    middle = _journal(root, "run_middle", body="m" * 500, modified_at=now - timedelta(days=2))
    newest = _journal(root, "run_new", body="n" * 500, modified_at=now - timedelta(days=1))
    before = {path: path.read_bytes() for path in (oldest, middle, newest)}
    policy = RetentionConfig(days=30, max_runs=1, max_bytes=newest.stat().st_size)

    plan = prune_trace_root(root, policy, dry_run=True, now=now)

    assert plan.applied is False
    assert {item["run_id"] for item in plan.to_dict()["selected"]} == {
        "run_old",
        "run_middle",
    }
    assert plan.to_dict()["limits_satisfied"] is True
    assert {path: path.read_bytes() for path in before} == before


def test_retention_preserves_current_trace_and_reports_unsatisfied_limit(tmp_path: Path):
    root = tmp_path / "traces"
    current = _journal(root, "run_current", body="large" * 500)
    plan = plan_trace_retention(
        root,
        RetentionConfig(max_bytes=1),
        preserve=[current],
    )

    assert plan.to_dict()["selected_runs"] == 0
    assert plan.to_dict()["limits_satisfied"] is False
    assert current.exists()


def test_retention_clears_only_matching_hive_path_and_open_reader_finishes(tmp_path: Path):
    root = tmp_path / "traces"
    now = datetime.now(UTC)
    old = _journal(root, "run_old", modified_at=now - timedelta(minutes=1))
    keep = _journal(root, "run_keep", modified_at=now)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(old)
        hive.ingest_trace_file(keep)
        with old.open("r", encoding="utf-8") as reader:
            plan = prune_trace_root(root, RetentionConfig(max_runs=1), hive=hive)
            assert reader.readline(), "an existing reader keeps its file descriptor"
        old_run = hive.get_run("run_old")
        kept_run = hive.get_run("run_keep")

    assert plan.applied is True
    assert not old.exists()
    assert keep.exists()
    assert old_run["trace_path"] is None
    assert old_run["trace_pruned_at"] is not None
    assert kept_run["trace_path"] == str(keep)


def test_retention_does_not_clear_a_newer_durable_hive_reference(tmp_path: Path):
    root = tmp_path / "traces"
    old_copy = _journal(root, "run_copied")
    durable_root = tmp_path / "durable"
    durable_root.mkdir()
    durable_copy = durable_root / old_copy.name
    shutil.copy2(old_copy, durable_copy)

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(old_copy)
        hive.ingest_trace_file(durable_copy)
        prune_trace_root(root, RetentionConfig(max_bytes=1), hive=hive)
        run = hive.get_run("run_copied")

    assert not old_copy.exists()
    assert durable_copy.exists()
    assert run["trace_path"] == str(durable_copy)
    assert run["trace_pruned_at"] is None


def test_retention_restores_renamed_files_when_hive_update_fails(tmp_path: Path):
    root = tmp_path / "traces"
    trace = _journal(root, "run_rollback")
    plan = plan_trace_retention(root, RetentionConfig(max_bytes=1))

    class BrokenHive:
        def mark_traces_pruned(self, *_args, **_kwargs):
            raise RuntimeError("synthetic database failure")

    with pytest.raises(RuntimeError, match="synthetic database failure"):
        apply_trace_retention(plan, hive=BrokenHive())

    assert trace.exists()
    assert not list(root.glob(".run_rollback.jsonl.pruning-*"))


def test_retention_refuses_unmanaged_roots_symlinks_and_outside_preserves(tmp_path: Path):
    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    with pytest.raises(SpecError, match="not a managed"):
        plan_trace_retention(unmanaged, RetentionConfig(max_runs=1))

    root = tmp_path / "managed"
    _journal(root, "run_safe")
    outside = _journal(tmp_path / "outside", "run_outside")
    with pytest.raises(SpecError, match="outside the managed root"):
        plan_trace_retention(root, RetentionConfig(max_runs=1), preserve=[outside])

    link = root / "run_link.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    with pytest.raises(SpecError, match="symlinked"):
        plan_trace_retention(root, RetentionConfig(max_runs=1))


def test_retention_cli_dry_run_then_apply_updates_hive(monkeypatch, tmp_path: Path):
    harness = tmp_path / "harness"
    shutil.copytree(EXAMPLE_HARNESS, harness, ignore=shutil.ignore_patterns(".hiveloom"))
    construct.set_value(harness, "logging.retention", {"max_runs": 1})
    spec = load_spec(harness)
    root = (harness / spec.logging.trace_dir).resolve()
    now = datetime.now(UTC)
    old = _journal(root, "run_cli_old", modified_at=now - timedelta(minutes=1))
    keep = _journal(root, "run_cli_keep", modified_at=now)
    db = tmp_path / "hive.db"
    monkeypatch.setenv("HIVELOOM_DB", str(db))
    with Hive(db) as hive:
        hive.ingest_trace_file(old)
        hive.ingest_trace_file(keep)

    preview = cli.invoke(app, ["traces", "prune", str(harness), "--dry-run", "--json"])
    assert preview.exit_code == ExitCode.OK
    assert json.loads(preview.stdout)["selected_runs"] == 1
    assert old.exists() and keep.exists()

    refused = cli.invoke(app, ["traces", "prune", str(harness), "--json"])
    applied = cli.invoke(app, ["traces", "prune", str(harness), "--yes", "--json"])

    assert refused.exit_code == ExitCode.SPEC_ERROR
    assert applied.exit_code == ExitCode.OK
    assert json.loads(applied.stdout)["applied"] is True
    assert not old.exists() and keep.exists()
    with Hive(db) as hive:
        assert hive.get_run("run_cli_old")["trace_path"] is None
    traced = cli.invoke(app, ["trace", "run_cli_old", "--json"])
    verified = cli.invoke(app, ["trace", "run_cli_old", "--verify", "--json"])
    assert traced.exit_code == ExitCode.OK
    assert json.loads(traced.stdout)["events"] == []
    assert json.loads(traced.stdout)["run"]["trace_pruned_at"] is not None
    assert verified.exit_code == ExitCode.SPEC_ERROR
    assert "pruned at" in json.loads(verified.stdout)["error"]


def test_run_applies_retention_but_never_prunes_its_own_result(tmp_path: Path):
    harness = tmp_path / "harness"
    shutil.copytree(EXAMPLE_HARNESS, harness, ignore=shutil.ignore_patterns(".hiveloom"))
    (harness / "notes.txt").write_text("synthetic notes " * 30)
    construct.set_value(harness, "logging.retention", {"max_runs": 1})
    db = tmp_path / "hive.db"

    first = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider(
            [tool_response("file_read", {"path": "notes.txt"}), text_response(_VALID)]
        ),
        hive_path=db,
    )
    second = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider(
            [tool_response("file_read", {"path": "notes.txt"}), text_response(_VALID)]
        ),
        hive_path=db,
    )

    assert not Path(first.trace_path).exists()
    assert Path(second.trace_path).exists()
    assert second.execution.trace_path == second.trace_path
    with Hive(db) as hive:
        assert hive.get_run(first.run_id)["trace_path"] is None
        assert hive.get_run(second.run_id)["trace_path"] == second.trace_path


def test_trace_root_marker_contains_no_run_data(tmp_path: Path):
    root = tmp_path / "traces"
    _journal(root, "run_marker")
    marker = root / TRACE_ROOT_MARKER
    assert marker.read_text(encoding="utf-8") == "hiveloom-trace-root-v1\n"
