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

from hiveloom import construct
from hiveloom.evolve.proposals import create_proposal
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.loop.agent_loop import RunResult
from hiveloom.runner import _maybe_auto_propose
from hiveloom.spec.loader import load_spec

_NEW_ISSUE = "brand new different issue"  # a signature the seeded proposal has not seen

_PAYLOAD = json.dumps(
    {"rationale": "seed", "yaml_changes": [{"path": "loop.max_turns", "value": 10}]}
)


def _harness(tmp_path: Path, *, min_failures: int = 1, cooldown_hours: float = 24.0) -> Path:
    return _auto_harness(
        tmp_path, name="demo", min_failures=min_failures, cooldown_hours=cooldown_hours
    )


def _write_failure(
    hive_path: Path, tmp_path: Path, harness: Path, run_id: str, feedback: str, at: str
) -> None:
    """Ingest a single minimal failing run with a precisely controlled timestamp.

    The trace carries ``harness``'s live version hash, as a real run writes it:
    auto-propose counts and analyses failures of the current version only, so a
    hand-written hash would be invisible to the guard chain under test.
    """
    spec = load_spec(harness)
    version = spec_version_hash(spec, harness)
    events = [
        {"run_id": run_id, "harness_name": spec.name, "harness_id": spec.id,
         "harness_version_hash": version, "seq": 0,
         "timestamp": at, "type": "run_started", "payload": {}},
        {"run_id": run_id, "harness_name": spec.name, "harness_id": spec.id,
         "harness_version_hash": version, "seq": 1,
         "timestamp": at, "type": "verification_result",
         "payload": {"verifier": "v", "passed": False, "feedback": feedback}},
        {"run_id": run_id, "harness_name": spec.name, "harness_id": spec.id,
         "harness_version_hash": version, "seq": 2,
         "timestamp": at, "type": "run_finished",
         "payload": {"status": "verify_failed", "turns": 1, "cost_usd": 0.01,
                     "duration_seconds": 0.1, "reason": ""}},
    ]
    trace_path = tmp_path / f"{run_id}.jsonl"
    trace_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    with Hive(hive_path) as hive:
        hive.ingest_trace_file(trace_path)


def _write_friction(
    hive_path: Path,
    tmp_path: Path,
    harness: Path,
    run_id: str,
    at: str,
    *,
    category: str = "retry",
) -> None:
    """Ingest one successful run carrying a repeatable recovered incident."""
    spec = load_spec(harness)
    version = spec_version_hash(spec, harness)
    envelope = {
        "run_id": run_id,
        "harness_name": spec.name,
        "harness_id": spec.id,
        "harness_version_hash": version,
        "timestamp": at,
    }
    events = [{**envelope, "seq": 0, "type": "run_started", "payload": {}}]
    if category == "output_validation":
        events.extend(
            [
                {
                    **envelope,
                    "seq": 1,
                    "type": "verification_result",
                    "payload": {
                        "verifier": "output_schema",
                        "passed": False,
                        "feedback": "required field was missing",
                    },
                },
                {
                    **envelope,
                    "seq": 2,
                    "type": "verification_result",
                    "payload": {
                        "verifier": "output_schema",
                        "passed": True,
                        "feedback": "",
                    },
                },
            ]
        )
    elif category == "retry":
        events.append(
            {
                **envelope,
                "seq": 1,
                "type": "tool_retry",
                "payload": {"name": "search", "id": "call"},
            }
        )
    events.append(
        {
            **envelope,
            "seq": len(events),
            "type": "run_finished",
            "payload": {
                "status": "success",
                "turns": 2,
                "cost_usd": 0.01,
                "duration_seconds": 0.1,
                "reason": "",
            },
        }
    )
    trace_path = tmp_path / f"{run_id}.jsonl"
    trace_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    with Hive(hive_path) as hive:
        hive.ingest_trace_file(trace_path)


def _set_friction_trigger(
    harness: Path,
    *,
    category: str = "retry",
    minimum_runs: int = 3,
    window: int = 10,
    cooldown_runs: int | None = None,
) -> None:
    construct.set_value(
        harness,
        "evolution.auto_propose.triggers",
        [
            {
                "kind": "repeated_friction",
                "category": category,
                "minimum_runs": minimum_runs,
                "window": window,
            }
        ],
    )
    if cooldown_runs is not None:
        construct.set_value(
            harness,
            "evolution.auto_propose.cooldown_runs",
            cooldown_runs,
        )


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
        _write_failure(hive_path, tmp_path, harness, f"run_{i}", "same issue", now.isoformat())

    model = FakeStrongModel([])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        assert hive.list_proposals(harness_name=load_spec(harness).identity) == []
    assert model.prompts == []


