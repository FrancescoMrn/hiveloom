"""Tests for the proposals queue: storage, dedup, and apply/reject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Reuse harness/report/CLI fixtures from the evolve tests (tests dir is on sys.path),
# mirroring how test_evolve.py itself reuses test_hive.py's _write_trace.
from test_evolve import _PROPOSAL_PAYLOAD, _fake_model, _harness, _report, _seed_failure, cli_runner

from hiveloom import cli, construct
from hiveloom import trust as trust_mod
from hiveloom.errors import ExitCode, ProposalQueueError, SpecError
from hiveloom.evolve.evolver import ApplyResult, MutationProposal
from hiveloom.evolve.evolver import apply_proposal as evolver_apply_proposal
from hiveloom.evolve.proposals import (
    _dedup_key,
    _memory_dedup_key,
    apply_proposal_by_id,
    create_memory_proposal,
    create_proposal,
    get_proposal,
    list_proposals,
    reject_proposal,
)
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import MemoryEntry


# --------------------------------------------------------------------------- #
# create_proposal
# --------------------------------------------------------------------------- #
def test_create_persists_row_with_dedup_key_and_version_hash(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    report = _report()
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])

    with Hive(tmp_path / "hive.db") as hive:
        record = create_proposal(hive, spec, harness, report, model, trigger="manual")

    assert record.id.startswith("prop_")
    assert record.harness_name == spec.identity
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
    model = FakeStrongModel([_PROPOSAL_PAYLOAD, _PROPOSAL_PAYLOAD])

    with Hive(tmp_path / "hive.db") as hive:
        first = create_proposal(hive, spec, harness, report, model, trigger="manual")
        second = create_proposal(hive, spec, harness, report, model, trigger="manual")

    assert second.id == first.id
    assert len(model.prompts) == 1  # the strong model was never called a second time


def test_create_does_not_queue_a_schema_invalid_gated_batch(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    payload = json.dumps(
        {
            "rationale": "switch policy",
            "yaml_changes": [{"path": "loop.policy", "value": "sequential_steps"}],
        }
    )
    model = FakeStrongModel([payload])

    with Hive(tmp_path / "hive.db") as hive:
        with pytest.raises(ProposalQueueError, match="no applicable changes"):
            create_proposal(hive, spec, harness, _report(), model, trigger="manual")
        assert hive.list_proposals() == []

    assert len(model.prompts) == 1


# --------------------------------------------------------------------------- #
# list_proposals / get_proposal
# --------------------------------------------------------------------------- #
def test_list_and_get_filtering(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")

        assert get_proposal(hive, created.id).id == created.id
        assert get_proposal(hive, "nope") is None

        assert [r.id for r in list_proposals(hive, harness_name=spec.identity)] == [created.id]
        assert list_proposals(hive, harness_name="other") == []
        assert [r.id for r in list_proposals(hive, status="pending")] == [created.id]
        assert list_proposals(hive, status="applied") == []


# --------------------------------------------------------------------------- #
# Trust enforcement (a past CRITICAL audit finding was a spec-loading path
# that skipped trust; the autouse conftest fixture sets HIVELOOM_TRUST=always,
# so these tests must override it to actually exercise the refusal).
# --------------------------------------------------------------------------- #
def test_create_proposal_refuses_when_untrusted(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)  # construct.init_harness auto-trusts locally-built harnesses
    trust_mod.revoke_trust(harness)
    monkeypatch.setenv("HIVELOOM_TRUST", "never")

    spec = load_spec(harness)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])
    with Hive(tmp_path / "hive.db") as hive:
        with pytest.raises(SpecError, match="not trusted"):
            create_proposal(hive, spec, harness, _report(), model, trigger="manual")

        assert model.prompts == []  # never reached the strong model call
        assert hive.list_proposals() == []  # nothing was persisted


def test_apply_proposal_by_id_refuses_when_untrusted(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")

        trust_mod.revoke_trust(harness)
        monkeypatch.setenv("HIVELOOM_TRUST", "never")

        yaml_path = harness / "harness.yaml"
        before = yaml_path.read_text()
        with pytest.raises(SpecError, match="not trusted"):
            apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)

        assert yaml_path.read_text() == before  # never touched disk
        assert get_proposal(hive, created.id).status == "pending"  # untouched


# --------------------------------------------------------------------------- #
# apply_proposal_by_id
# --------------------------------------------------------------------------- #
def test_apply_delegates_to_evolver_and_updates_status(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])

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
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])

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
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(hive, spec, harness, _report(), model, trigger="manual")
        apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)

        with pytest.raises(ProposalQueueError, match="already applied"):
            apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)


def test_apply_claim_blocks_a_second_caller_after_both_observed_pending(
    tmp_path: Path, monkeypatch
):
    """Simulate two callers retaining the same pre-claim pending snapshot."""
    harness = _harness(tmp_path)
    spec = load_spec(harness)

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(
            hive,
            spec,
            harness,
            _report(),
            FakeStrongModel([_PROPOSAL_PAYLOAD]),
            trigger="manual",
        )
        pending_snapshot = hive.get_proposal(created.id)
        real_get = hive.get_proposal
        stale_reads = 2

        def stale_get(proposal_id: str):
            nonlocal stale_reads
            if stale_reads:
                stale_reads -= 1
                return dict(pending_snapshot)
            return real_get(proposal_id)

        apply_calls: list[str] = []

        def fake_apply(*_args, **_kwargs):
            apply_calls.append(created.id)
            return ApplyResult(
                changed=True,
                old_version_hash=created.spec_version_hash,
                new_version_hash="deadbeefcafe",
                counter=1,
            )

        monkeypatch.setattr(hive, "get_proposal", stale_get)
        monkeypatch.setattr(
            "hiveloom.evolve.proposals.evolver.apply_proposal", fake_apply
        )

        apply_proposal_by_id(hive, harness, created.id)
        with pytest.raises(ProposalQueueError, match="already applied"):
            apply_proposal_by_id(hive, harness, created.id)

    assert apply_calls == [created.id]


def test_apply_failure_releases_claim_for_retry(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)
    spec = load_spec(harness)

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(
            hive,
            spec,
            harness,
            _report(),
            FakeStrongModel([_PROPOSAL_PAYLOAD]),
            trigger="manual",
        )
        statuses_during_apply: list[str] = []

        def fail_apply(*_args, **_kwargs):
            statuses_during_apply.append(hive.get_proposal(created.id)["status"])
            raise RuntimeError("apply failed")

        monkeypatch.setattr(
            "hiveloom.evolve.proposals.evolver.apply_proposal", fail_apply
        )

        with pytest.raises(RuntimeError, match="apply failed"):
            apply_proposal_by_id(hive, harness, created.id)

        assert statuses_during_apply == ["applying"]
        assert hive.get_proposal(created.id)["status"] == "pending"


def test_an_apply_that_changes_nothing_leaves_the_proposal_pending(tmp_path: Path):
    """Declining the YAML applies nothing, so the row is not resolved: the
    lesson stays in the queue to apply, instead of being marked `applied` and
    then refused as "already applied" on the retry."""
    harness = _harness(tmp_path)
    yaml_path = harness / "harness.yaml"
    before = yaml_path.read_text()

    with Hive(tmp_path / "hive.db") as hive:
        created = create_proposal(
            hive,
            load_spec(harness),
            harness,
            _report(),
            FakeStrongModel([_PROPOSAL_PAYLOAD]),
            trigger="manual",
        )

        declined = apply_proposal_by_id(hive, harness, created.id, apply_yaml=False)
        assert declined.changed is False
        assert get_proposal(hive, created.id).status == "pending"
        assert yaml_path.read_text() == before

        # The retry is an ordinary apply, not "already applied".
        applied = apply_proposal_by_id(hive, harness, created.id, apply_yaml=True)
        assert applied.changed is True
        assert get_proposal(hive, created.id).status == "applied"

    assert load_spec(harness).loop.max_turns == 25


def test_json_apply_without_yes_refuses_instead_of_resolving_the_row(tmp_path: Path):
    """`--json` cannot prompt, so applying gated YAML without `--yes` is a
    usage error (exit 3) — the proposal is still there afterwards."""
    harness = _harness(tmp_path)
    yaml_path = harness / "harness.yaml"
    before = yaml_path.read_text()
    with Hive() as hive:
        created = create_proposal(
            hive,
            load_spec(harness),
            harness,
            _report(),
            FakeStrongModel([_PROPOSAL_PAYLOAD]),
            trigger="manual",
        )

    refused = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), created.id, "--json"]
    )

    assert refused.exit_code == ExitCode.SPEC_ERROR
    payload = json.loads(refused.stdout)
    assert payload["ok"] is False
    assert "--yes" in payload["error"]
    assert yaml_path.read_text() == before
    with Hive() as hive:
        assert get_proposal(hive, created.id).status == "pending"

    applied = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), created.id, "--yes", "--json"]
    )
    assert applied.exit_code == ExitCode.OK, applied.stdout
    with Hive() as hive:
        assert get_proposal(hive, created.id).status == "applied"


def test_interactive_apply_still_asks_and_a_no_keeps_the_proposal(tmp_path: Path):
    """The prompt path is untouched: answering "n" applies nothing and leaves
    the row pending for a later yes."""
    harness = _harness(tmp_path)
    with Hive() as hive:
        created = create_proposal(
            hive,
            load_spec(harness),
            harness,
            _report(),
            FakeStrongModel([_PROPOSAL_PAYLOAD]),
            trigger="manual",
        )

    declined = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), created.id], input="n\n"
    )
    assert declined.exit_code == ExitCode.OK, declined.stdout
    assert "still pending" in declined.stdout
    with Hive() as hive:
        assert get_proposal(hive, created.id).status == "pending"

    accepted = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), created.id], input="y\n"
    )
    assert accepted.exit_code == ExitCode.OK, accepted.stdout
    assert load_spec(harness).loop.max_turns == 25
    with Hive() as hive:
        assert get_proposal(hive, created.id).status == "applied"


# --------------------------------------------------------------------------- #
# reject_proposal
# --------------------------------------------------------------------------- #
def test_reject_records_reason_and_leaves_harness_untouched(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])
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
# create_memory_proposal: the executor's model-free path into the same queue
# --------------------------------------------------------------------------- #
def _lesson(**overrides) -> MemoryEntry:
    fields = {
        "id": "iso-dates",
        "kind": "rule",
        "title": "Dates in ISO 8601",
        "content": "Emit dates as YYYY-MM-DD.",
    }
    return MemoryEntry(**{**fields, **overrides})


def test_create_memory_proposal_queues_an_append_without_a_model(tmp_path: Path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)

    with Hive(tmp_path / "hive.db") as hive:
        record = create_memory_proposal(hive, spec, harness, _lesson(), run_id="run_1")

    assert record.status == "pending"
    assert record.trigger == "executor"
    assert record.spec_version_hash == spec_version_hash(spec, harness)
    (change,) = record.proposal.yaml_changes
    assert change.path == "memory.entries.+"
    assert change.value["id"] == "iso-dates"
    assert record.gate.accepted[0].path == "memory.entries.+"
    assert record.evidence == {"run_id": "run_1", "entry_id": "iso-dates", "kind": "rule"}


def test_memory_proposals_dedup_on_the_lesson_not_its_wording(tmp_path: Path):
    """The dedup slot is keyed on normalized content, so the same lesson under
    a different id, title or evidence reuses the pending row a reviewer is
    already looking at."""
    harness = _harness(tmp_path)
    spec = load_spec(harness)

    with Hive(tmp_path / "hive.db") as hive:
        first = create_memory_proposal(hive, spec, harness, _lesson(), run_id="run_1")
        second = create_memory_proposal(
            hive,
            spec,
            harness,
            _lesson(id="dates", title="Use ISO", content="  Emit dates as   YYYY-MM-DD. "),
            run_id="run_2",
        )
        assert len(list_proposals(hive, spec.identity)) == 1

    assert second.id == first.id
    assert _memory_dedup_key(_lesson()).startswith("mem:")


def test_a_memory_proposal_the_gate_refuses_is_never_queued(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_value(harness, "evolution.mutable", ["system_prompt"])
    spec = load_spec(harness)

    with Hive(tmp_path / "hive.db") as hive:
        with pytest.raises(ProposalQueueError, match="refused by the gate"):
            create_memory_proposal(hive, spec, harness, _lesson(), run_id="run_1")
        assert list_proposals(hive, spec.identity) == []


def test_an_executor_row_is_reviewed_and_applied_like_any_other(tmp_path: Path):
    """`proposals list/show/apply` take no notice of the trigger: an executor
    row goes through the same gate, validation and rollback as an evolved one.
    """
    harness = _harness(tmp_path)
    with Hive() as hive:
        record = create_memory_proposal(
            hive, load_spec(harness), harness, _lesson(), run_id="run_1"
        )

    listed = cli_runner.invoke(cli.app, ["proposals", "list", str(harness), "--json"])
    assert [p["trigger"] for p in json.loads(listed.stdout)["proposals"]] == ["executor"]
    shown = cli_runner.invoke(
        cli.app, ["proposals", "show", str(harness), record.id, "--json"]
    )
    assert json.loads(shown.stdout)["proposal"]["yaml_changes"][0]["path"] == (
        "memory.entries.+"
    )
    applied = cli_runner.invoke(
        cli.app, ["proposals", "apply", str(harness), record.id, "--yes", "--json"]
    )
    assert applied.exit_code == ExitCode.OK, applied.stdout
    assert [e.id for e in load_spec(harness).memory.entries] == ["iso-dates"]


def test_an_executor_row_can_be_rejected_and_nothing_is_written(tmp_path: Path):
    harness = _harness(tmp_path)
    before = (harness / "harness.yaml").read_text()
    with Hive() as hive:
        record = create_memory_proposal(
            hive, load_spec(harness), harness, _lesson(), run_id="run_1"
        )

    rejected = cli_runner.invoke(
        cli.app,
        ["proposals", "reject", str(harness), record.id, "--reason", "not durable", "--json"],
    )

    assert rejected.exit_code == ExitCode.OK, rejected.stdout
    assert json.loads(rejected.stdout)["status"] == "rejected"
    assert (harness / "harness.yaml").read_text() == before


# --------------------------------------------------------------------------- #
# CLI: `hiveloom proposals list|show|apply|reject`
# --------------------------------------------------------------------------- #
def _queue_via_cli(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """Seed a failure, fake the strong model, and queue a proposal via the CLI."""
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, harness)
    _fake_model(monkeypatch, _PROPOSAL_PAYLOAD)

    result = cli_runner.invoke(cli.app, ["evolve", str(harness), "--propose", "--json"])
    assert result.exit_code == ExitCode.OK, result.stdout
    return harness, json.loads(result.stdout)["id"]


def test_cli_proposals_list_and_show(tmp_path: Path, monkeypatch):
    harness, proposal_id = _queue_via_cli(tmp_path, monkeypatch)

    listed = cli_runner.invoke(cli.app, ["proposals", "list", str(harness), "--json"])
    assert listed.exit_code == ExitCode.OK, listed.stdout
    listed_proposals = json.loads(listed.stdout)["proposals"]
    assert [p["id"] for p in listed_proposals] == [proposal_id]
    # list --json and show --json expand the same JSON-text columns identically.
    assert listed_proposals[0]["gate"]["accepted"][0]["path"] == "loop.max_turns"
    assert listed_proposals[0]["proposal"]["rationale"] == "clarify"
    raw_keys = {"proposal_json", "gate_json", "apply_result_json"}
    assert raw_keys.isdisjoint(listed_proposals[0])

    shown = cli_runner.invoke(cli.app, ["proposals", "show", str(harness), proposal_id, "--json"])
    assert shown.exit_code == ExitCode.OK, shown.stdout
    payload = json.loads(shown.stdout)
    assert payload["id"] == proposal_id
    assert payload["gate"] == listed_proposals[0]["gate"]
    assert raw_keys.isdisjoint(payload)

    missing = cli_runner.invoke(cli.app, ["proposals", "show", str(harness), "nope", "--json"])
    assert missing.exit_code == ExitCode.SPEC_ERROR


def _wrong_harness_for_queued_proposal(
    tmp_path: Path, monkeypatch
) -> tuple[Path, Path, str]:
    harness, proposal_id = _queue_via_cli(tmp_path, monkeypatch)
    wrong_harness = tmp_path / "wrong"
    construct.init_harness(wrong_harness, name="wrong", task="Do another thing.")
    return harness, wrong_harness, proposal_id


def test_cli_proposals_show_rejects_a_mismatched_harness(tmp_path: Path, monkeypatch):
    _harness_dir, wrong_harness, proposal_id = _wrong_harness_for_queued_proposal(
        tmp_path, monkeypatch
    )
    listed = cli_runner.invoke(
        cli.app, ["proposals", "list", str(wrong_harness), "--json"]
    )
    assert listed.exit_code == ExitCode.OK
    assert json.loads(listed.stdout)["proposals"] == []

    result = cli_runner.invoke(
        cli.app,
        ["proposals", "show", str(wrong_harness), proposal_id, "--json"],
    )
    assert result.exit_code == ExitCode.SPEC_ERROR, result.stdout
    right = load_spec(_harness_dir).identity
    wrong = load_spec(wrong_harness).identity
    assert f"belongs to harness '{right}', not '{wrong}'" in json.loads(result.stdout)["error"]


def test_cli_proposals_apply_rejects_a_mismatched_harness(tmp_path: Path, monkeypatch):
    harness, wrong_harness, proposal_id = _wrong_harness_for_queued_proposal(
        tmp_path, monkeypatch
    )
    result = cli_runner.invoke(
        cli.app,
        ["proposals", "apply", str(wrong_harness), proposal_id, "--yes", "--json"],
    )
    assert result.exit_code == ExitCode.SPEC_ERROR, result.stdout
    right = load_spec(harness).identity
    wrong = load_spec(wrong_harness).identity
    assert f"belongs to harness '{right}', not '{wrong}'" in json.loads(result.stdout)["error"]
    with Hive() as hive:
        assert get_proposal(hive, proposal_id).status == "pending"
    assert load_spec(harness).loop.max_turns != 25


def test_cli_proposals_reject_rejects_a_mismatched_harness(tmp_path: Path, monkeypatch):
    _harness_dir, wrong_harness, proposal_id = _wrong_harness_for_queued_proposal(
        tmp_path, monkeypatch
    )
    result = cli_runner.invoke(
        cli.app,
        [
            "proposals",
            "reject",
            str(wrong_harness),
            proposal_id,
            "--reason",
            "wrong harness",
            "--json",
        ],
    )
    assert result.exit_code == ExitCode.SPEC_ERROR, result.stdout
    right = load_spec(_harness_dir).identity
    wrong = load_spec(wrong_harness).identity
    assert f"belongs to harness '{right}', not '{wrong}'" in json.loads(result.stdout)["error"]
    with Hive() as hive:
        assert get_proposal(hive, proposal_id).status == "pending"


def test_cli_proposals_apply_never_prompts_for_an_unknown_id(tmp_path: Path):
    """Regression: the YAML-apply confirm must fire only after validation.

    Invoked with no --yes/--json/piped input on purpose: if the confirm were
    still computed before apply_proposal_by_id's trust/existence/staleness
    checks (the pre-fix ordering bug), typer.confirm would hit EOF on the
    empty stdin and abort outside `_guard`, producing something other than
    the clean SPEC_ERROR a caller-mistake (unknown id) should produce.
    """
    harness = _harness(tmp_path)
    result = cli_runner.invoke(cli.app, ["proposals", "apply", str(harness), "prop_nope"])
    assert result.exit_code == ExitCode.SPEC_ERROR, result.output


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


# --------------------------------------------------------------------------- #
# cli._make_approve_code (shared by `evolve` and `proposals apply`)
# --------------------------------------------------------------------------- #
def test_make_approve_code_allowlist_json_and_interactive_modes(tmp_path: Path, monkeypatch):
    from hiveloom.cli import _make_approve_code
    from hiveloom.evolve.evolver import CodeChange

    harness = _harness(tmp_path)
    change = CodeChange(file="validators/check.py", source="x", rationale="r")

    # An allowlisted path auto-approves without prompting.
    approve = _make_approve_code(harness, json_output=False, allowlist={"validators/check.py"})
    assert approve(change) is True

    # --json mode never prompts; unapproved changes are rejected.
    approve = _make_approve_code(harness, json_output=True)
    assert approve(change) is False

    # Interactive mode resolves the display path via resolve_code_change_path
    # (matching `evolve`'s own closure) and honors the confirm answer.
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    approve = _make_approve_code(harness, json_output=False)
    assert approve(change) is True


def test_cli_approve_code_strips_whitespace_and_drops_empty_entries(
    tmp_path: Path, monkeypatch
):
    harness = _harness(tmp_path)
    captured: list[set[str] | None] = []

    def capture_allowlist(_harness_dir, *, json_output, allowlist=None):
        captured.append(allowlist)
        return lambda _change: False

    monkeypatch.setattr(cli, "_make_approve_code", capture_allowlist)
    result = cli_runner.invoke(
        cli.app,
        [
            "proposals",
            "apply",
            str(harness),
            "prop_nope",
            "--approve-code",
            "a.py, b.py, ,",
            "--json",
        ],
    )

    assert result.exit_code == ExitCode.SPEC_ERROR
    assert captured == [{"a.py", "b.py"}]


# --------------------------------------------------------------------------- #
# Appending memory: `memory.entries.+` and the staleness guard around it
# --------------------------------------------------------------------------- #
def _append_payload(entry_id: str) -> str:
    """A proposing model's answer: one lesson appended with `+`."""
    return json.dumps(
        {
            "rationale": f"remember {entry_id}",
            "yaml_changes": [
                {
                    "path": "memory.entries.+",
                    "value": {
                        "id": entry_id,
                        "kind": "rule",
                        "title": entry_id,
                        "content": f"Remember {entry_id}.",
                        "source": "evolve",
                    },
                }
            ],
        }
    )


