"""Resumable native eval manifests and deterministic cell scheduling."""

from __future__ import annotations

import errno
import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest
import typer
import yaml
from typer.testing import CliRunner

from hiveloom import cli as cli_module
from hiveloom import construct, eval_runner, ext
from hiveloom.cli import app
from hiveloom.eval_runner import (
    load_eval_manifest,
    manifest_path,
    resume_eval,
    run_eval,
)
from hiveloom.evals import EvalCase, EvalSpec, ScorerOutput
from hiveloom.execution import ModelCallIdentity, RunExecutionEnvelope
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


def _wait_for_workers() -> None:
    """Block until the eval's pool threads have ended (an abandoned worker included)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
        thread.name.startswith("ThreadPoolExecutor") for thread in threading.enumerate()
    ):
        time.sleep(0.02)


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

    assert manifest.status == "completed", [
        {
            "cell": cell.cell_id,
            "status": cell.status,
            "phase": cell.error_phase,
            "error_type": cell.error_type,
            "error": cell.error,
        }
        for cell in manifest.cells
        if cell.status != "completed"
    ]
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


@pytest.mark.parametrize("trace_enabled", [True, False])
def test_resume_preserves_the_returned_result_without_reexecuting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trace_enabled: bool
):
    """A callback can add artifacts after its container has written the trace."""
    eval_file, probe = _fixture(tmp_path, case_count=1)
    returned = []
    scored = []
    original_score = eval_runner._score_cell

    def execute(**kwargs):
        result = _completed_result(**kwargs)
        if not trace_enabled:
            Path(result.trace_path).unlink()
            result.trace_path = ""
        result.artifacts.append({"kind": "workspace", "data": "/durable/workspace.tar"})
        result.runtime_config = {"requested": {"model": "synthetic-model"}}
        result.referrals = [{"name": "synthetic-peer"}]
        returned.append(result.model_dump(mode="json"))
        return result

    def score(*args):
        scored.append(args[-1].model_dump(mode="json"))
        if len(scored) == 1:
            raise OSError("temporary scorer outage")
        return original_score(*args)

    monkeypatch.setattr(eval_runner, "_score_cell", score)
    partial = run_eval(eval_file, execute_cell=execute, model_probe=probe)
    assert partial.status == "incomplete"
    assert partial.cells[0].error_phase == "scoring"
    resumed = resume_eval(partial.eval_run_id, execute_cell=execute, model_probe=probe)

    assert resumed.status == "completed"
    assert len(returned) == 1
    assert scored == [returned[0], returned[0]]
    resume_eval(partial.eval_run_id, execute_cell=execute, model_probe=probe)
    assert len(returned) == 1
    assert len(scored) == 2


def test_result_checkpoint_uses_the_harness_redaction_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    construct.set_value(tmp_path / "harness", "logging.redact", {
        "patterns": ["synthetic-pattern"],
        "keys": ["private_key"],
        "paths": ["artifacts[*].data.private_path"],
    })
    scored = []
    original_score = eval_runner._score_cell

    def execute(**kwargs):
        result = _completed_result(**kwargs)
        result.artifacts = [{"kind": "workspace", "data": {
            "label": "synthetic-pattern", "private_key": "synthetic-key-value",
            "private_path": "synthetic-path-value", "path": "/durable/workspace.tar",
        }}]
        return result

    def score(*args):
        scored.append(args[-1])
        if len(scored) == 1:
            raise OSError("temporary scorer outage")
        return original_score(*args)

    monkeypatch.setattr(eval_runner, "_score_cell", score)
    partial = run_eval(eval_file, execute_cell=execute, model_probe=probe)
    [checkpoint] = (manifest_path(partial.eval_run_id).parent / "results").glob("*.json")
    content = checkpoint.read_text()
    assert "synthetic-pattern" not in content
    assert "synthetic-key-value" not in content
    assert "synthetic-path-value" not in content
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert checkpoint.parent.stat().st_mode & 0o777 == 0o700
    public_manifest = manifest_path(partial.eval_run_id).read_text()
    assert "/durable/workspace.tar" not in public_manifest
    resumed = resume_eval(partial.eval_run_id, execute_cell=execute, model_probe=probe)
    assert resumed.status == "completed"
    assert scored[1].artifacts == [{"kind": "workspace", "data": {
        "label": "[REDACTED]", "private_key": "[REDACTED]",
        "private_path": "[REDACTED]", "path": "/durable/workspace.tar",
    }}]
    assert scored[0].artifacts[0]["data"]["label"] == "synthetic-pattern"


@pytest.mark.parametrize("corruption", ["json", "directory"])
def test_corrupt_checkpoint_never_discards_artifacts_or_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    calls = []
    original_score = eval_runner._score_cell

    def execute(**kwargs):
        calls.append(kwargs["cell"].cell_id)
        return _completed_result(**kwargs)

    def unavailable(*args):
        raise OSError("temporary scorer outage")

    monkeypatch.setattr(eval_runner, "_score_cell", unavailable)
    partial = run_eval(eval_file, execute_cell=execute, model_probe=probe)
    [checkpoint] = (manifest_path(partial.eval_run_id).parent / "results").glob("*.json")
    original = checkpoint.read_bytes()
    if corruption == "json":
        checkpoint.write_text("invalid json")
    else:
        checkpoint.unlink()
        checkpoint.mkdir()
    resumed = resume_eval(partial.eval_run_id, execute_cell=execute, model_probe=probe,
                          infrastructure_retries=2)
    assert resumed.status == "incomplete"
    assert resumed.cells[0].status == "infrastructure_error"
    assert resumed.cells[0].error_phase == "recovery"
    assert resumed.cells[0].error_type == "InvalidCheckpoint"
    assert len(calls) == 1
    if checkpoint.is_dir():
        checkpoint.rmdir()
    checkpoint.write_bytes(original)
    monkeypatch.setattr(eval_runner, "_score_cell", original_score)
    assert resume_eval(partial.eval_run_id, execute_cell=execute, model_probe=probe,
                          infrastructure_retries=2).status == (
        "completed"
    )
    assert len(calls) == 1


def test_checkpoint_survives_interruption_before_manifest_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    original_apply = eval_runner._apply_result
    calls = []

    def execute(**kwargs):
        calls.append(kwargs["cell"].cell_id)
        result = _completed_result(**kwargs)
        Path(result.trace_path).unlink()
        result.trace_path = ""
        return result

    def interrupt(*args):
        raise OSError("interrupted after durable result")

    monkeypatch.setattr(eval_runner, "_apply_result", interrupt)
    with pytest.raises(OSError, match="after durable result"):
        run_eval(eval_file, execute_cell=execute, model_probe=probe,
                 eval_run_id="eval_checkpoint_window")
    monkeypatch.setattr(eval_runner, "_apply_result", original_apply)
    resumed = resume_eval("eval_checkpoint_window", execute_cell=execute, model_probe=probe)
    assert resumed.status == "completed"
    assert len(calls) == 1


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


def test_a_run_can_be_restricted_to_some_cases(tmp_path: Path):
    """A working split and a held-out split of one eval keep one identity."""
    from hiveloom.eval_runner import case_key_for

    eval_file, probe = _fixture(tmp_path, case_count=4)
    subset = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        case_ids={"case-0", "case-2"},
    )
    assert subset.case_ids == ["case-0", "case-2"]
    assert {cell.case_key for cell in subset.cells} == {
        case_key_for("case-0"), case_key_for("case-2")
    }
    rest = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        case_ids=["case-1", "case-3"],
    )
    assert rest.eval_identity == subset.eval_identity
    with pytest.raises(ValueError, match="no case"):
        run_eval(eval_file, execute_cell=_completed_result, model_probe=probe,
                 case_ids={"case-9"})


def test_a_restricted_run_resumes_only_its_own_cases(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=4)
    partial = run_eval(
        eval_file, execute_cell=_completed_result, model_probe=probe,
        case_ids={"case-1", "case-3"}, max_cells=1,
    )
    assert partial.status == "incomplete"
    done = resume_eval(partial.eval_run_id, execute_cell=_completed_result, model_probe=probe)
    assert done.status == "completed" and len(done.cells) == 2


def test_a_whole_eval_manifest_stays_readable_by_older_releases(tmp_path: Path):
    """Releases before subset runs forbid unknown manifest fields: omit case_ids when unused."""
    import json

    from hiveloom.eval_runner import manifest_path

    eval_file, probe = _fixture(tmp_path, case_count=2)
    whole = run_eval(eval_file, execute_cell=_completed_result, model_probe=probe)
    assert "case_ids" not in json.loads(manifest_path(whole.eval_run_id).read_text())
    subset = run_eval(eval_file, execute_cell=_completed_result, model_probe=probe,
                      case_ids=["case-0"])
    assert json.loads(manifest_path(subset.eval_run_id).read_text())["case_ids"] == ["case-0"]


# --------------------------------------------------------------------------- #
# Operator interrupts: Ctrl-C must not spend an infrastructure attempt
# --------------------------------------------------------------------------- #
def test_an_interrupt_records_the_cell_in_flight_without_charging_an_attempt(
    tmp_path: Path,
):
    eval_file, probe = _fixture(tmp_path, case_count=3)
    seen: list[str] = []
    release = threading.Event()

    def interrupt_first(**kwargs):
        # The operator's Ctrl-C lands while this cell's provider call is in
        # flight; the call itself is abandoned, as a real one would be.
        seen.append(kwargs["cell"].cell_id)
        os.kill(os.getpid(), signal.SIGINT)
        release.wait(timeout=10)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_eval(
            eval_file,
            execute_cell=interrupt_first,
            model_probe=probe,
            concurrency=1,
            eval_run_id="eval_interrupt_fixture",
        )

    assert len(seen) == 1
    manifest = load_eval_manifest("eval_interrupt_fixture")
    assert manifest.status == "incomplete"
    assert manifest.summary()["pending"] == 3
    [hit] = [cell for cell in manifest.cells if cell.cell_id == seen[0]]
    assert hit.status == "pending"
    assert hit.error_phase == "interrupted"
    assert hit.error_type == "KeyboardInterrupt"
    assert hit.infrastructure_attempts == 0
    assert hit.attempt_run_ids == [hit.run_id]
    for cell in manifest.cells:
        if cell.cell_id != seen[0]:
            assert cell.error_phase == ""
            assert cell.attempt_run_ids == []
    release.set()

    # The default ceiling allows exactly one attempt: the interrupted one was
    # not it, so resume runs the cell again under a distinct run id.
    completed = resume_eval(
        "eval_interrupt_fixture", execute_cell=_completed_result, model_probe=probe
    )
    assert completed.status == "completed"
    assert completed.summary()["completed"] == 3
    [hit] = [cell for cell in completed.cells if cell.cell_id == seen[0]]
    assert hit.infrastructure_attempts == 1
    assert len(hit.attempt_run_ids) == 2
    assert hit.attempt_run_ids[1] == hit.run_id
    assert hit.run_id.endswith("-a2")


@pytest.mark.parametrize("outcome", ["fails", "finishes"])
def test_a_worker_that_outlives_an_interrupt_leaves_no_mark(tmp_path: Path, outcome: str):
    eval_file, probe = _fixture(tmp_path, case_count=2)
    calls: list[str] = []
    release = threading.Event()

    def abandoned(**kwargs):
        calls.append(kwargs["cell"].run_id)
        if len(calls) == 1:
            os.kill(os.getpid(), signal.SIGINT)
            release.wait(timeout=10)
        if outcome == "finishes":
            return _completed_result(**kwargs)
        raise RuntimeError("the abandoned call failed when its socket closed")

    with pytest.raises(KeyboardInterrupt):
        run_eval(
            eval_file,
            execute_cell=abandoned,
            model_probe=probe,
            concurrency=1,
            infrastructure_retries=1,
            eval_run_id="eval_outlived_fixture",
        )
    release.set()
    _wait_for_workers()  # a retry, if the worker were going to start one, has happened

    assert len(calls) == 1
    manifest = load_eval_manifest("eval_outlived_fixture")
    assert manifest.status == "incomplete"
    [interrupted, pending] = sorted(manifest.cells, key=lambda c: c.error_phase, reverse=True)
    assert interrupted.error_phase == "interrupted"
    assert interrupted.status == "pending"  # the abandoned call's late outcome left no mark
    assert interrupted.infrastructure_attempts == 0
    assert pending.attempt_run_ids == []
    if outcome == "finishes":
        # The finished trace is picked up on resume without running the cell again.
        resumed = resume_eval(
            "eval_outlived_fixture", execute_cell=_completed_result, model_probe=probe
        )
        [recovered] = [cell for cell in resumed.cells if cell.cell_id == interrupted.cell_id]
        assert recovered.status == "completed"
        assert recovered.attempt_run_ids == [calls[0]]
        assert (recovered.error_phase, recovered.error_type, recovered.error) == ("", "", "")


def test_a_cell_an_earlier_crash_left_running_keeps_its_charge(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=2)
    first = run_eval(
        eval_file,
        execute_cell=_completed_result,
        model_probe=probe,
        max_cells=0,
        eval_run_id="eval_stale_fixture",
    )
    # What a hard kill leaves behind: both cells charged and still "running",
    # with a retry left so the resume can run them.
    first.infrastructure_retries = 1
    for cell in first.cells:
        cell.status = "running"
        cell.infrastructure_attempts = 1
        cell.attempt_run_ids = [f"eval_stale_fixture_{cell.cell_id[:12]}"]
    manifest_path("eval_stale_fixture").write_text(first.model_dump_json(indent=2))
    release = threading.Event()

    def interrupt(**kwargs):
        os.kill(os.getpid(), signal.SIGINT)
        release.wait(timeout=10)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        resume_eval(
            "eval_stale_fixture",
            execute_cell=interrupt,
            model_probe=probe,
            max_cells=None,
        )
    release.set()

    cells = {cell.error_phase: cell for cell in load_eval_manifest("eval_stale_fixture").cells}
    refunded = cells["interrupted"]
    assert refunded.status == "pending"
    assert refunded.infrastructure_attempts == 1  # only this process's charge is refunded
    assert len(refunded.attempt_run_ids) == 2
    stale = cells[""]
    assert stale.status == "running"
    assert stale.infrastructure_attempts == 1  # the crashed attempt stays charged


def test_run_ids_count_the_attempts_issued_not_the_attempts_charged(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    release = threading.Event()
    calls = 0

    def fails_then_is_interrupted(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("infrastructure failure")
        os.kill(os.getpid(), signal.SIGINT)
        release.wait(timeout=10)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_eval(
            eval_file,
            execute_cell=fails_then_is_interrupted,
            model_probe=probe,
            concurrency=1,
            infrastructure_retries=2,
            eval_run_id="eval_numbering_fixture",
        )
    release.set()
    [cell] = load_eval_manifest("eval_numbering_fixture").cells
    assert cell.error_phase == "interrupted"
    assert cell.infrastructure_attempts == 1  # the failure was charged, the interrupt was not
    assert len(cell.attempt_run_ids) == 2

    completed = resume_eval(
        "eval_numbering_fixture", execute_cell=_completed_result, model_probe=probe
    )
    [cell] = completed.cells
    assert cell.infrastructure_attempts == 2
    assert len(set(cell.attempt_run_ids)) == 3
    assert cell.run_id.endswith("-a3")


def _leave_through(monkeypatch, exits: list[int]) -> None:
    """Stand in for ``os._exit``: record the code and leave through the CLI's own exit."""

    def _exit(code: int) -> None:
        exits.append(code)
        raise typer.Exit(code)

    monkeypatch.setattr("hiveloom.cli.os._exit", _exit)


