"""Atomic, resumable execution of versioned local eval documents."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hiveloom import ext, runner
from hiveloom.evals import (
    EvalCase,
    EvalIdentity,
    EvalSpec,
    ScoringResult,
    ValidatedEval,
    case_model_input,
    resolve_eval_spec,
    run_scorers,
)
from hiveloom.execution import RunExecutionEnvelope, VerificationSummary
from hiveloom.logging.hive import Hive
from hiveloom.logging.journal import read_events
from hiveloom.logging.trace import spec_version_hash
from hiveloom.loop.agent_loop import RunResult
from hiveloom.models.capabilities import (
    ModelProbeResult,
    probe_model,
    require_compatible_probe,
)
from hiveloom.paths import hiveloom_home
from hiveloom.spec.loader import atomic_write_text, load_spec
from hiveloom.spec.schema import ModelConfig
from hiveloom.verify.base import VerdictResult

_EVAL_RUN_RE = re.compile(r"eval_[A-Za-z0-9_-]{8,80}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvalCell(BaseModel):
    """Durable state for one case and repetition."""

    model_config = ConfigDict(extra="forbid")

    cell_id: str
    case_key: str
    case_digest: str
    repetition: int
    status: Literal[
        "pending", "running", "ran", "completed", "infrastructure_error"
    ] = "pending"
    run_id: str = ""
    attempt_run_ids: list[str] = Field(default_factory=list)
    infrastructure_attempts: int = 0
    run_status: str = ""
    scorer_status: str = "not_run"
    metric_ingestion: dict[str, Any] = Field(default_factory=dict)
    requested_provider: str = ""
    requested_model: str = ""
    effective_provider: str | None = None
    effective_model: str | None = None
    execution_fingerprint: str = ""
    duration_ms: int = 0
    cost_usd: float = 0.0
    cost_source: Literal["billed", "estimated", "mixed", "none"] = "none"
    verification: VerificationSummary = Field(default_factory=VerificationSummary)
    trace_path: str = ""
    trace_disabled: bool = False
    error_phase: str = ""
    error_type: str = ""
    error: str = ""
    started_at: str | None = None
    finished_at: str | None = None


class EvalManifest(BaseModel):
    """Atomic local checkpoint for one model/case/repetition matrix."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    eval_run_id: str
    status: Literal["running", "incomplete", "completed"] = "running"
    eval_spec_path: str
    harness_path: str
    harness_id: str = ""
    eval_identity: EvalIdentity
    harness_behavior_hash: str
    requested_provider: str
    requested_model: str
    repetitions: int
    concurrency: int
    infrastructure_retries: int
    model_probe: ModelProbeResult
    trace_root: str
    cells: list[EvalCell]
    created_at: str
    updated_at: str

    def summary(self) -> dict[str, int]:
        counts = {
            "total": len(self.cells),
            "pending": 0,
            "running": 0,
            "ran": 0,
            "completed": 0,
            "infrastructure_error": 0,
        }
        for cell in self.cells:
            counts[cell.status] += 1
        return counts


CellExecutor = Callable[..., RunResult]


def new_eval_run_id() -> str:
    return f"eval_{uuid.uuid4().hex[:16]}"


def _eval_dir(eval_run_id: str) -> Path:
    if not _EVAL_RUN_RE.fullmatch(eval_run_id):
        raise ValueError("invalid eval run id")
    return hiveloom_home() / "evals" / eval_run_id


def manifest_path(eval_run_id: str) -> Path:
    return _eval_dir(eval_run_id) / "manifest.json"


def load_eval_manifest(eval_run_id: str) -> EvalManifest:
    path = manifest_path(eval_run_id)
    if not path.is_file():
        raise ValueError(f"eval run not found: {eval_run_id}")
    return EvalManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _save_manifest(manifest: EvalManifest, lock: threading.Lock) -> None:
    with lock:
        manifest.updated_at = _now()
        path = manifest_path(manifest.eval_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, manifest.model_dump_json(indent=2) + "\n")
        with Hive() as hive:
            hive.upsert_eval_manifest(manifest.model_dump(mode="json"), str(path))


def _case_key(case: EvalCase) -> str:
    return _digest(case.id)[:20]


def _case_digest(case: EvalCase) -> str:
    return _digest(case.model_dump(mode="json"))


def _resolve_model(
    validated: ValidatedEval,
    model_override: str | None,
    provider_override: str | None,
) -> ModelConfig:
    harness = load_spec(validated.harness_path)
    return ModelConfig(
        provider=provider_override or harness.model.provider,
        id=model_override or harness.model.id,
        max_tokens=harness.model.max_tokens,
        temperature=harness.model.temperature,
    )


