"""Tests for the Hive: ingestion, idempotency, and queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hiveloom.logging.hive import Hive, default_db_path


def _write_trace(
    path: Path,
    run_id: str,
    *,
    name: str = "h",
    version: str = "v1",
    status: str = "success",
    verifications: list[tuple[bool, str]] | None = None,
    guardrails: list[tuple[str, str, str]] | None = None,
) -> Path:
    """Write a minimal but well-formed run trace JSONL file."""
    seq = 0

    def env(etype: str, payload: dict, ts: str) -> str:
        nonlocal seq
        event = {
            "run_id": run_id,
            "harness_name": name,
            "harness_version_hash": version,
            "seq": seq,
            "timestamp": ts,
            "type": etype,
            "payload": payload,
        }
        seq += 1
        return json.dumps(event)

    lines = [env("run_started", {"input": "x"}, "2026-01-01T00:00:00+00:00")]
    for passed, feedback in verifications or []:
        lines.append(
            env("verification_result", {"verifier": "v", "passed": passed, "feedback": feedback},
                "2026-01-01T00:00:01+00:00")
        )
    for guardrail, kind, reason in guardrails or []:
        lines.append(
            env("guardrail_triggered", {"guardrail": guardrail, "kind": kind, "reason": reason},
                "2026-01-01T00:00:01+00:00")
        )
    lines.append(
        env(
            "run_finished",
            {"status": status, "turns": 3, "cost_usd": 0.02, "duration_seconds": 1.5, "reason": ""},
            "2026-01-01T00:00:02+00:00",
        )
    )
    file_path = path / f"{run_id}.jsonl"
    file_path.write_text("\n".join(lines) + "\n")
    return file_path


def test_default_db_path_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVELOOM_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"


def test_ingest_and_get_run(tmp_path: Path):
    trace = _write_trace(tmp_path, "run_a", verifications=[(True, "")])
    with Hive(tmp_path / "hive.db") as hive:
        ingested = hive.ingest_trace_file(trace)
        assert ingested == ["run_a"]
        run = hive.get_run("run_a")
        assert run["status"] == "success"
        assert run["turns"] == 3
        assert run["harness_version_hash"] == "v1"


def test_ingestion_is_idempotent(tmp_path: Path):
    trace = _write_trace(tmp_path, "run_a")
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(trace)
        hive.ingest_trace_file(trace)  # second time must not duplicate
        stats = hive.version_stats("h")
        assert len(stats) == 1
        assert stats[0]["runs"] == 1


def test_version_stats_buckets_by_hash(tmp_path: Path):
    _write_trace(tmp_path, "run_a", version="v1", status="success")
    _write_trace(tmp_path, "run_b", version="v1", status="verify_failed")
    _write_trace(tmp_path, "run_c", version="v2", status="success")
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        by_version = {s["version"]: s for s in hive.version_stats("h")}
        assert by_version["v1"]["runs"] == 2
        assert by_version["v1"]["success_rate"] == 0.5
        assert by_version["v2"]["success_rate"] == 1.0


def test_failure_signatures(tmp_path: Path):
    vf = [(False, "not valid JSON")]
    _write_trace(tmp_path, "run_a", status="verify_failed", verifications=vf)
    _write_trace(tmp_path, "run_b", status="verify_failed", verifications=vf)
    _write_trace(
        tmp_path, "run_c", status="guardrail_halt", guardrails=[("max_cost_usd", "halt", "over")]
    )
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        sigs = hive.failure_signatures("h")
        assert sigs["verdicts"][0]["feedback"] == "not valid JSON"
        assert sigs["verdicts"][0]["count"] == 2
        assert sigs["guardrails"][0]["guardrail"] == "max_cost_usd"
        statuses = {s["status"]: s["count"] for s in sigs["statuses"]}
        assert statuses["verify_failed"] == 2


def test_recent_failures_with_feedback(tmp_path: Path):
    _write_trace(tmp_path, "run_ok", status="success")
    _write_trace(
        tmp_path, "run_bad", status="verify_failed", verifications=[(False, "fix the totals")]
    )
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        failures = hive.recent_failures("h", 5)
        assert len(failures) == 1
        assert failures[0]["run_id"] == "run_bad"
        assert failures[0]["failed_verifications"][0]["feedback"] == "fix the totals"


def test_get_run_unknown_returns_none(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        assert hive.get_run("nope") is None


def test_ingest_skips_non_run_events(tmp_path: Path):
    # A construction log has no run_id envelope; it must be ignored.
    (tmp_path / "construction.jsonl").write_text(
        json.dumps({"type": "construction_event", "command": "init"}) + "\n"
    )
    with Hive(tmp_path / "hive.db") as hive:
        assert hive.ingest_trace_file(tmp_path / "construction.jsonl") == []


def test_hive_uses_wal_and_can_prune_completed_runs(tmp_path: Path):
    old = _write_trace(tmp_path, "run_old")
    recent = _write_trace(tmp_path, "run_recent")
    # The helper uses a fixed timestamp; update the recent trace for this case.
    recent.write_text(recent.read_text().replace("2026-01-01", "2026-01-31"))

    with Hive(tmp_path / "hive.db") as hive:
        assert hive._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert hive._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        hive.ingest_trace_file(old)
        hive.ingest_trace_file(recent)
        removed = hive.prune_runs(10, now=datetime(2026, 2, 1, tzinfo=UTC))
        assert removed == 1
        assert hive.get_run("run_old") is None
        assert hive.get_run("run_recent") is not None