def test_eval_run_reports_how_to_resume_and_leaves_with_130(monkeypatch, tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    exits: list[int] = []
    _leave_through(monkeypatch, exits)
    monkeypatch.setattr("hiveloom.eval_runner._probe_for_eval", lambda *_args: probe)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    # The manifest exists by the time the batch is interrupted.
    monkeypatch.setattr("hiveloom.eval_runner._run_pending", interrupted)

    result = cli.invoke(app, ["eval", "run", str(eval_file), "--approve", "--json"])

    assert exits == [130]
    assert result.exit_code == 130
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "interrupted"
    assert payload["eval_run_id"].startswith("eval_")
    assert f"hiveloom eval resume {payload['eval_run_id']}" in payload["error"]


def test_an_interrupt_before_the_eval_exists_has_nothing_to_resume(monkeypatch, tmp_path: Path):
    eval_file, _probe = _fixture(tmp_path, case_count=1)
    exits: list[int] = []
    _leave_through(monkeypatch, exits)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt  # the probe, the trust prompt: before any manifest

    monkeypatch.setattr("hiveloom.eval_runner.run_eval", interrupted)

    result = cli.invoke(app, ["eval", "run", str(eval_file), "--approve", "--json"])

    assert exits == [130]
    payload = json.loads(result.stdout)
    assert payload["status"] == "interrupted"
    assert "eval_run_id" not in payload
    assert "nothing to resume" in payload["error"]


def test_a_termination_signal_stops_eval_resume_like_ctrl_c(monkeypatch, tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    finished = run_eval(eval_file, execute_cell=_completed_result, model_probe=probe)
    exits: list[int] = []
    _leave_through(monkeypatch, exits)

    def killed(*_args, **_kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        raise AssertionError("SIGTERM was not turned into an interrupt")

    monkeypatch.setattr("hiveloom.eval_runner.resume_eval", killed)
    before = {name: signal.getsignal(name) for name in (signal.SIGINT, signal.SIGTERM)}

    result = cli.invoke(app, ["eval", "resume", finished.eval_run_id, "--approve"])

    assert exits == [130]
    assert result.exit_code == 130
    assert f"hiveloom eval resume {finished.eval_run_id}" in result.output
    assert {name: signal.getsignal(name) for name in before} == before


def test_a_second_signal_leaves_at_once_without_waiting_for_the_record(monkeypatch):
    exits: list[int] = []
    armed: list[object] = []

    def _exit(code: int) -> None:
        exits.append(code)
        if not armed:
            # What a second Ctrl-C, SIGTERM or SIGHUP meets while the record
            # is written: the handlers armed by the first signal.
            armed.extend(
                signal.getsignal(name)
                for name in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            )
            raise typer.Exit(code)

    monkeypatch.setattr("hiveloom.cli.os._exit", _exit)

    with pytest.raises(typer.Exit):
        with cli_module._stop_on_signal("eval_0123456789abcdef", True):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(1)

    assert len(set(map(id, armed))) == 1
    armed[0](signal.SIGINT, None)
    assert exits == [130, 130]


def test_a_signal_the_caller_ignored_on_purpose_stays_ignored():
    # ``nohup`` ignores SIGHUP so an eval outlives its terminal.
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with cli_module._stop_on_signal("eval_0123456789abcdef", True):
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


def test_the_process_still_leaves_when_the_terminal_is_gone(monkeypatch):
    exits: list[int] = []
    _leave_through(monkeypatch, exits)

    def hung_up(*_args, **_kwargs):
        raise OSError(errno.EIO, "the terminal hung up")

    monkeypatch.setattr(cli_module._err_console, "print", hung_up)

    with pytest.raises(typer.Exit):
        with cli_module._stop_on_signal("eval_0123456789abcdef", False):
            raise KeyboardInterrupt

    assert exits == [130]


def test_an_abort_at_the_trust_prompt_leaves_with_130_and_nothing_to_resume(
    monkeypatch, tmp_path: Path
):
    eval_file, _probe = _fixture(tmp_path, case_count=1)
    exits: list[int] = []
    _leave_through(monkeypatch, exits)
    # Treat the harness as untrusted so the real prompt runs. With no stdin the
    # prompt ends the way click ends it on Ctrl-C: with click's Abort, not a
    # KeyboardInterrupt.
    monkeypatch.setattr(
        "hiveloom.trust.ensure_trusted",
        lambda base, approve, *_args, **_kwargs: approve(str(base)) if approve else None,
    )

    result = cli.invoke(app, ["eval", "run", str(eval_file)])

    assert "Trust this harness folder?" in result.output
    assert exits == [130]
    assert result.exit_code == 130
    assert "nothing to resume" in result.output
    assert "Abort" not in result.output
def test_resume_can_raise_retry_ceiling_without_reexecuting_completed_cells(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=2)
    attempts: list[str] = []

    def unavailable(**kwargs):
        if kwargs['case'].id == 'case-0':
            attempts.append(kwargs['cell'].run_id)
            raise ConnectionError('synthetic endpoint outage')
        return _completed_result(**kwargs)

    partial = run_eval(eval_file, execute_cell=unavailable, model_probe=probe,
                       infrastructure_retries=1)
    completed = next(cell for cell in partial.cells if cell.status == 'completed')
    completed_receipt = completed.model_dump(mode='json')
    unchanged = resume_eval(partial.eval_run_id, execute_cell=unavailable, model_probe=probe)
    assert unchanged.status == 'incomplete' and len(attempts) == 2

    def repaired(**kwargs):
        assert kwargs['case'].id == 'case-0'
        attempts.append(kwargs['cell'].run_id)
        return _completed_result(**kwargs)

    resumed = resume_eval(partial.eval_run_id, execute_cell=repaired, model_probe=probe,
                          infrastructure_retries=2)
    assert resumed.status == 'completed'
    assert resumed.infrastructure_retries == 2
    recovered = next(cell for cell in resumed.cells if cell.cell_id != completed.cell_id)
    assert recovered.infrastructure_attempts == 3
    assert recovered.attempt_run_ids == attempts and attempts[-1].endswith('-a3')
    assert next(cell for cell in resumed.cells if cell.cell_id == completed.cell_id).model_dump(
        mode='json'
    ) == completed_receipt
    assert resume_eval(partial.eval_run_id, execute_cell=repaired, model_probe=probe,
                       infrastructure_retries=2).status == 'completed'
    assert len(attempts) == 3


def test_resume_retry_ceiling_is_absolute_persisted_and_not_a_counter_reset(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    attempts = []

    def unavailable(**kwargs):
        attempts.append(kwargs['cell'].run_id)
        raise ConnectionError('synthetic endpoint outage')

    failed = run_eval(eval_file, execute_cell=unavailable, model_probe=probe,
                      infrastructure_retries=1)
    granted = resume_eval(failed.eval_run_id, execute_cell=unavailable, model_probe=probe,
                          infrastructure_retries=3, max_cells=0)
    assert granted.infrastructure_retries == 3 and len(attempts) == 2
    assert load_eval_manifest(failed.eval_run_id).infrastructure_retries == 3
    still_failed = resume_eval(failed.eval_run_id, execute_cell=unavailable, model_probe=probe)
    assert still_failed.status == 'incomplete' and len(attempts) == 4
    again = resume_eval(failed.eval_run_id, execute_cell=unavailable, model_probe=probe,
                        infrastructure_retries=3)
    assert again.status == 'incomplete' and len(attempts) == 4
    assert again.cells[0].attempt_run_ids == attempts


@pytest.mark.parametrize('budget', [-1, 21, True, 1.5, '2', 0])
def test_resume_rejects_invalid_or_lower_retry_ceiling_without_writing(
    tmp_path: Path, budget: object
):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    partial = run_eval(eval_file, execute_cell=_completed_result, model_probe=probe,
                       infrastructure_retries=1, max_cells=0)
    before = manifest_path(partial.eval_run_id).read_bytes()
    with pytest.raises(ValueError, match='infrastructure retries'):
        resume_eval(partial.eval_run_id, execute_cell=_completed_result, model_probe=probe,
                    infrastructure_retries=budget)
    assert manifest_path(partial.eval_run_id).read_bytes() == before


def test_retry_ceiling_is_not_written_when_resume_identity_is_rejected(tmp_path: Path):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    partial = run_eval(eval_file, execute_cell=_completed_result, model_probe=probe,
                       infrastructure_retries=1, max_cells=0)
    before = manifest_path(partial.eval_run_id).read_bytes()
    construct.set_value(tmp_path / 'harness', 'system_prompt', 'Changed behavior.')
    with pytest.raises(ValueError, match='harness behavior changed'):
        resume_eval(partial.eval_run_id, execute_cell=_completed_result, model_probe=probe,
                    infrastructure_retries=2)
    assert manifest_path(partial.eval_run_id).read_bytes() == before


def test_eval_resume_cli_forwards_the_explicit_retry_ceiling(tmp_path: Path, monkeypatch):
    eval_file, probe = _fixture(tmp_path, case_count=1)
    partial = run_eval(eval_file, execute_cell=_completed_result, model_probe=probe,
                       infrastructure_retries=1)
    received = []

    def resume(run_id, **kwargs):
        received.append((run_id, kwargs['infrastructure_retries']))
        return partial

    monkeypatch.setattr(eval_runner, 'resume_eval', resume)
    result = cli.invoke(app, ['eval', 'resume', partial.eval_run_id,
                             '--infrastructure-retries', '3', '--json'])
    assert result.exit_code == 0, result.output
    assert received == [(partial.eval_run_id, 3)]
    result = cli.invoke(app, ['eval', 'resume', partial.eval_run_id, '--json'])
    assert result.exit_code == 0, result.output
    assert received[-1] == (partial.eval_run_id, None)
    result = cli.invoke(app, ['eval', 'resume', partial.eval_run_id,
                             '--infrastructure-retries', '21', '--json'])
    assert result.exit_code != 0 and len(received) == 2


def test_apply_result_never_keeps_an_earlier_attempts_call_models():
    call = ModelCallIdentity(
        turn=1, phase="main", provider="fake", requested_model="m", effective_model="m"
    )
    cell = eval_runner.EvalCell(
        cell_id="c", case_key="k", case_digest="d", repetition=0, call_models=[call]
    )

    eval_runner._apply_result(cell, RunResult(status="success", run_id="r2"))
    assert cell.call_models is None

    execution = RunExecutionEnvelope(run_id="r3", status="success", call_models=[call])
    eval_runner._apply_result(cell, RunResult(status="success", run_id="r3",
                                              execution=execution))
    assert cell.call_models == [call]


def test_apply_result_without_an_envelope_does_not_keep_an_earlier_model():
    """A stale effective_model would hide a modelless cell from the report."""
    from hiveloom.eval_reports import _cell_identities

    cell = eval_runner.EvalCell(
        cell_id="c", case_key="k", case_digest="d", repetition=0,
        requested_provider="fake", requested_model="old-model",
        effective_provider="fake", effective_model="old-model",
        execution_fingerprint="fp-old",
    )

    eval_runner._apply_result(cell, RunResult(status="success", run_id="r2"))

    assert cell.status == "ran"
    assert cell.effective_model is None
    assert cell.effective_provider is None
    assert cell.requested_model == ""
    assert cell.execution_fingerprint == ""
    _identities, missing = _cell_identities([cell.model_dump(mode="json")])
    assert [(item["cell_id"], item["reason"]) for item in missing] == [
        ("c", "no_effective_model")
    ]
