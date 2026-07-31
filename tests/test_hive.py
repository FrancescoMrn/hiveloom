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


def test_failure_signatures_group_one_behaviour_across_different_inputs(tmp_path: Path):
    """The point of the grouping: a behaviour is systematic precisely when it
    recurs across *different* inputs, and validators interpolate the input into
    their feedback. Grouping on raw text therefore splits exactly the failures
    worth acting on into one group per input.
    """
    for i, title in enumerate(["Foo - Ars Technica", "Bar \\ Anthropic", "Baz | Blog"]):
        _write_trace(
            tmp_path,
            f"run_{i}",
            status="verify_failed",
            verifications=[(False, f"Title '{title}' does not appear on the live page.")],
        )
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        verdicts = hive.failure_signatures("h")["verdicts"]

    assert len(verdicts) == 1, "three inputs, one behaviour, one cluster"
    assert verdicts[0]["count"] == 3
    assert verdicts[0]["feedback"] == "Title <str> does not appear on the live page."
    # A representative original is kept so the evolver still sees a real case.
    assert "Ars Technica" in verdicts[0]["example"]


def test_failure_signatures_count_runs_not_verification_rows(tmp_path: Path):
    """One run failing two validators is one failing run, not two."""
    _write_trace(
        tmp_path,
        "run_multi",
        status="verify_failed",
        verifications=[
            (False, "Title 'A' does not appear."),
            (False, "Title 'B' does not appear."),
        ],
    )
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        verdicts = hive.failure_signatures("h")["verdicts"]

    assert verdicts[0]["count"] == 1


def test_failure_signatures_can_scope_to_one_version(tmp_path: Path):
    """Failures an earlier evolution already repaired must not keep driving the
    next proposal."""
    _write_trace(tmp_path, "old", version="v1", status="verify_failed",
                 verifications=[(False, "Title 'X' does not appear.")])
    _write_trace(tmp_path, "new", version="v2", status="verify_failed",
                 verifications=[(False, "headings are wrong")])
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        assert len(hive.failure_signatures("h")["verdicts"]) == 2
        scoped = hive.failure_signatures("h", version="v2")["verdicts"]

    assert [v["feedback"] for v in scoped] == ["headings are wrong"]


def test_normalize_feedback_is_generic_not_validator_specific():
    """It must not encode knowledge of any particular validator or harness."""
    from hiveloom.logging.hive import normalize_feedback

    assert normalize_feedback("saw ['a', 'b'] at line 12") == "saw <list> at line <num>"
    assert normalize_feedback('fetch of "https://x.test/a" failed') == "fetch of <str> failed"
    assert normalize_feedback("value  spread\n  over lines") == "value spread over lines"


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


def test_failure_count_with_and_without_since(tmp_path: Path):
    _write_trace(tmp_path, "run_old", status="verify_failed")
    recent = _write_trace(tmp_path, "run_recent", status="verify_failed")
    recent.write_text(recent.read_text().replace("2026-01-01", "2026-01-31"))
    _write_trace(tmp_path, "run_ok", status="success")

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(tmp_path)
        assert hive.failure_count("h") == 2
        assert hive.failure_count("h", since="2026-01-15T00:00:00+00:00") == 1
        assert hive.failure_count("h", since="2027-01-01T00:00:00+00:00") == 0


def _proposal_row(**overrides) -> dict:
    row = {
        "id": "prop_aaaaaaaaaaaaaaaa",
        "harness_name": "demo",
        "spec_version_hash": "v1",
        "dedup_key": "abc123",
        "status": "pending",
        "trigger": "manual",
        "rationale": "r",
        "proposal_json": "{}",
        "gate_json": "{}",
        "apply_result_json": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "resolved_at": None,
    }
    row.update(overrides)
    return row


def test_insert_proposal_dedups_via_the_partial_unique_index(tmp_path: Path):
    """The (harness, spec_version, dedup_key) WHERE status='pending' index is
    the dedup mechanism: a colliding insert returns the existing row instead
    of raising, even below the proposals module's own pre-check."""
    with Hive(tmp_path / "hive.db") as hive:
        first = hive.insert_proposal(_proposal_row())
        second = hive.insert_proposal(_proposal_row(id="prop_bbbbbbbbbbbbbbbb"))
        assert second["id"] == first["id"] == "prop_aaaaaaaaaaaaaaaa"
        assert len(hive.list_proposals(harness_name="demo")) == 1
        assert hive._conn.in_transaction is False


def test_insert_proposal_reopens_slot_after_resolution(tmp_path: Path):
    """A resolved (applied/rejected) proposal no longer occupies the dedup slot."""
    with Hive(tmp_path / "hive.db") as hive:
        first = hive.insert_proposal(_proposal_row())
        hive.update_proposal(
            first["id"], status="rejected", resolved_at="2026-01-02T00:00:00+00:00"
        )
        second = hive.insert_proposal(_proposal_row(id="prop_bbbbbbbbbbbbbbbb"))
        assert second["id"] == "prop_bbbbbbbbbbbbbbbb"
        assert hive.find_pending_proposal("demo", "v1", "abc123")["id"] == second["id"]


def test_release_proposal_claim_survives_a_dedup_collision(tmp_path: Path):
    """Releasing a claim must never raise — it runs inside a failed apply's
    exception handler, so raising would mask the real error.

    The dedup index is partial on status='pending', so a claimed proposal's
    slot is momentarily free and a concurrent create can queue a fresh row for
    the same cluster. Restoring the claimed one then collides on that index.
    """
    with Hive(tmp_path / "hive.db") as hive:
        claimed = hive.insert_proposal(_proposal_row())
        assert hive.claim_pending_proposal(claimed["id"]) is True
        # The freed slot lets a concurrent auto-propose queue the same cluster.
        rival = hive.insert_proposal(_proposal_row(id="prop_bbbbbbbbbbbbbbbb"))
        assert rival["id"] == "prop_bbbbbbbbbbbbbbbb"

        hive.release_proposal_claim(claimed["id"])  # must not raise

        assert hive._conn.in_transaction is False
        assert hive.get_proposal(claimed["id"])["status"] == "applying"
        assert hive.find_pending_proposal("demo", "v1", "abc123")["id"] == rival["id"]


def test_last_auto_proposal_at_filters_and_limits_in_sql(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        hive.insert_proposal(
            _proposal_row(
                id="prop_manual",
                dedup_key="manual",
                created_at="2026-01-03T00:00:00+00:00",
            )
        )
        hive.insert_proposal(
            _proposal_row(
                id="prop_auto_old",
                dedup_key="auto-old",
                trigger="auto",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        hive.insert_proposal(
            _proposal_row(
                id="prop_auto_new",
                dedup_key="auto-new",
                trigger="auto",
                created_at="2026-01-02T00:00:00+00:00",
            )
        )

        statements: list[str] = []
        hive._conn.set_trace_callback(statements.append)
        created_at = hive.last_auto_proposal_at("demo")
        hive._conn.set_trace_callback(None)

    assert created_at == "2026-01-02T00:00:00+00:00"
    query = next(statement for statement in statements if statement.startswith("SELECT"))
    assert "SELECT created_at FROM proposals" in query
    assert "trigger='auto'" in query
    assert "LIMIT 1" in query
    assert "proposal_json" not in query


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
