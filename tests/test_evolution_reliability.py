"""Release regressions for bounded, private evolution feedback."""

import json

import pytest
from test_evolve import _PROPOSAL_PAYLOAD, _harness, _report

from hiveloom import cli
from hiveloom.evolve.analyzer import AttemptRecord, FailureReport, analyze, queued_attempt_history
from hiveloom.evolve.evolver import (
    _MAX_HISTORY_CHARS,
    _MAX_NOTES_CHARS,
    _MAX_REPORT_CHARS,
    build_evolve_prompt,
    propose,
)
from hiveloom.evolve.proposals import apply_proposal_by_id, create_proposal, reject_proposal
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import HarnessSpec


def _spec(**kwargs):
    return HarnessSpec(name="h", description="d", system_prompt="s", **kwargs)


def test_redaction_precedes_truncation_and_section_splitting():
    secret = "BEGIN:" + "x" * 3000 + ":END"
    spec = _spec(logging={"redact": {
        "patterns": ["BEGIN:.*:END"],
        "keys": ["password"],
        "paths": ["attempt_history[*].note"],
    }})
    report = FailureReport(
        harness_name="h", total_runs=1, success_rate=0,
        recent_failures=[{"task": secret}],
        analyst_notes=[secret],
        attempt_history=[AttemptRecord(
            outcome="reverted", rationale=secret, yaml_diff=secret,
            measured={"password": "key-secret"}, note="path-secret",
        )],
    )
    _, prompt = build_evolve_prompt(spec, report)
    assert "BEGIN:" not in prompt.split("<untrusted_failure_report_json>")[1]
    assert "x" * 30 not in prompt
    assert "key-secret" not in prompt
    assert "path-secret" not in prompt
    assert "[REDACTED]" in prompt
    assert report.attempt_history[0].note == "path-secret"  # no caller mutation


def test_provider_params_in_current_spec_are_redacted():
    spec = _spec(model={"params": {"metadata": {"password": "private-value"}}},
                 logging={"redact": {"keys": ["password"]}})
    _, prompt = build_evolve_prompt(spec, _report())
    assert "private-value" not in prompt


def test_wide_collections_and_custom_ledgers_have_section_budgets():
    report = FailureReport(
        harness_name="h", total_runs=100, success_rate=0,
        recent_failures=[{"task": "x" * 2000} for _ in range(200)],
        analyst_notes=["note" * 2000 for _ in range(100)],
        attempt_history=[AttemptRecord(outcome="reverted", rationale="z" * 20_000,
                                      measured={str(i): "w" * 2000 for i in range(50)})
                         for _ in range(100)],
    )
    _, prompt = build_evolve_prompt(_spec(), report)
    for tag, limit in [("untrusted_failure_report_json", _MAX_REPORT_CHARS),
                       ("operator_findings_json", _MAX_NOTES_CHARS)]:
        section = prompt.split(f"<{tag}>\n")[1].split(f"</{tag}>")[0].strip()
        assert len(section) <= limit
        assert json.loads(section)["truncated"]
    history = prompt.split("<untrusted_attempt_history>\n")[1].split(
        "</untrusted_attempt_history>")[0]
    assert len(history) < _MAX_HISTORY_CHARS + 100
    assert "truncated" in history


def test_queue_memory_keeps_rejected_paths_and_reason(tmp_path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        rejected = create_proposal(hive, spec, harness, _report(),
                                   FakeStrongModel([_PROPOSAL_PAYLOAD]), trigger="manual")
        reject_proposal(hive, rejected.id, "measurement was underpowered")
        [record] = queued_attempt_history(hive, spec.identity)
        assert record.outcome == "rejected"
        assert "loop.max_turns" in record.changed_paths
        assert "underpowered" in record.note
        assert record.measured == {}
        assert queued_attempt_history(hive, "unrelated") == []
        # Default memory is cross-version; failure evidence remains scoped.
        report = analyze(hive, spec.identity, version="another-version")
        assert report.attempt_history == [record]
        assert report.total_runs == 0
        assert analyze(hive, spec.identity, attempt_history=[]).attempt_history == []


def test_applied_history_records_only_applied_paths(tmp_path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        record = create_proposal(hive, spec, harness, _report(),
                                 FakeStrongModel([_PROPOSAL_PAYLOAD]), trigger="manual")
        apply_proposal_by_id(hive, harness, record.id)
        [attempt] = queued_attempt_history(hive, spec.identity)
        assert attempt.outcome == "applied"
        assert attempt.changed_paths == ["loop.max_turns"]
        assert "no measured effect" in attempt.note


@pytest.mark.parametrize("bad_json", ["not JSON", "null", "[]"])
def test_malformed_legacy_queue_receipts_do_not_break_analysis(tmp_path, bad_json):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        record = create_proposal(hive, spec, harness, _report(),
                                 FakeStrongModel([_PROPOSAL_PAYLOAD]), trigger="manual")
        reject_proposal(hive, record.id, "skip")
        hive.update_proposal(record.id, apply_result_json=bad_json)
        assert queued_attempt_history(hive, spec.identity)[0].changed_paths


def test_new_findings_or_measurements_do_not_reuse_a_stale_pending_proposal(tmp_path):
    harness = _harness(tmp_path)
    spec = load_spec(harness)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD] * 3)
    with Hive(tmp_path / "hive.db") as hive:
        report = _report()
        first = create_proposal(hive, spec, harness, report, model, trigger="manual")
        report.analyst_notes = ["Formatting is no longer the bottleneck"]
        second = create_proposal(hive, spec, harness, report, model, trigger="manual")
        report.attempt_history = [AttemptRecord(outcome="inconclusive", measured={"n": 10})]
        third = create_proposal(hive, spec, harness, report, model, trigger="manual")
        again = create_proposal(hive, spec, harness, report, model, trigger="manual")
    assert len({first.id, second.id, third.id}) == 3
    assert again.id == third.id
    assert len(model.prompts) == 3


def test_cli_accepts_findings_without_prior_failed_runs(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    harness = _harness(tmp_path)
    model = FakeStrongModel([_PROPOSAL_PAYLOAD])
    monkeypatch.setattr("hiveloom.generate.llm.build_strong_model", lambda *a: model)
    result = CliRunner().invoke(cli.app, ["evolve", str(harness), "--propose", "--json",
                                        "--note", "Check retrieval coverage",
                                        "--note", "Formatting already passes"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "pending"
    assert "Check retrieval coverage" in model.prompts[0]["user"]
    assert "Formatting already passes" in model.prompts[0]["user"]


def test_invalid_reply_values_are_not_echoed_into_repair_prompt():
    model = FakeStrongModel([
        json.dumps({"yaml_changes": "password=private"}), _PROPOSAL_PAYLOAD,
    ])
    propose(_spec(), _report(), model)
    assert "private" not in model.prompts[-1]["user"]
    assert "yaml_changes" in model.prompts[-1]["user"]
