"""Tests for the proposals queue: storage, dedup, and apply/reject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Reuse the Hive trace-writing helper from the Hive tests (tests dir is on sys.path).
from test_hive import _write_trace
from typer.testing import CliRunner

from hiveloom import cli, construct
from hiveloom.errors import ExitCode, ProposalQueueError
from hiveloom.evolve.analyzer import FailureCluster, FailureReport
from hiveloom.evolve.evolver import MutationProposal
from hiveloom.evolve.evolver import apply_proposal as evolver_apply_proposal
from hiveloom.evolve.proposals import (
    _dedup_key,
    apply_proposal_by_id,
    create_proposal,
    get_proposal,
    list_proposals,
    reject_proposal,
)
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import load_spec

cli_runner = CliRunner()


def _harness(tmp_path: Path) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="demo", task="Do a thing.")
    return directory


def _report() -> FailureReport:
    return FailureReport(
        harness_name="demo",
        total_runs=5,
        success_rate=0.2,
        clusters=[FailureCluster(kind="verdict", signature="not valid JSON", count=4)],
    )


def _proposal_payload() -> str:
    return json.dumps(
        {"rationale": "clarify", "yaml_changes": [{"path": "loop.max_turns", "value": 25}]}
    )


# --------------------------------------------------------------------------- #
# create_proposal
# --------------------------------------------------------------------------- #
def test_create_persists_row_with_dedup_key_and_version_hash(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    report = _report()
    model = FakeStrongModel([_proposal_payload()])

    with Hive(tmp_path / "hive.db") as hive:
        record = create_proposal(hive, spec, harness, report, model, trigger="manual")

    assert record.id.startswith("prop_")
    assert record.harness_name == "demo"
    assert record.spec_version_hash == spec_version_hash(spec, harness)
    assert record.dedup_key == _dedup_key(report)
    assert record.status == "pending"
    assert record.trigger == "manual"
    assert json.loads(record.proposal_json)["rationale"] == "clarify"
    accepted = json.loads(record.gate_json)["accepted"]
    assert accepted[0]["path"] == "loop.max_turns"


def test_second_create_with_same_failure_state_dedups_without_calling_model(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    report = _report()
    model = FakeStrongModel([_proposal_payload(), _proposal_payload()])

    with Hive(tmp_path / "hive.db") as hive:
        first = create_proposal(hive, spec, harness, report, model, trigger="manual")
        second = create_proposal(hive, spec, harness, report, model, trigger="manual")

    assert second.id == first.id
    assert len(model.prompts) == 1  # the strong model was never called a second time


# --------------------------------------------------------------------------- #
# list_proposals / get_proposal
# --------------------------------------------------------------------------- #
def test_list_and_get_filtering(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_proposal_payload()])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")

        assert get_proposal(hive, created.id).id == created.id
        assert get_proposal(hive, "nope") is None

        assert [r.id for r in list_proposals(hive, harness_name="demo")] == [created.id]
        assert list_proposals(hive, harness_name="other") == []
        assert [r.id for r in list_proposals(hive, status="pending")] == [created.id]
        assert list_proposals(hive, status="applied") == []


# --------------------------------------------------------------------------- #
# apply_proposal_by_id
# --------------------------------------------------------------------------- #
def test_apply_delegates_to_evolver_and_updates_status(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_proposal_payload()])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")
        result = apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)
        resolved = get_proposal(hive, created.id)

    assert result.changed is True
    assert result.applied_yaml[0].path == "loop.max_turns"
    assert resolved.status == "applied"
    assert resolved.resolved_at is not None
    assert json.loads(resolved.apply_result_json)["counter"] == result.counter

    assert load_spec(harness).loop.max_turns == 25
    assert (harness / "harness.yaml").read_text().startswith("# evolved: 1")


def test_apply_unknown_id_raises(tmp_path: Path):
    harness = _harness(tmp_path)
    with Hive(tmp_path / "hive.db") as hive:
        with pytest.raises(ProposalQueueError, match="no proposal"):
            apply_proposal_by_id(hive, harness, "prop_does_not_exist")


def test_apply_stale_spec_hash_raises_without_touching_disk(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_proposal_payload()])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")

        # Simulate an out-of-band harness change between create and apply.
        evolver_apply_proposal(
            harness,
            MutationProposal(
                yaml_changes=[{"path": "system_prompt", "value": "changed out of band"}]
            ),
            apply_yaml=True,
        )
        yaml_path = harness / "harness.yaml"
        before = yaml_path.read_text()

        with pytest.raises(ProposalQueueError, match="regenerate"):
            apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)

        after = yaml_path.read_text()
        resolved = get_proposal(hive, created.id)

    assert after == before  # apply_proposal_by_id never touched disk
    assert resolved.status == "pending"


def test_apply_already_resolved_raises(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_proposal_payload()])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")
        apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)

        with pytest.raises(ProposalQueueError, match="already applied"):
            apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)


# --------------------------------------------------------------------------- #
# reject_proposal
# --------------------------------------------------------------------------- #
def test_reject_records_reason_and_leaves_harness_untouched(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_proposal_payload()])
    yaml_path = harness / "harness.yaml"
    before = yaml_path.read_text()

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")
        reject_proposal(hive, created.id, "not worth it")
        resolved = get_proposal(hive, created.id)

    assert resolved.status == "rejected"
    assert json.loads(resolved.apply_result_json)["reason"] == "not worth it"
    assert resolved.resolved_at is not None
    assert yaml_path.read_text() == before


def test_reject_unknown_id_raises(tmp_path: Path):
    with Hive(tmp_path / "hive.db") as hive:
        with pytest.raises(ProposalQueueError, match="no proposal"):
            reject_proposal(hive, "prop_does_not_exist", "why not")


# --------------------------------------------------------------------------- #
# CLI: `hiveloom proposals list|show|apply|reject`
# --------------------------------------------------------------------------- #
def _queue_via_cli(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """Seed a failure, monkeypatch the strong model, and queue a proposal via the CLI."""
    harness = _harness(tmp_path)
    trace = _write_trace(
        tmp_path, "run_a", name="demo", status="verify_failed",
        verifications=[(False, "not valid JSON")],
    )
    with Hive() as hive:  # the autouse conftest fixture points this at a throwaway db
        hive.ingest_trace_file(trace)
    monkeypatch.setattr(
        cli, "_strong_model", lambda *a, **k: FakeStrongModel([_proposal_payload()])
    )

    result = cli_runner.invoke(cli.app, ["evolve", str(harness), "--propose", "--json"])
    assert result.exit_code == ExitCode.OK, result.stdout
    return harness, json.loads(result.stdout)["id"]


def test_cli_proposals_list_and_show(tmp_path: Path, monkeypatch):
    harness, proposal_id = _queue_via_cli(tmp_path, monkeypatch)

    listed = cli_runner.invoke(cli.app, ["proposals", "list", str(harness), "--json"])
    assert listed.exit_code == ExitCode.OK, listed.stdout
    ids = [p["id"] for p in json.loads(listed.stdout)["proposals"]]
    assert ids == [proposal_id]

    shown = cli_runner.invoke(cli.app, ["proposals", "show", str(harness), proposal_id, "--json"])
    assert shown.exit_code == ExitCode.OK, shown.stdout
    payload = json.loads(shown.stdout)
    assert payload["id"] == proposal_id
    assert payload["gate"]["accepted"][0]["path"] == "loop.max_turns"

    missing = cli_runner.invoke(cli.app, ["proposals", "show", str(harness), "nope", "--json"])
    assert missing.exit_code == ExitCode.SPEC_ERROR


def test_cli_proposals_apply_then_reject_on_resolved_fails(tmp_path: Path, monkeypatch):
    harness, proposal_id = _queue_via_cli(tmp_path, monkeypatch)

    applied = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), proposal_id, "--yes", "--json"]
    )
    assert applied.exit_code == ExitCode.OK, applied.stdout
    payload = json.loads(applied.stdout)
    assert payload["changed"] is True
    assert load_spec(harness).loop.max_turns == 25

    again = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), proposal_id, "--yes", "--json"]
    )
    assert again.exit_code == ExitCode.SPEC_ERROR


def test_cli_proposals_reject(tmp_path: Path, monkeypatch):
    harness, proposal_id = _queue_via_cli(tmp_path, monkeypatch)
    before = (harness / "harness.yaml").read_text()

    rejected = cli_runner.invoke(
        cli.app,
        ["proposals", "reject", str(harness), proposal_id, "--reason", "nah", "--json"],
    )

    assert rejected.exit_code == ExitCode.OK, rejected.stdout
    assert json.loads(rejected.stdout)["status"] == "rejected"
    assert (harness / "harness.yaml").read_text() == before
