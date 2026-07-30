"""Direct unit tests for runner._maybe_auto_propose's threshold/cooldown guards.

Exercises the guard chain against a hand-seeded Hive rather than driving full
end-to-end runs per permutation. Timestamps are fully controlled (never real
elapsed wall-clock time) so these tests are deterministic regardless of how
fast they execute.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Reuse the harness-builder helper from the run-integration tests (tests dir
# is on sys.path), mirroring the existing cross-file test-helper precedent
# (e.g. test_evolve.py importing _write_trace from test_hive.py).
from test_run_integration import _auto_harness

from hiveloom.evolve.proposals import create_proposal
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.loop.agent_loop import RunResult
from hiveloom.runner import _maybe_auto_propose
from hiveloom.spec.loader import load_spec

_PAYLOAD = json.dumps(
    {"rationale": "seed", "yaml_changes": [{"path": "loop.max_turns", "value": 10}]}
)


def _harness(tmp_path: Path, *, min_failures: int = 1, cooldown_hours: float = 24.0) -> Path:
    return _auto_harness(
        tmp_path, name="demo", min_failures=min_failures, cooldown_hours=cooldown_hours
    )


def _write_failure(hive_path: Path, tmp_path: Path, run_id: str, feedback: str, at: str) -> None:
    """Ingest a single minimal failing run with a precisely controlled timestamp."""
    events = [
        {"run_id": run_id, "harness_name": "demo", "harness_version_hash": "v1", "seq": 0,
         "timestamp": at, "type": "run_started", "payload": {}},
        {"run_id": run_id, "harness_name": "demo", "harness_version_hash": "v1", "seq": 1,
         "timestamp": at, "type": "verification_result",
         "payload": {"verifier": "v", "passed": False, "feedback": feedback}},
        {"run_id": run_id, "harness_name": "demo", "harness_version_hash": "v1", "seq": 2,
         "timestamp": at, "type": "run_finished",
         "payload": {"status": "verify_failed", "turns": 1, "cost_usd": 0.01,
                     "duration_seconds": 0.1, "reason": ""}},
    ]
    trace_path = tmp_path / f"{run_id}.jsonl"
    trace_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    with Hive(hive_path) as hive:
        hive.ingest_trace_file(trace_path)


def _seed_auto_proposal(hive_path: Path, harness: Path, *, feedback: str, created_at: str) -> str:
    """Insert an auto-triggered proposal with a precisely controlled created_at."""
    spec = load_spec(harness)
    from hiveloom.evolve.analyzer import FailureCluster, FailureReport

    report = FailureReport(
        harness_name=spec.name, total_runs=1, success_rate=0.0,
        clusters=[FailureCluster(kind="verdict", signature=feedback, count=1)],
    )
    with Hive(hive_path) as hive:
        record = create_proposal(
            hive, spec, harness, report, FakeStrongModel([_PAYLOAD]), trigger="auto"
        )
        hive.update_proposal(record.id, created_at=created_at)
    return record.id


def _fail_result() -> RunResult:
    return RunResult(status="verify_failed", run_id="run_current")


def test_min_failures_not_met_skips(tmp_path: Path):
    harness = _harness(tmp_path, min_failures=5)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    for i in range(3):  # below the threshold of 5
        _write_failure(hive_path, tmp_path, f"run_{i}", "same issue", now.isoformat())

    model = FakeStrongModel([])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        assert hive.list_proposals(harness_name="demo") == []
    assert model.prompts == []


def test_cooldown_not_expired_skips(tmp_path: Path):
    harness = _harness(tmp_path, min_failures=1, cooldown_hours=24.0)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)

    _seed_auto_proposal(
        hive_path, harness, feedback="old issue",
        created_at=(now - timedelta(hours=1)).isoformat(),  # only 1h ago, cooldown is 24h
    )
    # Plenty of NEW failures (different signature) since the last auto-proposal —
    # min_failures is satisfied; only the cooldown should be the blocker.
    now_iso = now.isoformat()
    for i in range(5):
        _write_failure(hive_path, tmp_path, f"run_{i}", "brand new different issue", now_iso)

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        proposals = hive.list_proposals(harness_name="demo")
    assert len(proposals) == 1  # still just the seeded one
    assert model.prompts == []  # cooldown blocked before any model call


def test_cooldown_expired_proposes(tmp_path: Path):
    harness = _harness(tmp_path, min_failures=1, cooldown_hours=1.0)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)

    _seed_auto_proposal(
        hive_path, harness, feedback="old issue",
        created_at=(now - timedelta(hours=2)).isoformat(),  # 2h ago, cooldown is 1h — expired
    )
    now_iso = now.isoformat()
    for i in range(5):
        _write_failure(hive_path, tmp_path, f"run_{i}", "brand new different issue", now_iso)

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        proposals = hive.list_proposals(harness_name="demo")
    assert len(proposals) == 2  # the seeded one plus a fresh one
    assert len(model.prompts) == 1  # the model was called this time


def test_successful_run_result_short_circuits(tmp_path: Path):
    harness = _harness(tmp_path, min_failures=1)
    hive_path = tmp_path / "hive.db"
    model = FakeStrongModel([])

    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="success", run_id="run_ok"),
        hive_path,
        strong_model=model,
    )

    with Hive(hive_path) as hive:
        assert hive.list_proposals(harness_name="demo") == []
    assert model.prompts == []


def test_schema_invalid_auto_proposal_does_not_burn_dedup_or_cooldown(tmp_path: Path):
    harness = _harness(tmp_path, min_failures=1, cooldown_hours=24.0)
    hive_path = tmp_path / "hive.db"
    _write_failure(
        hive_path,
        tmp_path,
        "run_bad",
        "same issue",
        datetime.now(UTC).isoformat(),
    )
    invalid_payload = json.dumps(
        {
            "rationale": "switch policy",
            "yaml_changes": [{"path": "loop.policy", "value": "sequential_steps"}],
        }
    )
    model = FakeStrongModel([invalid_payload, invalid_payload])
    spec = load_spec(harness)

    _maybe_auto_propose(spec, harness, _fail_result(), hive_path, strong_model=model)
    _maybe_auto_propose(spec, harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        assert hive.list_proposals(harness_name="demo") == []
    assert len(model.prompts) == 2
