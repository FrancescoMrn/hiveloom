"""Closing the loop: predictions on evolutions, assessments, measured experiments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_signal import write_run
from typer.testing import CliRunner

from hiveloom import cli, construct, ext
from hiveloom.eval_runner import run_eval
from hiveloom.evals import EvalCase, EvalSpec, ScorerOutput
from hiveloom.evolve import proposals as proposals_mod
from hiveloom.evolve.analyzer import analyze
from hiveloom.evolve.assess import assess_all, assess_evolution, attempt_history
from hiveloom.evolve.evolver import MutationProposal, apply_proposal
from hiveloom.evolve.experiment import run_experiment
from hiveloom.execution import RunExecutionEnvelope
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter, spec_version_hash
from hiveloom.loop.agent_loop import RunResult
from hiveloom.models.capabilities import IdentityEvidence, ModelProbeResult
from hiveloom.spec.loader import load_spec

cli_runner = CliRunner()


def _evolution(
    hive: Hive,
    *,
    target: dict | None = None,
    old: str = "v1",
    new: str = "v2",
    proposal_id: str | None = None,
) -> dict:
    hive.record_evolution(
        "h", old, new, 1, "fix the fetch", "2026-01-02T00:00:00+00:00",
        proposal_id=proposal_id,
        prediction={"target": target, "objective_expectations": []} if target else None,
        changes={"paths": ["system_prompt"], "code": [], "yaml_diff": "-a\n+b"},
    )
    return hive.evolutions("h")[0]


def _runs(
    traces: Path, version: str, *, failing: int, passing: int, error: bool = True,
    prefix: str = "",
) -> None:
    for i in range(failing):
        write_run(
            traces, f"{prefix}{version}f{i}", status="verify_failed", version=version,
            tools=["http_get"], tool_errors=["http_get"] if error else [],
        )
    for i in range(passing):
        write_run(traces, f"{prefix}{version}s{i}", version=version, tools=["http_get"])


def _pair(hive: Hive, eval_id: str, case: str, left_run: str, right_run: str) -> None:
    """Record that two runs are the same eval case under two versions."""
    for eval_run_id, run_id in ((f"e-{left_run}", left_run), (f"e-{right_run}", right_run)):
        hive._conn.execute(
            "INSERT OR REPLACE INTO eval_runs VALUES (?, 'completed', ?, 'h', 'b', 'p', 'm', 1, "
            "'x', 't', 't')",
            (eval_run_id, eval_id),
        )
        hive._conn.execute(
            "INSERT INTO eval_cells (eval_run_id, cell_id, case_key, repetition, status, run_id, "
            "run_status, scorer_status, requested_provider, requested_model, "
            "execution_fingerprint, duration_ms, cost_usd, cost_source, "
            "verification_attempts, recovery_attempted, recovered, "
            "verification_final_status, trace_disabled) VALUES "
            "(?, ?, ?, 0, 'completed', ?, 's', 'ok', 'p', 'm', 'f', 1, 0, 'none', 0, 0, 0, "
            "'ok', 0)",
            (eval_run_id, f"c-{case}", case, run_id),
        )
    hive._conn.commit()


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_evolution_rows_keep_their_prediction_and_changes(tmp_path):
    with Hive(tmp_path / "hive.db") as hive:
        row = _evolution(
            hive, target={"signal": "tool_error:http_get", "expect": "decrease"},
            proposal_id="prop_1",
        )
        hive.record_evolution_decision(row["evolution_id"], {"action": "kept"})
        [row] = hive.evolutions("h")
    assert row["proposal_id"] == "prop_1"
    assert row["prediction"]["target"]["signal"] == "tool_error:http_get"
    assert row["changes"]["paths"] == ["system_prompt"]
    assert row["decision"] == {"action": "kept"}


def test_apply_records_the_prediction_against_the_proposal(tmp_path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="h", task="Do a thing.")
    proposal = MutationProposal.model_validate(
        {
            "rationale": "tell it to retry the fetch",
            "target": {"signal": "tool_error:http_get", "expect": "decrease", "by": 0.3},
            "yaml_changes": [{"path": "system_prompt", "value": "Retry a failed fetch once."}],
        }
    )
    with Hive(tmp_path / "hive.db") as hive:
        result = apply_proposal(directory, proposal, hive=hive, proposal_id="prop_x")
        [row] = hive.evolutions(load_spec(directory).identity)
    assert result.changed
    assert row["proposal_id"] == "prop_x"
    assert row["prediction"]["target"] == {
        "signal": "tool_error:http_get", "expect": "decrease", "by": 0.3,
        "rationale": "",
    }
    assert row["changes"]["paths"] == ["system_prompt"]
    assert "Retry a failed fetch once." in row["changes"]["yaml_diff"]


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #
def test_a_target_that_moves_as_predicted_is_confirmed(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=8, passing=2)
    _runs(traces, "v2", failing=1, passing=9, error=False)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        row = _evolution(hive, target={"signal": "tool_error:http_get", "expect": "decrease"})
        assessment = assess_evolution(hive, "h", row)
    assert assessment.verdict == "confirmed"
    assert assessment.target_measure.test == "fisher"
    assert (assessment.target_measure.before_count, assessment.target_measure.after_count) == (8, 0)
    assert assessment.measured()["verdict"] == "confirmed"


def test_a_worse_success_rate_is_a_regression_whatever_the_target_did(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=1, passing=11, error=True)
    _runs(traces, "v2", failing=10, passing=2, error=False)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        row = _evolution(hive, target={"signal": "tool_error:http_get", "expect": "decrease"})
        assert assess_evolution(hive, "h", row).verdict == "regressed"


def test_too_few_new_runs_is_pending_and_a_small_sample_inconclusive(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=3, passing=3)
    _runs(traces, "v2", failing=1, passing=2)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        row = _evolution(hive, target={"signal": "tool_error:http_get", "expect": "decrease"})
        pending = assess_evolution(hive, "h", row)
        assert pending.verdict == "pending" and pending.runs_needed
        inconclusive = assess_evolution(hive, "h", row, min_runs=3)
    assert inconclusive.verdict == "inconclusive"
    assert "runs per version" in inconclusive.summary


def test_a_stated_effect_the_sample_could_see_but_did_not_is_refuted(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=10, passing=10)
    _runs(traces, "v2", failing=10, passing=10)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        row = _evolution(
            hive, target={"signal": "tool_error:http_get", "expect": "decrease", "by": 0.5}
        )
        assert assess_evolution(hive, "h", row).verdict == "refuted"
        row = _evolution(
            hive, target={"signal": "tool_error:http_get", "expect": "decrease"}, new="v2"
        )
        # Without a stated size, no change is only inconclusive.
        assert assess_evolution(hive, "h", row).verdict == "inconclusive"


def test_paired_eval_cells_are_compared_case_by_case(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=6, passing=0)
    _runs(traces, "v2", failing=0, passing=6, error=False)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        for i in range(6):
            _pair(hive, "eval-a", f"case{i}", f"v1f{i}", f"v2s{i}")
        row = _evolution(hive, target={"signal": "success_rate", "expect": "increase"})
        assessment = assess_evolution(hive, "h", row)
    assert assessment.success.test == "mcnemar"
    assert (assessment.success.improved_pairs, assessment.success.worsened_pairs) == (6, 0)
    assert assessment.success.p_value == pytest.approx(0.03125)
    assert assessment.verdict == "confirmed"


def test_a_metric_target_uses_the_sign_test_when_paired(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=0, passing=6)
    _runs(traces, "v2", failing=0, passing=6)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        for i in range(6):
            _pair(hive, "eval-a", f"case{i}", f"v1s{i}", f"v2s{i}")
            for run_id, value in ((f"v1s{i}", 0.2), (f"v2s{i}", 0.9)):
                hive._conn.execute(
                    "INSERT INTO run_metrics (idempotency_key, run_id, name, value, direction, "
                    "unit, source, scope, metadata_json, recorded_at) VALUES "
                    "(?, ?, 'quality', ?, 'maximize', 'ratio', 's', 'case', '{}', 't')",
                    (run_id, run_id, value),
                )
        hive._conn.commit()
        hive.record_evolution(
            "h", "v1", "v2", 1, "r", "2026-01-02T00:00:00+00:00",
            prediction={
                "target": None,
                "objective_expectations": [
                    {"metric": "quality", "expected_change": "increase", "rationale": ""}
                ],
            },
        )
        assessment = assess_evolution(hive, "h", hive.evolutions("h")[0])
    assert assessment.target == "metric:quality"
    assert assessment.target_measure.test == "sign"
    assert assessment.verdict == "confirmed"


def test_an_evolution_without_a_prediction_is_judged_on_success(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=9, passing=1)
    _runs(traces, "v2", failing=1, passing=9)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        hive.record_evolution("h", "v1", "v2", 1, "legacy", "2026-01-02T00:00:00+00:00")
        [assessment] = assess_all(hive, "h")
    assert assessment.implicit_target and assessment.target == "success_rate"
    assert assessment.verdict == "confirmed"


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def test_history_carries_measurements_and_skips_the_linked_queue_row(tmp_path):
    traces = tmp_path / "traces"
    _runs(traces, "v1", failing=8, passing=2)
    _runs(traces, "v2", failing=1, passing=9, error=False)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_dir(traces)
        for pid, status in (("prop_applied", "applied"), ("prop_rejected", "rejected")):
            hive.insert_proposal(
                {
                    "id": pid, "harness_name": "h", "spec_version_hash": "v1",
                    "dedup_key": pid, "status": status, "trigger": "manual",
                    "rationale": pid, "proposal_json": "{}", "gate_json": "{}",
                    "evidence_json": None,
                    "apply_result_json": json.dumps({"reason": "no"}),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "resolved_at": "2026-01-01T00:00:00+00:00",
                }
            )
        _evolution(
            hive, target={"signal": "tool_error:http_get", "expect": "decrease"},
            proposal_id="prop_applied",
        )
        history = attempt_history(hive, "h")
        report = analyze(hive, "h", version="v2")
    assert [record.outcome for record in history] == ["confirmed", "rejected"]
    assert history[0].measured["target"] == "tool_error:http_get expected to decrease"
    assert "no measured effect" in history[1].note
    assert report.attempt_history[0].outcome == "confirmed"


def test_assess_cli_reports_verdicts(tmp_path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="h", task="Do a thing.")
    spec = load_spec(directory)
    traces = directory / ".hiveloom" / "traces"
    for version, failing, passing in (("v1", 8, 2), ("v2", 1, 9)):
        for i in range(failing):
            write_run(traces, f"{version}f{i}", status="verify_failed", version=version,
                      name="h", harness_id=spec.id)
        for i in range(passing):
            write_run(traces, f"{version}s{i}", version=version, name="h", harness_id=spec.id)
    with Hive() as hive:
        hive.record_evolution(spec.identity, "v1", "v2", 1, "r", "2026-01-02T00:00:00+00:00")
    result = cli_runner.invoke(cli.app, ["assess", str(directory), "--json"])
    assert result.exit_code == 0, result.output
    [assessment] = json.loads(result.output)["assessments"]
    assert assessment["verdict"] == "confirmed"
    plain = cli_runner.invoke(cli.app, ["assess", str(directory)])
    assert "confirmed" in plain.output


def test_proposals_apply_links_the_queue_row(tmp_path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="h", task="Do a thing.")
    spec = load_spec(directory)
    with Hive() as hive:
        hive.insert_proposal(
            {
                "id": "prop_q", "harness_name": spec.identity,
                "spec_version_hash": spec_version_hash(spec, directory),
                "dedup_key": "k", "status": "pending", "trigger": "manual",
                "rationale": "r",
                "proposal_json": MutationProposal.model_validate(
                    {
                        "target": {"signal": "success_rate", "expect": "increase"},
                        "yaml_changes": [{"path": "system_prompt", "value": "Be careful."}],
                    }
                ).model_dump_json(),
                "gate_json": "{}", "evidence_json": None, "apply_result_json": None,
                "created_at": "2026-01-01T00:00:00+00:00", "resolved_at": None,
            }
        )
        proposals_mod.apply_proposal_by_id(hive, directory, "prop_q")
        [row] = hive.evolutions(spec.identity)
    assert row["proposal_id"] == "prop_q"
    assert row["prediction"]["target"]["signal"] == "success_rate"


# --------------------------------------------------------------------------- #
# Experiment
# --------------------------------------------------------------------------- #
def _eval_fixture(tmp_path: Path, cases: int = 6):
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="exp", task="Fetch and answer.")
    api = ext.ExtensionAPI(source="test:experiment")
    api.register_dataset(
        "exp_cases",
        lambda _p, _c: lambda: [
            EvalCase(id=f"case-{i}", input=f"input {i}", expected={}) for i in range(cases)
        ],
        description="Experiment cases.",
    )
    api.register_scorer(
        "exp_none", lambda _p, _c: (lambda _ctx: ScorerOutput(metrics=[])),
        description="No metrics.",
    )
    spec = EvalSpec(
        harness="harness", dataset={"loader": "exp_cases"}, scorers=["exp_none"],
        repetitions=1, model_identity="exact",
    )
    eval_file = tmp_path / "eval.yaml"
    eval_file.write_text(yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False))
    harness_spec = load_spec(harness)
    probe = ModelProbeResult(
        requested_provider=harness_spec.model.provider,
        requested_model=harness_spec.model.id,
        effective_provider=harness_spec.model.provider,
        effective_models=[harness_spec.model.id],
        identity=IdentityEvidence(
            policy="exact", status="exact", accepted=True,
            accepted_models=[harness_spec.model.id],
        ),
        capabilities={}, live=True, calls=1, adapter_digest="fixture",
        probed_at="2026-01-01T00:00:00+00:00", expires_at="2100-01-01T00:00:00+00:00",
    )

    def execute(*, manifest, cell, case, spec):
        """The harness succeeds exactly when its prompt says to retry the fetch."""
        del spec
        harness_now = load_spec(manifest.harness_path)
        fixed = "retry" in harness_now.system_prompt.lower()
        status = "success" if fixed else "verify_failed"
        writer = TraceWriter(
            manifest.trace_root, cell.run_id, harness_now.name,
            manifest.harness_behavior_hash, harness_id=harness_now.id,
        )
        writer.emit("run_started", input=case.input)
        writer.emit("tool_call", id="1", name="http_get", input={})
        writer.emit("tool_result", id="1", name="http_get", is_error=not fixed, content="x")
        execution = RunExecutionEnvelope(
            run_id=cell.run_id, status=status, harness_id=harness_now.id,
            harness_name=harness_now.name, schema_version=harness_now.schema_version,
            behavior_hash=manifest.harness_behavior_hash,
            execution_fingerprint=f"fp-{cell.cell_id}",
            requested_provider=manifest.requested_provider,
            requested_model=manifest.requested_model,
            resolved_provider=manifest.requested_provider,
            resolved_model=manifest.requested_model,
            effective_provider=manifest.requested_provider,
            effective_model=manifest.requested_model,
        )
        writer.emit(
            "run_finished", status=status, output="answer", turns=1, cost_usd=0.01,
            duration_seconds=0.1, execution=execution.model_dump(mode="json"),
            verdicts=[], artifacts=[], provider_calls=[],
        )
        return RunResult(
            status=status, output="answer", turns=1, cost_usd=0.01, duration_seconds=0.1,
            run_id=cell.run_id, trace_path=str(writer.path), execution=execution,
        )

    def runner():
        return run_eval(eval_file, execute_cell=execute, model_probe=probe)

    return harness, eval_file, runner


def _proposal(prompt: str) -> str:
    return json.dumps(
        {
            "rationale": "the fetch errors in every failing run",
            "target": {"signal": "tool_error:http_get", "expect": "decrease", "by": 0.5},
            "yaml_changes": [{"path": "system_prompt", "value": prompt}],
        }
    )


def test_experiment_keeps_a_confirmed_change(tmp_path):
    harness, eval_file, runner = _eval_fixture(tmp_path)
    model = FakeStrongModel([_proposal("Retry a failed fetch once before answering.")])
    with Hive() as hive:
        [result] = run_experiment(harness, eval_file, model, hive=hive, eval_runner=runner)
        [row] = [r for r in hive.evolutions(load_spec(harness).identity)]
    assert result.status == "kept", result.reason
    assert result.assessment.verdict == "confirmed"
    assert result.assessment.success.test == "mcnemar"
    assert result.baseline_eval_run_id and result.candidate_eval_run_id
    assert row["decision"]["action"] == "kept"
    assert "Retry a failed fetch" in load_spec(harness).system_prompt
    # The proposer saw the located signal.
    assert "tool_error:http_get" in model.prompts[0]["user"]


def test_experiment_reverts_a_change_that_did_nothing(tmp_path):
    harness, eval_file, runner = _eval_fixture(tmp_path)
    before = (harness / "harness.yaml").read_text()
    model = FakeStrongModel(
        [_proposal("Answer concisely."), _proposal("Retry a failed fetch once.")]
    )
    with Hive() as hive:
        results = run_experiment(
            harness, eval_file, model, rounds=2, hive=hive, eval_runner=runner
        )
        key = load_spec(harness).identity
        history = attempt_history(hive, key)
    assert [r.status for r in results] == ["reverted", "kept"]
    assert results[0].assessment.verdict == "refuted"
    # The second round was proposed with the first one's measured refutation in view.
    assert "reverted" in model.prompts[1]["user"]
    assert history[0].outcome == "kept" and history[1].outcome == "reverted"
    # The revert restored the bytes of the version it replaced, so round two
    # started from the same spec version (reused baseline, no re-run).
    assert results[1].old_version == results[0].old_version
    assert results[1].baseline_eval_run_id is None
    assert before != (harness / "harness.yaml").read_text()


def test_experiment_requires_yes_and_its_own_harness(tmp_path):
    harness, eval_file, _runner = _eval_fixture(tmp_path)
    result = cli_runner.invoke(
        cli.app, ["evolve", str(harness), "--experiment", str(eval_file), "--json"]
    )
    assert result.exit_code == 3
    assert "--yes" in json.loads(result.output)["error"]
    other = tmp_path / "other"
    construct.init_harness(other, name="other", task="t")
    with pytest.raises(Exception, match="an experiment must measure the harness it changes"):
        run_experiment(other, eval_file, FakeStrongModel([]))
