"""Eval reports and paired comparisons use indexed state, not raw traces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hiveloom import catalog, construct, ext
from hiveloom.cli import app
from hiveloom.eval_reports import build_eval_report, compare_evals, render_report_markdown
from hiveloom.eval_runner import load_eval_manifest, manifest_path, run_eval
from hiveloom.evals import EvalCase, EvalSpec, ScorerOutput
from hiveloom.execution import RunExecutionEnvelope, VerificationSummary
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.loop.agent_loop import RunResult
from hiveloom.metrics import RunMetric
from hiveloom.models.capabilities import IdentityEvidence, ModelProbeResult
from hiveloom.spec.loader import load_spec

cli = CliRunner()


def _register() -> None:
    if "report_cases" in catalog.DATASETS:
        return
    api = ext.ExtensionAPI(source="test:eval-report")
    api.register_dataset(
        "report_cases",
        lambda params, _ctx: lambda: [
            EvalCase(id=f"case-{index}", input=f"case {index}", expected=index)
            for index in range(params["count"])
        ],
        description="Synthetic report cases.",
        params=[{"name": "count", "type": "int", "default": 2}],
    )

    def score(context):
        return ScorerOutput(
            metrics=[
                RunMetric(
                    run_id=context.run_result.run_id,
                    name="quality",
                    value=float(context.run_result.output),
                    direction="maximize",
                    unit="ratio",
                    source="report_fixture",
                    scope="case",
                )
            ]
        )

    api.register_scorer(
        "report_quality",
        lambda _params, _ctx: score,
        description="Synthetic report score.",
    )


def _files(tmp_path: Path, case_count: int = 2) -> tuple[Path, ModelProbeResult]:
    _register()
    harness = tmp_path / "harness"
    if not harness.exists():
        construct.init_harness(harness, name="eval-report", task="Synthetic task.")
    spec = EvalSpec(
        harness="harness",
        dataset={"loader": "report_cases", "params": {"count": case_count}},
        scorers=["report_quality"],
        repetitions=2,
    )
    eval_file = tmp_path / "eval.yaml"
    eval_file.write_text(
        yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    model = load_spec(harness).model
    probe = ModelProbeResult(
        requested_provider=model.provider,
        requested_model=model.id,
        effective_provider=model.provider,
        effective_models=[model.id],
        identity=IdentityEvidence(
            policy="exact",
            status="exact",
            accepted=True,
            accepted_models=[model.id],
        ),
        capabilities={},
        live=True,
        calls=1,
        adapter_digest="report-adapter-v1",
        probed_at="2026-01-01T00:00:00+00:00",
        expires_at="2100-01-01T00:00:00+00:00",
    )
    return eval_file, probe


def _executor(offset: float, cost_source: str, identity=None):
    """``identity(cell, case)`` may return envelope fields to override."""

    def execute(*, manifest, cell, case, spec):
        del spec
        harness = load_spec(manifest.harness_path)
        value = offset + (case.expected * 0.1) + (cell.repetition * 0.2)
        recovered = cell.repetition == 1
        failed = case.expected == 1 and cell.repetition == 1 and offset == 0.2
        status = "verify_failed" if failed else "success"
        verification = VerificationSummary(
            attempts=2 if recovered else 1,
            first_pass_valid=not recovered,
            recovery_attempted=recovered,
            recovered=recovered and not failed,
            final_status="failed" if failed else "passed",
        )
        execution = RunExecutionEnvelope(
            run_id=cell.run_id,
            status=status,
            harness_id=harness.id,
            harness_name=harness.name,
            schema_version=harness.schema_version,
            behavior_hash=manifest.harness_behavior_hash,
            execution_fingerprint=f"fp-{manifest.eval_run_id}-{cell.cell_id}",
            requested_provider=manifest.requested_provider,
            requested_model=manifest.requested_model,
            effective_provider=manifest.requested_provider,
            effective_model=manifest.requested_model,
            duration_ms=100 + cell.repetition * 50,
            cost_usd=0.01 + offset / 100,
            cost_source=cost_source,
            verification=verification,
        )
        if identity is not None:
            execution = RunExecutionEnvelope.model_validate(
                {**execution.model_dump(mode="json"), **identity(cell, case)}
            )
        writer = TraceWriter(
            manifest.trace_root,
            cell.run_id,
            harness.name,
            manifest.harness_behavior_hash,
            harness_id=harness.id,
        )
        writer.emit("run_started", input=case.input)
        writer.emit(
            "run_finished",
            status=status,
            output=str(value),
            turns=1,
            cost_usd=execution.cost_usd,
            duration_seconds=execution.duration_ms / 1000,
            execution=execution.model_dump(mode="json"),
            verdicts=[],
            artifacts=[],
            provider_calls=[],
        )
        return RunResult(
            status=status,
            output=str(value),
            turns=1,
            cost_usd=execution.cost_usd,
            duration_seconds=execution.duration_ms / 1000,
            run_id=cell.run_id,
            trace_path=str(writer.path),
            execution=execution,
        )

    return execute


def test_report_rebuilds_counts_metrics_cost_and_stability_without_traces(tmp_path: Path):
    eval_file, probe = _files(tmp_path)
    manifest = run_eval(
        eval_file,
        execute_cell=_executor(0.2, "billed"),
        model_probe=probe,
        eval_run_id="eval_report_baseline",
    )
    for cell in manifest.cells:
        Path(cell.trace_path).unlink()

    report = build_eval_report(manifest.eval_run_id)

    assert report["cells"]["sample_count"] == 4
    assert report["final_success"]["count"] == 3
    assert report["first_pass"]["valid_count"] == 2
    assert report["recovery"]["attempted_count"] == 2
    assert report["recovery"]["recovered_count"] == 1
    assert report["cost"][0]["source"] == "billed"
    assert report["cost"][0]["sample_count"] == 4
    assert report["metrics"][0]["sample_count"] == 4
    assert report["metrics"][0]["missing_value_count"] == 0
    assert report["metrics"][0]["stability"]["case_count"] == 2

    single = run_eval(
        eval_file,
        repetitions=1,
        execute_cell=_executor(0.5, "estimated"),
        model_probe=probe,
        eval_run_id="eval_report_single",
    )
    single_report = build_eval_report(single.eval_run_id)
    assert "stability" not in single_report["metrics"][0]


def test_compare_pairs_cells_and_labels_unmatched_cases(tmp_path: Path):
    eval_file, probe = _files(tmp_path, case_count=2)
    baseline = run_eval(
        eval_file,
        execute_cell=_executor(0.2, "billed"),
        model_probe=probe,
        eval_run_id="eval_compare_baseline",
    )
    eval_file, _probe = _files(tmp_path, case_count=1)
    candidate = run_eval(
        eval_file,
        execute_cell=_executor(0.5, "estimated"),
        model_probe=probe,
        eval_run_id="eval_compare_candidate",
    )

    comparison = compare_evals(baseline.eval_run_id, candidate.eval_run_id)

    assert comparison["pairing"]["matched_count"] == 2
    assert comparison["pairing"]["baseline_unmatched_count"] == 2
    assert comparison["pairing"]["candidate_unmatched_count"] == 0
    metric = comparison["metrics"][0]
    assert metric["sample_count"] == 2
    assert metric["missing_value_count"] == 0
    assert metric["mean_delta"] == pytest.approx(0.3)
    assert comparison["cost_usd"]["comparable_count"] == 0
    assert comparison["cost_usd"]["incomparable_or_missing_count"] == 2


def test_report_and_compare_cli_emit_canonical_json_and_markdown(tmp_path: Path):
    eval_file, probe = _files(tmp_path, case_count=1)
    baseline = run_eval(
        eval_file,
        execute_cell=_executor(0.2, "billed"),
        model_probe=probe,
        eval_run_id="eval_cli_baseline",
    )
    candidate = run_eval(
        eval_file,
        execute_cell=_executor(0.5, "estimated"),
        model_probe=probe,
        eval_run_id="eval_cli_candidate",
    )

    report = cli.invoke(app, ["eval", "report", baseline.eval_run_id, "--json"])
    comparison = cli.invoke(
        app,
        [
            "eval",
            "compare",
            baseline.eval_run_id,
            candidate.eval_run_id,
            "--format",
            "markdown",
        ],
    )

    assert report.exit_code == 0
    assert json.loads(report.stdout)["report"]["eval_run_id"] == baseline.eval_run_id
    assert comparison.exit_code == 0
    assert "# Eval comparison:" in comparison.stdout
    assert "Paired cells: 2" in comparison.stdout


def test_eval_manifest_index_rolls_back_an_invalid_replacement(tmp_path: Path):
    eval_file, probe = _files(tmp_path, case_count=1)
    manifest = run_eval(
        eval_file,
        execute_cell=_executor(0.2, "billed"),
        model_probe=probe,
        eval_run_id="eval_index_rollback",
    )
    invalid = manifest.model_dump(mode="json")
    invalid["status"] = "running"
    invalid["cells"][0].pop("case_key")

    with Hive() as hive:
        before = hive.get_eval_snapshot(manifest.eval_run_id)
        with pytest.raises(KeyError, match="case_key"):
            hive.upsert_eval_manifest(invalid, "invalid.json")
        after = hive.get_eval_snapshot(manifest.eval_run_id)

    assert before is not None
    assert after is not None
    assert after["eval"]["status"] == "completed"
    assert after["cells"] == before["cells"]


def _call(model: str | None, turn: int = 1) -> dict:
    return {
        "turn": turn,
        "phase": "main",
        "provider": "fake",
        "requested_model": "requested",
        "effective_model": model,
    }


def test_report_carries_per_call_identity_end_to_end(tmp_path: Path):
    eval_file, probe = _files(tmp_path, case_count=2)

    def identity(cell, case):
        if case.expected == 0:
            return {
                "effective_model": "model-a",
                "call_models": [_call("model-a"), _call("model-b", 2), _call("model-a", 3)],
            }
        return {"effective_model": "model-a", "call_models": [_call("model-a")]}

    manifest = run_eval(
        eval_file,
        repetitions=1,
        execute_cell=_executor(0.2, "billed", identity),
        model_probe=probe,
        eval_run_id="eval_identity_carried",
    )
    for cell in manifest.cells:
        Path(cell.trace_path).unlink()

    # The field survives the manifest round-trip.
    reloaded = load_eval_manifest(manifest.eval_run_id)
    assert sorted(
        [call.effective_model for call in cell.call_models or []]
        for cell in reloaded.cells
    ) == [["model-a"], ["model-a", "model-b", "model-a"]]

    report = build_eval_report(manifest.eval_run_id)

    assert report["schema_version"] == 1
    assert report["effective_models"] == ["model-a"]
    assert report["cells"]["identity_missing_count"] == 0
    assert report["cells"]["identity_mixed_count"] == 1
    assert report["cell_identity_missing"] == []
    by_count = {item["call_count"]: item for item in report["cell_identities"]}
    assert by_count[3]["call_effective_models"] == ["model-a", "model-b", "model-a"]
    assert by_count[1]["call_effective_models"] == ["model-a"]
    assert all(not item["identity_missing"] for item in report["cell_identities"])
    assert "Warning" not in render_report_markdown(report)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"effective_model": "model-a", "call_models": []}, "no_model_calls"),
        (
            {"effective_model": "model-a", "call_models": [_call("model-a"), _call(None, 2)]},
            "call_without_effective_model",
        ),
        ({"effective_model": None, "call_models": None}, "no_effective_model"),
        ({"effective_model": None}, "no_effective_model"),
    ],
)
def test_report_flags_each_kind_of_missing_identity(
    tmp_path: Path, fields: dict, reason: str
):
    eval_file, probe = _files(tmp_path, case_count=1)
    manifest = run_eval(
        eval_file,
        repetitions=1,
        execute_cell=_executor(0.2, "billed", lambda _cell, _case: fields),
        model_probe=probe,
        eval_run_id=f"eval_identity_{reason}",
    )

    report = build_eval_report(manifest.eval_run_id)

    assert report["cells"]["identity_missing_count"] == 1
    [flagged] = report["cell_identity_missing"]
    cell = manifest.cells[0]
    assert flagged == {
        "cell_id": cell.cell_id,
        "case_key": cell.case_key,
        "repetition": cell.repetition,
        "run_id": cell.run_id,
        "run_status": cell.run_status,
        "reason": reason,
    }
    assert report["cell_identities"][0]["identity_missing"] is True
    assert "Warning: 1 cell(s) ran without a reported model identity" in (
        render_report_markdown(report)
    )


def test_manifest_without_call_models_reads_as_unreported(tmp_path: Path):
    """An executor that reports no per-call identity writes no field at all, so
    the manifest stays readable by releases that predate it."""
    eval_file, probe = _files(tmp_path, case_count=1)
    manifest = run_eval(
        eval_file,
        repetitions=1,
        execute_cell=_executor(0.2, "billed"),
        model_probe=probe,
        eval_run_id="eval_identity_unreported",
    )

    raw = json.loads(manifest_path(manifest.eval_run_id).read_text(encoding="utf-8"))
    assert all("call_models" not in cell for cell in raw["cells"])
    assert load_eval_manifest(manifest.eval_run_id).cells[0].call_models is None

    report = build_eval_report(manifest.eval_run_id)
    assert report["cells"]["identity_missing_count"] == 0
    assert report["cell_identities"][0]["call_count"] is None
    assert report["cell_identities"][0]["call_effective_models"] is None


def test_pending_and_infrastructure_error_cells_are_not_identity_flagged(tmp_path: Path):
    eval_file, probe = _files(tmp_path, case_count=2)
    pending = run_eval(
        eval_file,
        repetitions=1,
        execute_cell=_executor(0.2, "billed"),
        model_probe=probe,
        eval_run_id="eval_identity_pending",
        max_cells=1,
    )

    def broken(**_kwargs):
        raise RuntimeError("provider unreachable")

    errored = run_eval(
        eval_file,
        repetitions=1,
        execute_cell=broken,
        model_probe=probe,
        eval_run_id="eval_identity_infra",
    )

    pending_report = build_eval_report(pending.eval_run_id)
    assert pending_report["cells"]["missing_count"] == 1
    assert pending_report["cells"]["identity_missing_count"] == 0
    errored_report = build_eval_report(errored.eval_run_id)
    assert errored_report["cells"]["missing_count"] == 2
    assert errored_report["cells"]["identity_missing_count"] == 0
    assert errored_report["cell_identity_missing"] == []


def _collect(report: dict, expected_models: set[str]) -> None:
    """A collector built only on the additive report fields."""
    if report["cells"]["identity_missing_count"]:
        raise ValueError("eval has cells with no model identity")
    for item in report["cell_identities"]:
        served = set(item["call_effective_models"] or []) | {item["effective_model"]}
        unexpected = {model for model in served if model} - expected_models
        if unexpected:
            raise ValueError(f"eval served an unexpected model: {sorted(unexpected)}")


def test_collector_refuses_modelless_and_unexpected_model_cells(tmp_path: Path):
    eval_file, probe = _files(tmp_path, case_count=1)

    def run(eval_run_id: str, fields: dict) -> dict:
        run_eval(
            eval_file,
            repetitions=1,
            execute_cell=_executor(0.2, "billed", lambda _cell, _case: fields),
            model_probe=probe,
            eval_run_id=eval_run_id,
        )
        return build_eval_report(eval_run_id)

    clean = run("eval_collect_clean", {"call_models": [_call("model-a")],
                                       "effective_model": "model-a"})
    modelless = run("eval_collect_modelless", {"call_models": [_call(None)],
                                               "effective_model": None})
    unexpected = run(
        "eval_collect_unexpected",
        {"call_models": [_call("model-b"), _call("model-a", 2)], "effective_model": "model-a"},
    )

    _collect(clean, {"model-a"})
    # The pre-existing aggregate drops the modelless cell and hides the
    # substituted call; only the per-cell fields expose either.
    assert modelless["effective_models"] == []
    assert unexpected["effective_models"] == ["model-a"]
    with pytest.raises(ValueError, match="no model identity"):
        _collect(modelless, {"model-a"})
    with pytest.raises(ValueError, match="unexpected model"):
        _collect(unexpected, {"model-a"})
