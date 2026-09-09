"""Friction indexing and CLI queries over already-redacted run journals."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.errors import ExitCode
from hiveloom.logging.hive import Hive

cli = CliRunner()


def _write_events(
    path: Path,
    run_id: str,
    events: list[tuple[str, dict]],
    *,
    status: str = "success",
    started_at: str = "2026-01-01T00:00:00+00:00",
    finished_at: str = "2026-01-01T00:00:10+00:00",
    requested_model: str = "requested-model",
    effective_model: str = "effective-model",
) -> Path:
    rows: list[dict] = []

    def add(event_type: str, payload: dict, timestamp: str) -> None:
        rows.append(
            {
                "run_id": run_id,
                "harness_name": "h",
                "harness_id": "hl-friction",
                "harness_version_hash": "v1",
                "seq": len(rows),
                "timestamp": timestamp,
                "type": event_type,
                "payload": payload,
            }
        )

    add("run_started", {"input": "private task"}, started_at)
    for offset, (event_type, payload) in enumerate(events, start=1):
        add(event_type, payload, f"2026-01-01T00:00:{offset:02d}+00:00")
    add(
        "run_finished",
        {
            "status": status,
            "turns": 2,
            "cost_usd": 0.01,
            "duration_seconds": 10,
            "reason": "",
            "execution": {
                "requested_provider": "fixture",
                "requested_model": requested_model,
                "effective_provider": "fixture",
                "effective_model": effective_model,
                "execution_fingerprint": f"fingerprint-{run_id}",
            },
        },
        finished_at,
    )
    trace = path / f"{run_id}.jsonl"
    trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return trace


def test_recovered_verification_failure_remains_visible_after_success(tmp_path: Path):
    trace = _write_events(
        tmp_path,
        "recovered",
        [
            ("model_call", {"turn": 0, "phase": "act"}),
            ("model_response", {"turn": 1, "phase": "act"}),
            (
                "verification_result",
                {
                    "verifier": "output_schema",
                    "passed": False,
                    "feedback": "talent id 'private-123' was invalid",
                },
            ),
            ("model_call", {"turn": 1, "phase": "act"}),
            ("model_response", {"turn": 2, "phase": "act"}),
            (
                "verification_result",
                {"verifier": "output_schema", "passed": True, "feedback": ""},
            ),
        ],
    )

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        [event] = hive.list_friction("hl-friction")
        run = hive.get_run("recovered")

    assert event["category"] == "output_validation"
    assert event["phase"] == "verification"
    assert event["attempt"] == 1
    assert event["recovered"] is True
    assert "private-123" not in event["summary"]
    assert run["friction"][0]["fingerprint"] == event["fingerprint"]


def test_index_covers_trace_friction_and_is_idempotent(tmp_path: Path):
    trace = _write_events(
        tmp_path,
        "friction-run",
        [
            ("model_call", {"turn": 0, "phase": "act"}),
            ("model_response", {"turn": 1, "phase": "act"}),
            ("tool_retry", {"id": "c1", "name": "search"}),
            (
                "tool_result",
                {
                    "id": "c1",
                    "name": "search",
                    "is_error": True,
                    "content": "candidate private@example.com was rejected",
                },
            ),
            ("tool_truncated", {"id": "c2", "name": "rank"}),
            ("context_compaction", {"method": "truncate_oldest", "dropped": 2}),
            ("context_overflow_recovery", {"phase": "act", "error": "window 123"}),
            ("user_steer", {"content": "the private operator message"}),
            (
                "guardrail_triggered",
                {"guardrail": "shell_allowlist", "kind": "block", "reason": "not allowed"},
            ),
        ],
    )

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        hive.ingest_trace_file(trace)
        events = hive.list_friction("hl-friction", limit=100)
        summary = hive.friction_summary("hl-friction")

    assert summary["events"] == len(events) == 7
    assert summary["runs"] == 1
    assert summary["recovered"] == 7
    assert {event["category"] for event in events} == {
        "retry",
        "tool_error",
        "context_compaction",
        "user_steer",
        "guardrail_block",
    }
    serialized = json.dumps(events)
    assert "private@example.com" not in serialized
    assert "private operator message" not in serialized


def test_unrecovered_categories_and_filters_use_run_provenance(tmp_path: Path):
    failed = _write_events(
        tmp_path,
        "failed",
        [
            ("model_call", {"turn": 0, "phase": "act"}),
            ("model_response", {"turn": 1, "phase": "act"}),
            (
                "verification_result",
                {"verifier": "grounding", "passed": False, "feedback": "missing id 44"},
            ),
        ],
        status="max_turns",
        requested_model="model-a",
        effective_model="model-b",
    )
    provider_error = _write_events(
        tmp_path,
        "provider-error",
        [("model_call", {"turn": 0, "phase": "act"})],
        status="error",
        requested_model="model-c",
        effective_model="",
    )

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(failed)
        hive.ingest_trace_file(provider_error)
        grounding = hive.list_friction(
            "hl-friction",
            category="verifier_failure",
            component="grounding",
            recovered=False,
            model="model-b",
            since="2026-01-01T00:00:00+00:00",
            until="2026-01-01T00:01:00+00:00",
        )
        requested = hive.list_friction("hl-friction", model="model-a", limit=100)
        provider = hive.list_friction("hl-friction", category="provider_error")

    assert len(grounding) == 1
    assert grounding[0]["model"] == "model-b"
    assert {event["category"] for event in requested} == {
        "verifier_failure",
        "loop_limit",
    }
    assert len(provider) == 1
    assert provider[0]["component"] == "model-c"
    assert provider[0]["recovered"] is False


def test_hive_migrates_old_runs_and_prunes_friction(tmp_path: Path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, harness_name TEXT, harness_id TEXT, "
        "harness_key TEXT, harness_version_hash TEXT, status TEXT, turns INTEGER, "
        "cost_usd REAL, duration_seconds REAL, started_at TEXT, finished_at TEXT, reason TEXT, "
        "trace_path TEXT, parent_run_id TEXT, forked_at_seq INTEGER, model_path TEXT, task TEXT)"
    )
    connection.commit()
    connection.close()
    trace = _write_events(
        tmp_path,
        "old",
        [("tool_retry", {"id": "c1", "name": "search"})],
        finished_at="2025-01-01T00:00:10+00:00",
    )

    with Hive(db) as hive:
        columns = {row["name"] for row in hive._conn.execute("PRAGMA table_info(runs)")}
        hive.ingest_trace_file(trace)
        assert hive.friction_summary("hl-friction")["events"] == 1
        removed = hive.prune_runs(30, now=datetime(2026, 1, 1, tzinfo=UTC))
        remaining = hive._conn.execute("SELECT COUNT(*) FROM friction_events").fetchone()[0]

    assert {"effective_model", "execution_fingerprint"} <= columns
    assert removed == 1
    assert remaining == 0


def test_friction_cli_and_stats_json(monkeypatch, tmp_path: Path):
    db = tmp_path / "hive.db"
    monkeypatch.setenv("HIVELOOM_DB", str(db))
    trace = _write_events(
        tmp_path,
        "cli-run",
        [("tool_retry", {"id": "c1", "name": "search"})],
    )
    with Hive(db) as hive:
        hive.ingest_trace_file(trace)

    listed = cli.invoke(
        app,
        [
            "friction",
            "list",
            "hl-friction",
            "--category",
            "retry",
            "--recovered",
            "true",
            "--json",
        ],
    )
    stats = cli.invoke(app, ["stats", "hl-friction", "--include-friction", "--json"])
    invalid = cli.invoke(
        app, ["friction", "list", "hl-friction", "--recovered", "maybe", "--json"]
    )

    assert listed.exit_code == ExitCode.OK
    assert json.loads(listed.stdout)["friction"][0]["component"] == "search"
    assert stats.exit_code == ExitCode.OK
    assert json.loads(stats.stdout)["friction"]["events"] == 1
    assert invalid.exit_code == ExitCode.SPEC_ERROR
    assert json.loads(invalid.stdout)["ok"] is False