def _distinct_report(note: str):
    """The same failure state under a different operator finding — a different
    dedup key, so two proposals queue against one spec version."""
    return _report().model_copy(update={"analyst_notes": [note]})


def test_two_memory_appends_queued_against_one_version_both_apply(tmp_path: Path):
    """The first apply moves the version hash. An append says *what* it does,
    not where it lands, so the second still applies — and composes with the
    first instead of replacing it."""
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_append_payload("iso-dates"), _append_payload("trim-space")])

    with Hive(tmp_path / "hive.db") as hive:
        first = create_proposal(
            hive, spec, harness, _distinct_report("dates"), model, trigger="manual"
        )
        second = create_proposal(
            hive, spec, harness, _distinct_report("whitespace"), model, trigger="manual"
        )
        assert first.spec_version_hash == second.spec_version_hash

        assert apply_proposal_by_id(hive, harness, first.id, apply_yaml=True).changed
        assert apply_proposal_by_id(hive, harness, second.id, apply_yaml=True).changed
        assert [get_proposal(hive, r.id).status for r in (first, second)] == [
            "applied",
            "applied",
        ]

    assert [e.id for e in load_spec(harness).memory.entries] == ["iso-dates", "trim-space"]


def test_a_non_append_proposal_is_still_refused_once_the_harness_moves(tmp_path: Path):
    """The exemption is exactly as wide as the reason for it: anything that is
    not a pure memory append still has to be regenerated."""
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_append_payload("iso-dates"), _PROPOSAL_PAYLOAD])

    with Hive(tmp_path / "hive.db") as hive:
        lesson = create_proposal(
            hive, spec, harness, _distinct_report("dates"), model, trigger="manual"
        )
        turns = create_proposal(hive, spec, harness, _report(), model, trigger="manual")
        apply_proposal_by_id(hive, harness, lesson.id, apply_yaml=True)

        with pytest.raises(ProposalQueueError, match="regenerate"):
            apply_proposal_by_id(hive, harness, turns.id, apply_yaml=True)
        assert get_proposal(hive, turns.id).status == "pending"

    assert load_spec(harness).loop.max_turns != 25


def test_an_append_that_no_longer_fits_is_refused_at_apply(tmp_path: Path):
    """Applying against a newer version is not applying unchecked: the gate and
    full re-validation run at apply, so a store that filled up in the meantime
    refuses the append and leaves `harness.yaml` untouched."""
    harness = _harness(tmp_path)
    construct.set_value(harness, "memory.max_entries", 1)

    with Hive(tmp_path / "hive.db") as hive:
        record = create_memory_proposal(
            hive, load_spec(harness), harness, _lesson(), run_id="run_1"
        )
        # Out-of-band: a reviewer adds the one entry this harness may hold.
        construct.add_memory_entry(harness, kind="fact", title="Nulls", content="Null is null.")
        before = (harness / "harness.yaml").read_text()

        result = apply_proposal_by_id(hive, harness, record.id, apply_yaml=True)

        assert result.changed is False
        assert "max_entries" in result.rejected[0]["reason"]
        # Nothing landed, so the row is still there to reject or re-queue.
        assert get_proposal(hive, record.id).status == "pending"

    assert (harness / "harness.yaml").read_text() == before
    assert [e.id for e in load_spec(harness).memory.entries] == ["nulls"]
