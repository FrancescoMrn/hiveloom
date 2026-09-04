"""Resumable native eval manifests and deterministic cell scheduling."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hiveloom import construct, ext
from hiveloom.cli import app
from hiveloom.eval_runner import (
    load_eval_manifest,
    manifest_path,
    resume_eval,
    run_eval,
)
from hiveloom.evals import EvalCase, EvalSpec, ScorerOutput
from hiveloom.execution import RunExecutionEnvelope
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.loop.agent_loop import RunResult
from hiveloom.metrics import RunMetric
from hiveloom.models.capabilities import IdentityEvidence, ModelProbeResult
from hiveloom.spec.loader import load_spec

cli = CliRunner()


def _register_components(case_count: int) -> None:
    api = ext.ExtensionAPI(source="test:eval-runner")
    api.register_dataset(
        "runner_cases",
        lambda _params, _ctx: lambda: [
            EvalCase(
                id=f"case-{index}",
                input=f"synthetic input {index}",
                expected={"answer": f"answer-{index}"},
            )
            for index in range(case_count)
        ],
        description="Synthetic runner cases.",
    )

    def scorer(context):
        return ScorerOutput(
            metrics=[
                RunMetric(
                    run_id=context.run_result.run_id,
                    name="quality",
                    value=float(context.expected["answer"] in context.run_result.output),
                    direction="maximize",
                    unit="ratio",
                    source="runner_fixture",
                    scope="case",
                )
            ]
        )

    api.register_scorer(
        "runner_quality",
        lambda _params, _ctx: scorer,
        description="Synthetic runner score.",
    )


def _fixture(tmp_path: Path, *, case_count: int = 4, repetitions: int = 1):
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="eval-runner", task="Synthetic task.")
    _register_components(case_count)
    spec = EvalSpec(
        harness="harness",
        dataset={"loader": "runner_cases"},
        scorers=["runner_quality"],
        repetitions=repetitions,
        model_identity="exact",
    )
    eval_file = tmp_path / "eval.yaml"
    eval_file.write_text(
        yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    harness_spec = load_spec(harness)
    probe = ModelProbeResult(
        requested_provider=harness_spec.model.provider,
        requested_model=harness_spec.model.id,
        effective_provider=harness_spec.model.provider,
        effective_models=[harness_spec.model.id],
        identity=IdentityEvidence(
            policy="exact",
            status="exact",
            accepted=True,
            accepted_models=[harness_spec.model.id],
        ),
        capabilities={},
        live=True,
        calls=1,
        adapter_digest="fixture-adapter-v1",
        probed_at="2026-01-01T00:00:00+00:00",
        expires_at="2100-01-01T00:00:00+00:00",
    )
    return eval_file, probe


def _completed_result(*, manifest, cell, case, spec) -> RunResult:
    harness = load_spec(manifest.harness_path)
    execution = RunExecutionEnvelope(
        run_id=cell.run_id,
        status="success",
        harness_id=harness.id,
        harness_name=harness.name,
        schema_version=harness.schema_version,
        behavior_hash=manifest.harness_behavior_hash,
        execution_fingerprint=f"fingerprint-{cell.cell_id}",
        requested_provider=manifest.requested_provider,
        requested_model=manifest.requested_model,
        resolved_provider=manifest.requested_provider,
        resolved_model=manifest.requested_model,
        effective_provider=manifest.requested_provider,
        effective_model=manifest.requested_model,
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
        status="success",
        output=case.expected["answer"],
        turns=1,
        cost_usd=0.01,
        duration_seconds=0.1,
        execution=execution.model_dump(mode="json"),
        verdicts=[],
        artifacts=[],
        provider_calls=[],
    )
    return RunResult(
        status="success",
        output=case.expected["answer"],
        turns=1,
        cost_usd=0.01,
        duration_seconds=0.1,
        run_id=cell.run_id,
        trace_path=str(writer.path),
        execution=execution,
    )


def test_interrupted_batch_resumes_only_missing_cells_and_never_rescores(
    tmp_path: Path,
):
    eval_file, probe = _fixture(tmp_path, case_count=4)
    calls: list[str] = []

    def execute(**kwargs):
        calls.append(kwargs["cell"].cell_id)
        return _completed_result(**kwargs)

    partial = run_eval(
        eval_file,
        execute_cell=execute,
        model_probe=probe,
        max_cells=2,
        eval_run_id="eval_resume_fixture",
    )
    assert partial.status == "incomplete"
    assert partial.summary()["completed"] == 2
    first_calls = list(calls)

    completed = resume_eval(
        partial.eval_run_id,
        execute_cell=execute,
        model_probe=probe,
    )
    assert completed.status == "completed"
    assert completed.summary()["completed"] == 4
    assert len(calls) == 4
    assert len(set(calls)) == 4
    assert all(call in calls for call in first_calls)

    resumed_again = resume_eval(
        partial.eval_run_id,
        execute_cell=execute,
        model_probe=probe,
    )
    assert resumed_again.status == "completed"
    assert len(calls) == 4
    with Hive() as hive:
        metrics = hive.list_metrics(load_spec(tmp_path / "harness").identity)
    assert len(metrics) == 4


def test_resume_rejects_changed_eval_identity(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=2)
    manifest = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        max_cells=1,
        eval_run_id="eval_changed_fixture",
    )
    data = yaml.safe_load(eval_file.read_text(encoding="utf-8"))
    data["repetitions"] = 2
    eval_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="identity changed"):
        resume_eval(
            manifest.eval_run_id,
            execute_cell=_completed_result,
            model_probe=probe,
        )


def test_resume_rejects_changed_harness_behavior(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=2)
    manifest = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        max_cells=1,
        eval_run_id="eval_behavior_fixture",
    )
    construct.set_value(
        tmp_path / "harness", "description", "Changed synthetic task."
    )

    with pytest.raises(ValueError, match="harness behavior changed"):
        resume_eval(
            manifest.eval_run_id,
            execute_cell=_completed_result,
            model_probe=probe,
        )


def test_concurrency_limit_and_manifest_order_are_deterministic(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=6)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def execute(**kwargs):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        try:
            return _completed_result(**kwargs)
        finally:
            with state_lock:
                active -= 1

    manifest = run_eval(
        eval_file,
        execute_cell=execute,
        model_probe=probe,
        concurrency=2,
        eval_run_id="eval_concurrency_fixture",
    )

    assert manifest.status == "completed"
    assert maximum_active == 2
    order = [(cell.case_key, cell.repetition) for cell in manifest.cells]
    assert order == sorted(order)
    assert all(Path(cell.trace_path).is_file() for cell in manifest.cells)


def test_model_outcome_and_infrastructure_error_remain_distinct(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=2)
    call_count = 0

    def execute(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("synthetic runner transport failure")
        result = _completed_result(**kwargs)
        result.status = "error"
        return result

    manifest = run_eval(
        eval_file,
        execute_cell=execute,
        model_probe=probe,
        concurrency=1,
        eval_run_id="eval_failure_fixture",
    )

    assert manifest.status == "incomplete"
    infrastructure = next(
        cell for cell in manifest.cells if cell.status == "infrastructure_error"
    )
    model_error = next(cell for cell in manifest.cells if cell.status == "completed")
    assert infrastructure.error_phase == "execution"
    assert infrastructure.error_type == "ConnectionError"
    assert model_error.run_status == "error"
    assert model_error.scorer_status == "success"


def test_infrastructure_retry_uses_a_distinct_run_id(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    attempts: list[str] = []

    def execute(**kwargs):
        attempts.append(kwargs["cell"].run_id)
        if len(attempts) == 1:
            raise ConnectionError("synthetic transient failure")
        return _completed_result(**kwargs)

    manifest = run_eval(
        eval_file,
        execute_cell=execute,
        model_probe=probe,
        infrastructure_retries=1,
        eval_run_id="eval_retry_fixture",
    )

    assert manifest.status == "completed"
    [cell] = manifest.cells
    assert cell.infrastructure_attempts == 2
    assert cell.attempt_run_ids == attempts
    assert attempts[0] != attempts[1]
    assert attempts[1].endswith("-a2")
    assert cell.metric_ingestion["inserted"] == 1


def test_trace_disabled_is_explicit_and_status_json_is_valid(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)

    def no_trace(**kwargs):
        return RunResult(
            status="success",
            output=kwargs["case"].expected["answer"],
            run_id=kwargs["cell"].run_id,
            trace_path="",
        )

    manifest = run_eval(
        eval_file,
        execute_cell=no_trace,
        model_probe=probe,
        eval_run_id="eval_no_trace_fixture",
    )
    [cell] = manifest.cells
    assert cell.trace_disabled is True
    assert cell.metric_ingestion["state"] == "trace_disabled"

    status = cli.invoke(app, ["eval", "status", manifest.eval_run_id, "--json"])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["manifest"]["cells"][0]["trace_disabled"] is True
    assert load_eval_manifest(manifest.eval_run_id).status == "completed"


def test_resume_scores_a_traced_run_without_executing_it_again(tmp_path: Path):
    """An interruption after the model trace is durable must not bill it twice."""
    eval_file, probe = _fixture(tmp_path, case_count=1)
    manifest = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        max_cells=0,
        eval_run_id="eval_recover_trace_fixture",
    )
    [cell] = manifest.cells
    spec = EvalSpec(
        harness="harness",
        dataset={"loader": "runner_cases"},
        scorers=["runner_quality"],
    )
    result = _completed_result(
        manifest=manifest,
        cell=cell,
        case=EvalCase(id="case-0", input="synthetic input 0", expected={"answer": "answer-0"}),
        spec=spec,
    )
    cell.status = "ran"
    cell.trace_path = result.trace_path
    manifest_path(manifest.eval_run_id).write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    calls: list[str] = []

    def execute(**kwargs):
        calls.append(kwargs["cell"].cell_id)
        return _completed_result(**kwargs)

    resumed = resume_eval(
        manifest.eval_run_id, execute_cell=execute, model_probe=probe
    )

    assert resumed.status == "completed"
    assert calls == []
    assert resumed.cells[0].scorer_status == "success"
    assert resumed.cells[0].metric_ingestion["inserted"] == 1


def test_resume_replays_a_ran_cell_when_its_trace_is_missing(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    manifest = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        max_cells=0,
        eval_run_id="eval_missing_trace_fixture",
    )
    [cell] = manifest.cells
    cell.status = "ran"
    cell.trace_path = str(tmp_path / "missing.jsonl")
    manifest_path(manifest.eval_run_id).write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    calls: list[str] = []

    def execute(**kwargs):
        calls.append(kwargs["cell"].run_id)
        return _completed_result(**kwargs)

    resumed = resume_eval(
        manifest.eval_run_id, execute_cell=execute, model_probe=probe
    )

    assert resumed.status == "completed"
    assert len(calls) == 1
    assert resumed.cells[0].infrastructure_attempts == 1


def test_execution_exception_uses_the_trace_written_before_the_interrupt(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)

    def write_then_interrupt(**kwargs):
        _completed_result(**kwargs)
        raise ConnectionError("connection closed after the trace was written")

    manifest = run_eval(
        eval_file,
        execute_cell=write_then_interrupt,
        model_probe=probe,
        eval_run_id="eval_interrupted_trace_fixture",
    )

    assert manifest.status == "completed"
    assert manifest.cells[0].run_status == "success"
    assert manifest.cells[0].infrastructure_attempts == 1


def test_scoring_failure_preserves_the_completed_model_outcome(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)

    def missing_trace(**kwargs):
        result = RunResult(
            status="success",
            output=kwargs["case"].expected["answer"],
            run_id=kwargs["cell"].run_id,
            trace_path=str(tmp_path / "lost-trace.jsonl"),
        )
        return result

    manifest = run_eval(
        eval_file,
        execute_cell=missing_trace,
        model_probe=probe,
        eval_run_id="eval_scoring_failure_fixture",
    )

    assert manifest.status == "incomplete"
    assert manifest.cells[0].status == "ran"
    assert manifest.cells[0].run_status == "success"
    assert manifest.cells[0].error_phase == "scoring"


def test_manifest_lookup_rejects_unknown_and_malformed_ids():
    with pytest.raises(ValueError, match="eval run not found"):
        load_eval_manifest("eval_missing_fixture")
    with pytest.raises(ValueError, match="invalid eval run id"):
        load_eval_manifest("not an eval id")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"adapter_digest": "different-adapter"}, "provider adapter changed"),
        ({"effective_models": ["different-model"]}, "effective model identity changed"),
    ],
)
def test_resume_rejects_changed_model_execution_identity(
    tmp_path: Path, change: dict[str, object], message: str
):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    manifest = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        max_cells=0,
        eval_run_id="eval_probe_identity_fixture",
    )

    with pytest.raises(ValueError, match=message):
        resume_eval(
            manifest.eval_run_id,
            execute_cell=_completed_result,
            model_probe=probe.model_copy(update=change),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"concurrency": 0}, "concurrency"),
        ({"infrastructure_retries": -1}, "infrastructure retries"),
        ({"max_cells": -1}, "max_cells"),
        ({"repetitions": 0}, "repetitions"),
    ],
)
def test_run_eval_rejects_invalid_scheduling_limits(
    tmp_path: Path, kwargs: dict[str, int], message: str
):
    eval_file, probe = _fixture(tmp_path)

    with pytest.raises(ValueError, match=message):
        run_eval(eval_file, execute_cell=_completed_result, model_probe=probe, **kwargs)