def test_earlier_versions_failures_do_not_open_the_gate(tmp_path: Path):
    """The gate counts what the report will carry. Counting pooled failures while
    analysing only the current version would pay for a strong-model call on an
    empty report."""
    harness = _harness(tmp_path, min_failures=1)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC).isoformat()
    for i in range(3):
        _write_failure(hive_path, tmp_path, harness, f"run_{i}", "same issue", now)
    construct.set_field(harness, "loop.max_turns", "9")  # new spec, new version hash

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        assert hive.list_proposals(harness_name=load_spec(harness).identity) == []
    assert model.prompts == []  # no paid call


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
        _write_failure(hive_path, tmp_path, harness, f"run_{i}", _NEW_ISSUE, now_iso)

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        proposals = hive.list_proposals(harness_name=load_spec(harness).identity)
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
        _write_failure(hive_path, tmp_path, harness, f"run_{i}", _NEW_ISSUE, now_iso)

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(load_spec(harness), harness, _fail_result(), hive_path, strong_model=model)

    with Hive(hive_path) as hive:
        proposals = hive.list_proposals(harness_name=load_spec(harness).identity)
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
        assert hive.list_proposals(harness_name=load_spec(harness).identity) == []
    assert model.prompts == []


def test_ungateable_auto_proposal_records_attempt_and_is_not_repaid(tmp_path: Path):
    """When an auto-draft gates to nothing, a terminal `rejected` row is still
    recorded so the cooldown/failure-window advances — otherwise every failing
    run past min_failures re-pays a strong-model call with no throttle."""
    harness = _harness(tmp_path, min_failures=1, cooldown_hours=24.0)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC).isoformat()
    _write_failure(hive_path, tmp_path, harness, "run_bad", "same issue", now)
    invalid_payload = json.dumps(
        {
            "rationale": "switch policy",
            "yaml_changes": [{"path": "loop.policy", "value": "sequential_steps"}],
        }
    )
    model = FakeStrongModel([invalid_payload, invalid_payload])
    spec = load_spec(harness)

    # First failing run: the draft gates to nothing (sequential_steps needs a
    # non-empty loop.steps), but the attempt is persisted as a terminal
    # `rejected` row — not a can-never-apply pending row, and not nothing.
    _maybe_auto_propose(spec, harness, _fail_result(), hive_path, strong_model=model)
    with Hive(hive_path) as hive:
        rows = hive.list_proposals(harness_name=load_spec(harness).identity)
    assert len(rows) == 1
    assert rows[0]["status"] == "rejected"
    assert rows[0]["trigger"] == "auto"
    assert len(model.prompts) == 1

    # A second failing run inside the 24h cooldown must NOT re-pay the model.
    _write_failure(hive_path, tmp_path, harness, "run_bad2", "same issue", now)
    _maybe_auto_propose(spec, harness, _fail_result(), hive_path, strong_model=model)
    assert len(model.prompts) == 1


def test_repeated_recovered_friction_drafts_after_success(tmp_path: Path):
    harness = _harness(tmp_path, cooldown_hours=1.0)
    _set_friction_trigger(
        harness,
        category="output_validation",
        minimum_runs=3,
        window=5,
    )
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    for index in range(3):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"run_{index}",
            (now + timedelta(seconds=index)).isoformat(),
            category="output_validation",
        )

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="success", run_id="run_2"),
        hive_path,
        strong_model=model,
    )

    with Hive(hive_path) as hive:
        [proposal] = hive.list_proposals(harness_name=load_spec(harness).identity)
    evidence = json.loads(proposal["evidence_json"])["auto_trigger"]
    assert proposal["trigger"] == "auto"
    assert evidence["kind"] == "repeated_friction"
    assert evidence["matched"]["runs"] == 3
    assert evidence["matched"]["category"] == "output_validation"
    assert evidence["matched"]["fingerprint"]
    assert evidence["window_run_ids"] == ["run_2", "run_1", "run_0"]
    assert len(model.prompts) == 1
    assert evidence["matched"]["fingerprint"] in model.prompts[0]["user"]


def test_repeated_friction_below_threshold_does_not_draft(tmp_path: Path):
    harness = _harness(tmp_path)
    _set_friction_trigger(harness, minimum_runs=3)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    for index in range(2):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"run_{index}",
            (now + timedelta(seconds=index)).isoformat(),
        )

    model = FakeStrongModel([])
    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="success", run_id="run_1"),
        hive_path,
        strong_model=model,
    )

    with Hive(hive_path) as hive:
        assert hive.list_proposals(harness_name=load_spec(harness).identity) == []
    assert model.prompts == []


def test_unrelated_success_does_not_retrigger_old_friction(tmp_path: Path):
    harness = _harness(tmp_path)
    _set_friction_trigger(harness, minimum_runs=2)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    for index in range(2):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"run_{index}",
            (now + timedelta(seconds=index)).isoformat(),
        )
    _write_friction(
        hive_path,
        tmp_path,
        harness,
        "clean",
        (now + timedelta(seconds=3)).isoformat(),
        category="none",
    )

    model = FakeStrongModel([])
    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="success", run_id="clean"),
        hive_path,
        strong_model=model,
    )

    assert model.prompts == []