def _harness_base(path: str | Path) -> Path:
    resolved = Path(path)
    return resolved if resolved.is_dir() else resolved.parent


def _probe_for_eval(
    validated: ValidatedEval,
    selected: ModelConfig,
    supplied: ModelProbeResult | None,
) -> ModelProbeResult:
    if supplied is not None:
        result = supplied
    else:
        provider = ext.build_provider(
            selected.provider, _harness_base(validated.harness_path)
        )
        result = probe_model(
            selected.provider,
            selected.id,
            provider=provider,
            live=True,
            policy=validated.spec.model_identity,
            aliases=validated.spec.model_aliases,
        )
    if result.requested_provider != selected.provider or result.requested_model != selected.id:
        raise ValueError("model probe does not match the selected eval provider/model")
    require_compatible_probe(result)
    return result


def _build_cells(
    eval_run_id: str,
    identity: EvalIdentity,
    cases: list[EvalCase],
    repetitions: int,
    selected: ModelConfig,
) -> list[EvalCell]:
    cells: list[EvalCell] = []
    seen_keys: set[str] = set()
    for case in sorted(cases, key=_case_key):
        key = _case_key(case)
        if key in seen_keys:
            raise ValueError("case identity digest collision")
        seen_keys.add(key)
        for repetition in range(repetitions):
            cell_id = _digest(
                {
                    "eval": identity.eval_id,
                    "case": key,
                    "repetition": repetition,
                    "provider": selected.provider,
                    "model": selected.id,
                }
            )[:20]
            cells.append(
                EvalCell(
                    cell_id=cell_id,
                    case_key=key,
                    case_digest=_case_digest(case),
                    repetition=repetition,
                    requested_provider=selected.provider,
                    requested_model=selected.id,
                    run_id=f"{eval_run_id}_{cell_id[:12]}",
                )
            )
    return cells


def _result_from_trace(path: str | Path) -> RunResult | None:
    trace_path = Path(path)
    if not trace_path.is_file():
        return None
    events = read_events(trace_path)
    finished = next(
        (event for event in reversed(events) if event.get("type") == "run_finished"),
        None,
    )
    if finished is None:
        return None
    payload = finished.get("payload") or {}
    execution = payload.get("execution")
    return RunResult(
        status=str(payload.get("status") or "error"),
        output=str(payload.get("output") or ""),
        turns=int(payload.get("turns") or 0),
        cost_usd=float(payload.get("cost_usd") or 0.0),
        duration_seconds=float(payload.get("duration_seconds") or 0.0),
        run_id=str(finished.get("run_id") or trace_path.stem),
        trace_path=str(trace_path),
        verdicts=[
            VerdictResult.model_validate(value)
            for value in payload.get("verdicts") or []
        ],
        reason=str(payload.get("reason") or ""),
        artifacts=list(payload.get("artifacts") or []),
        provider_calls=list(payload.get("provider_calls") or []),
        execution=(
            RunExecutionEnvelope.model_validate(execution)
            if isinstance(execution, dict)
            else None
        ),
    )


def _default_execute(
    *,
    manifest: EvalManifest,
    cell: EvalCell,
    case: EvalCase,
    spec: EvalSpec,
) -> RunResult:
    return runner.run_harness(
        manifest.harness_path,
        case_model_input(case, spec.dataset),
        literal_input=True,
        run_id=cell.run_id,
        trace_dir=manifest.trace_root,
        model_override=manifest.requested_model,
        provider_override=manifest.requested_provider,
    )


def _apply_result(cell: EvalCell, result: RunResult) -> None:
    cell.run_id = result.run_id or cell.run_id
    cell.run_status = result.status
    cell.trace_path = result.trace_path or ""
    cell.trace_disabled = not bool(result.trace_path)
    cell.duration_ms = round(result.duration_seconds * 1000)
    cell.cost_usd = result.cost_usd
    if result.execution is not None:
        cell.requested_provider = result.execution.requested_provider
        cell.requested_model = result.execution.requested_model
        cell.effective_provider = result.execution.effective_provider
        cell.effective_model = result.execution.effective_model
        cell.execution_fingerprint = result.execution.execution_fingerprint
        cell.duration_ms = result.execution.duration_ms
        cell.cost_usd = result.execution.cost_usd
        cell.cost_source = result.execution.cost_source
        cell.verification = result.execution.verification
    cell.status = "ran"
    cell.error_phase = ""
    cell.error_type = ""
    cell.error = ""


