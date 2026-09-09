"""Eval reports and paired comparisons use indexed state, not raw traces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hiveloom import catalog, construct, ext
from hiveloom.cli import app
from hiveloom.eval_reports import build_eval_report, compare_evals
from hiveloom.eval_runner import run_eval
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


def _executor(offset: float, cost_source: str):
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