def test_friction_cooldown_runs_survives_reopen(tmp_path: Path):
    harness = _harness(tmp_path, cooldown_hours=1.0)
    _set_friction_trigger(harness, minimum_runs=2, cooldown_runs=3)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    _seed_auto_proposal(
        hive_path,
        harness,
        feedback="old issue",
        created_at=(now - timedelta(hours=2)).isoformat(),
    )
    model = FakeStrongModel([_PAYLOAD])

    for index in range(2):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"new_{index}",
            (now + timedelta(seconds=index)).isoformat(),
        )
    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="success", run_id="new_1"),
        hive_path,
        strong_model=model,
    )
    assert model.prompts == []

    _write_friction(
        hive_path,
        tmp_path,
        harness,
        "new_2",
        (now + timedelta(seconds=2)).isoformat(),
    )
    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="success", run_id="new_2"),
        hive_path,
        strong_model=model,
    )

    assert len(model.prompts) == 1
    with Hive(hive_path) as hive:
        assert len(hive.list_proposals(harness_name=load_spec(harness).identity)) == 2


def test_same_friction_window_dedups_before_model_call(tmp_path: Path):
    harness = _harness(tmp_path, cooldown_hours=1.0)
    _set_friction_trigger(harness, minimum_runs=2)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    for index in range(2):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"run_{index}",
            (now + timedelta(seconds=index)).isoformat(),
        )
    model = FakeStrongModel([_PAYLOAD])
    spec = load_spec(harness)
    result = RunResult(status="success", run_id="run_1")
    _maybe_auto_propose(spec, harness, result, hive_path, strong_model=model)
    with Hive(hive_path) as hive:
        [proposal] = hive.list_proposals(harness_name=spec.identity)
        hive.update_proposal(
            proposal["id"],
            created_at=(now - timedelta(hours=2)).isoformat(),
        )

    _maybe_auto_propose(spec, harness, result, hive_path, strong_model=model)

    assert len(model.prompts) == 1


def test_explicit_final_failure_trigger_uses_its_own_window(tmp_path: Path):
    harness = _harness(tmp_path, min_failures=99)
    construct.set_value(
        harness,
        "evolution.auto_propose.triggers",
        [{"kind": "final_failure", "minimum_runs": 2, "window": 3}],
    )
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    for index in range(2):
        _write_failure(
            hive_path,
            tmp_path,
            harness,
            f"failed_{index}",
            "same issue",
            (now + timedelta(seconds=index)).isoformat(),
        )

    model = FakeStrongModel([_PAYLOAD])
    _maybe_auto_propose(
        load_spec(harness),
        harness,
        RunResult(status="verify_failed", run_id="failed_1"),
        hive_path,
        strong_model=model,
    )

    with Hive(hive_path) as hive:
        [proposal] = hive.list_proposals(harness_name=load_spec(harness).identity)
    trigger = json.loads(proposal["evidence_json"])["auto_trigger"]
    assert trigger["kind"] == "final_failure"
    assert trigger["matched"]["run_ids"] == ["failed_1", "failed_0"]
    assert len(model.prompts) == 1


def test_behavior_change_allows_fresh_friction_proposal(tmp_path: Path):
    harness = _harness(tmp_path, cooldown_hours=1.0)
    _set_friction_trigger(harness, minimum_runs=2)
    hive_path = tmp_path / "hive.db"
    now = datetime.now(UTC)
    model = FakeStrongModel([_PAYLOAD, _PAYLOAD])

    for index in range(2):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"before_{index}",
            (now + timedelta(seconds=index)).isoformat(),
        )
    spec_before = load_spec(harness)
    _maybe_auto_propose(
        spec_before,
        harness,
        RunResult(status="success", run_id="before_1"),
        hive_path,
        strong_model=model,
    )
    with Hive(hive_path) as hive:
        [first] = hive.list_proposals(harness_name=spec_before.identity)
        hive.update_proposal(
            first["id"],
            created_at=(now - timedelta(hours=2)).isoformat(),
        )

    construct.set_value(harness, "loop.max_turns", 9)
    for index in range(2):
        _write_friction(
            hive_path,
            tmp_path,
            harness,
            f"after_{index}",
            (now + timedelta(seconds=10 + index)).isoformat(),
        )
    spec_after = load_spec(harness)
    _maybe_auto_propose(
        spec_after,
        harness,
        RunResult(status="success", run_id="after_1"),
        hive_path,
        strong_model=model,
    )

    with Hive(hive_path) as hive:
        proposals = hive.list_proposals(harness_name=spec_after.identity)
    assert spec_version_hash(spec_before, harness) != spec_version_hash(spec_after, harness)
    assert len(proposals) == 2
    assert len(model.prompts) == 2