def _score_cell(
    manifest: EvalManifest,
    cell: EvalCell,
    case: EvalCase,
    spec: EvalSpec,
    result: RunResult,
) -> ScoringResult:
    if result.trace_path:
        with Hive() as hive:
            hive.ingest_trace_file(result.trace_path)
            harness = load_spec(manifest.harness_path)
            scoring = run_scorers(
                spec,
                case,
                result,
                base_dir=Path(manifest.eval_spec_path).parent,
                hive=hive,
                harness_key=harness.identity,
            )
    else:
        scoring = run_scorers(
            spec,
            case,
            result,
            base_dir=Path(manifest.eval_spec_path).parent,
        )
    return scoring


def _apply_scoring(cell: EvalCell, scoring: ScoringResult) -> None:
    cell.scorer_status = scoring.status
    cell.metric_ingestion = scoring.ingestion or {
        "received": len(scoring.metrics),
        "inserted": 0,
        "duplicates": 0,
        "state": "trace_disabled",
    }
    cell.status = "completed"
    cell.finished_at = _now()


def _run_one_cell(
    manifest: EvalManifest,
    cell: EvalCell,
    case: EvalCase,
    spec: EvalSpec,
    execute: CellExecutor,
    lock: threading.Lock,
) -> None:
    recovered = _result_from_trace(cell.trace_path) if cell.trace_path else None
    if recovered is not None:
        with lock:
            _apply_result(cell, recovered)
        _save_manifest(manifest, lock)
    elif cell.status == "ran":
        with lock:
            cell.status = "infrastructure_error"
            cell.error_phase = "recovery"
            cell.error_type = "MissingTrace"
            cell.error = "completed run cannot be reconstructed from its trace"
        _save_manifest(manifest, lock)

    if cell.status == "ran" and recovered is not None:
        try:
            scoring = _score_cell(manifest, cell, case, spec, recovered)
        except Exception as exc:  # noqa: BLE001 - scoring can resume without rebilling
            with lock:
                cell.error_phase = "scoring"
                cell.error_type = type(exc).__name__
                cell.error = str(exc)[:1000]
            _save_manifest(manifest, lock)
            return
        with lock:
            _apply_scoring(cell, scoring)
        _save_manifest(manifest, lock)
        return

    maximum_attempts = manifest.infrastructure_retries + 1
    while cell.infrastructure_attempts < maximum_attempts:
        with lock:
            cell.infrastructure_attempts += 1
            attempt = cell.infrastructure_attempts
            base_run_id = f"{manifest.eval_run_id}_{cell.cell_id[:12]}"
            cell.run_id = base_run_id if attempt == 1 else f"{base_run_id}-a{attempt}"
            cell.attempt_run_ids.append(cell.run_id)
            cell.trace_path = str(Path(manifest.trace_root) / f"{cell.run_id}.jsonl")
            cell.trace_disabled = False
            cell.status = "running"
            cell.started_at = cell.started_at or _now()
            cell.error_phase = ""
            cell.error_type = ""
            cell.error = ""
        _save_manifest(manifest, lock)
        try:
            result = execute(manifest=manifest, cell=cell, case=case, spec=spec)
        except Exception as exc:  # noqa: BLE001 - infrastructure retry boundary
            recovered = _result_from_trace(cell.trace_path)
            if recovered is not None:
                result = recovered
            else:
                with lock:
                    cell.status = "infrastructure_error"
                    cell.error_phase = "execution"
                    cell.error_type = type(exc).__name__
                    cell.error = str(exc)[:1000]
                _save_manifest(manifest, lock)
                continue
        with lock:
            _apply_result(cell, result)
        _save_manifest(manifest, lock)
        try:
            scoring = _score_cell(manifest, cell, case, spec, result)
        except Exception as exc:  # noqa: BLE001 - preserve the completed model call
            with lock:
                cell.error_phase = "scoring"
                cell.error_type = type(exc).__name__
                cell.error = str(exc)[:1000]
            _save_manifest(manifest, lock)
            return
        with lock:
            _apply_scoring(cell, scoring)
        _save_manifest(manifest, lock)
        return
    if cell.status == "running":
        with lock:
            cell.status = "infrastructure_error"
            cell.error_phase = "execution"
            cell.error_type = "InterruptedAttempt"
            cell.error = "incomplete prior attempt exhausted the infrastructure retry limit"
        _save_manifest(manifest, lock)


def _case_map(cases: list[EvalCase]) -> dict[str, EvalCase]:
    return {_case_key(case): case for case in cases}


def _run_pending(
    manifest: EvalManifest,
    spec: EvalSpec,
    cases: list[EvalCase],
    *,
    execute_cell: CellExecutor | None,
    max_cells: int | None,
) -> EvalManifest:
    execute = execute_cell or _default_execute
    case_by_key = _case_map(cases)
    eligible = [cell for cell in manifest.cells if cell.status != "completed"]
    eligible.sort(key=lambda cell: (cell.case_key, cell.repetition))
    if max_cells is not None:
        eligible = eligible[:max_cells]
    lock = threading.Lock()
    manifest.status = "running"
    _save_manifest(manifest, lock)
    with ThreadPoolExecutor(max_workers=manifest.concurrency) as pool:
        futures = [
            pool.submit(
                _run_one_cell,
                manifest,
                cell,
                case_by_key[cell.case_key],
                spec,
                execute,
                lock,
            )
            for cell in eligible
        ]
        for future in futures:
            future.result()
    manifest.status = (
        "completed"
        if all(cell.status == "completed" for cell in manifest.cells)
        else "incomplete"
    )
    _save_manifest(manifest, lock)
    return manifest


def run_eval(
    path: str | Path,
    *,
    model_override: str | None = None,
    provider_override: str | None = None,
    repetitions: int | None = None,
    concurrency: int = 1,
    infrastructure_retries: int = 0,
    execute_cell: CellExecutor | None = None,
    model_probe: ModelProbeResult | None = None,
    approve_trust=None,
    eval_run_id: str | None = None,
    max_cells: int | None = None,
) -> EvalManifest:
    """Create and execute an atomic eval manifest."""
    if concurrency < 1 or concurrency > 128:
        raise ValueError("eval concurrency must be between 1 and 128")
    if infrastructure_retries < 0 or infrastructure_retries > 20:
        raise ValueError("infrastructure retries must be between 0 and 20")
    if max_cells is not None and max_cells < 0:
        raise ValueError("max_cells cannot be negative")
    validated, cases = resolve_eval_spec(path, approve_trust=approve_trust)
    selected = _resolve_model(validated, model_override, provider_override)
    probe = _probe_for_eval(validated, selected, model_probe)
    repetition_count = repetitions if repetitions is not None else validated.spec.repetitions
    if repetition_count < 1 or repetition_count > 10_000:
        raise ValueError("eval repetitions must be between 1 and 10000")
    run_id = eval_run_id or new_eval_run_id()
    root = _eval_dir(run_id)
    if manifest_path(run_id).exists():
        raise ValueError(f"eval run already exists: {run_id}")
    harness = load_spec(validated.harness_path)
    created = _now()
    manifest = EvalManifest(
        eval_run_id=run_id,
        eval_spec_path=str(validated.path),
        harness_path=str(validated.harness_path),
        harness_id=harness.identity,
        eval_identity=validated.identity,
        harness_behavior_hash=spec_version_hash(
            harness, _harness_base(validated.harness_path)
        ),
        requested_provider=selected.provider,
        requested_model=selected.id,
        repetitions=repetition_count,
        concurrency=concurrency,
        infrastructure_retries=infrastructure_retries,
        model_probe=probe,
        trace_root=str(root / "traces"),
        cells=_build_cells(
            run_id,
            validated.identity,
            cases,
            repetition_count,
            selected,
        ),
        created_at=created,
        updated_at=created,
    )
    _save_manifest(manifest, threading.Lock())
    return _run_pending(
        manifest,
        validated.spec,
        cases,
        execute_cell=execute_cell,
        max_cells=max_cells,
    )


def resume_eval(
    eval_run_id: str,
    *,
    execute_cell: CellExecutor | None = None,
    model_probe: ModelProbeResult | None = None,
    approve_trust=None,
    max_cells: int | None = None,
) -> EvalManifest:
    """Resume only unfinished cells after revalidating every content identity."""
    manifest = load_eval_manifest(eval_run_id)
    validated, cases = resolve_eval_spec(
        manifest.eval_spec_path, approve_trust=approve_trust
    )
    if validated.identity != manifest.eval_identity:
        raise ValueError("eval spec, dataset, or scorer identity changed; refusing resume")
    harness = load_spec(validated.harness_path)
    behavior = spec_version_hash(harness, _harness_base(validated.harness_path))
    if behavior != manifest.harness_behavior_hash:
        raise ValueError("harness behavior changed; refusing resume")
    selected = _resolve_model(
        validated,
        manifest.requested_model,
        manifest.requested_provider,
    )
    probe = _probe_for_eval(validated, selected, model_probe)
    if probe.adapter_digest != manifest.model_probe.adapter_digest:
        raise ValueError("provider adapter changed; refusing to mix eval executions")
    if probe.effective_models != manifest.model_probe.effective_models:
        raise ValueError("effective model identity changed; refusing to mix eval executions")
    current = _case_map(cases)
    for cell in manifest.cells:
        case = current.get(cell.case_key)
        if case is None or _case_digest(case) != cell.case_digest:
            raise ValueError("eval case identity changed; refusing resume")
    return _run_pending(
        manifest,
        validated.spec,
        cases,
        execute_cell=execute_cell,
        max_cells=max_cells,
    )
