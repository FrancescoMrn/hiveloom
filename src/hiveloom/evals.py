"""Versioned local evaluation contracts and scorer execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hiveloom import catalog, ext, trust
from hiveloom.errors import SpecError
from hiveloom.ext import BuildContext
from hiveloom.logging.hive import Hive
from hiveloom.loop.agent_loop import RunResult
from hiveloom.metrics import RunMetric, record_run_metrics
from hiveloom.spec.loader import load_spec

EVAL_SCHEMA_VERSION = 1


class DatasetLoader(Protocol):
    """Public protocol returned by a registered dataset factory."""

    def load(self) -> Iterable[EvalCase | dict[str, Any]]:
        """Return the complete local case set."""


class Scorer(Protocol):
    """Public protocol returned by a registered scorer factory."""

    def score(self, context: ScorerContext) -> ScorerOutput:
        """Produce validated metrics and diagnostics after a run."""


def _canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SpecError("eval data must contain only finite JSON-safe values") from exc
    return hashlib.sha256(encoded).hexdigest()


class EvalCase(BaseModel):
    """One private evaluator input and its held-out expected data."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    input: str
    expected: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetSpec(BaseModel):
    """A registered dataset loader plus its construction parameters."""

    model_config = ConfigDict(extra="forbid")

    loader: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    include_expected_in_input: bool = False

    @model_validator(mode="after")
    def _registered_loader(self) -> DatasetSpec:
        entry = catalog.DATASETS.get(self.loader)
        if entry is None:
            available = ", ".join(sorted(catalog.DATASETS)) or "none"
            raise ValueError(
                f"unknown dataset loader '{self.loader}' (available: {available})"
            )
        problems = catalog.validate_builtin_params(entry, self.params)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ScorerSpec(BaseModel):
    """A registered scorer plus its construction parameters."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _registered_scorer(self) -> ScorerSpec:
        entry = catalog.SCORERS.get(self.name)
        if entry is None:
            available = ", ".join(sorted(catalog.SCORERS)) or "none"
            raise ValueError(f"unknown scorer '{self.name}' (available: {available})")
        problems = catalog.validate_builtin_params(entry, self.params)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class EvalSpec(BaseModel):
    """Version one of the local, reproducible evaluation document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = EVAL_SCHEMA_VERSION
    harness: str = Field(min_length=1)
    dataset: DatasetSpec
    scorers: list[ScorerSpec] = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1, le=10_000)
    extensions: list[str] = Field(default_factory=list)

    @field_validator("scorers", mode="before")
    @classmethod
    def _short_scorer_refs(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [{"name": item} if isinstance(item, str) else item for item in value]


class ScorerDiagnostic(BaseModel):
    """A bounded, structured scorer observation separate from run status."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(default="", max_length=1000)
    severity: Literal["info", "warning", "error"] = "info"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _json_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                value, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("diagnostic metadata must be JSON-safe") from exc
        if len(encoded) > 16 * 1024:
            raise ValueError("diagnostic metadata exceeds the 16384-byte limit")
        return value


class ScorerOutput(BaseModel):
    """Validated return value from one scorer."""

    model_config = ConfigDict(extra="forbid")

    metrics: list[RunMetric] = Field(default_factory=list)
    diagnostics: list[ScorerDiagnostic] = Field(default_factory=list)


class ScorerContext(BaseModel):
    """Read-only public inputs available after Hiveloom verification."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    case_id: str
    case_input: str
    expected: Any = None
    case_metadata: dict[str, Any] = Field(default_factory=dict)
    run_result: RunResult
    verification_context: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ScorerReceipt(BaseModel):
    """Whether one scorer ran, independent of the model run's status."""

    name: str
    status: Literal["success", "error"]
    metric_count: int = 0
    diagnostic_count: int = 0
    error_type: str | None = None
    error: str | None = None


class ScoringResult(BaseModel):
    """All scoring receipts and validated signals for one completed run."""

    run_id: str
    run_status: str
    status: Literal["success", "partial", "error"]
    metrics: list[RunMetric] = Field(default_factory=list)
    diagnostics: list[ScorerDiagnostic] = Field(default_factory=list)
    scorers: list[ScorerReceipt] = Field(default_factory=list)
    ingestion: dict[str, int] | None = None


class EvalIdentity(BaseModel):
    """Content receipts that make a local eval batch safe to resume."""

    spec_digest: str
    dataset_digest: str
    scorer_digest: str
    eval_id: str


class ValidatedEval(BaseModel):
    """A resolved eval contract without exposing its private cases."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: EvalSpec
    path: Path
    harness_path: Path
    case_count: int
    identity: EvalIdentity


def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"invalid eval spec ({source}):"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def eval_spec_from_dict(
    data: dict[str, Any],
    *,
    source: str = "<dict>",
    base_dir: Path | None = None,
    approve_trust=None,
) -> EvalSpec:
    """Load declared eval extensions before validating their catalog refs."""
    ext.ensure_environment_loaded()
    declared = data.get("extensions") or []
    if declared and base_dir is None:
        raise SpecError("eval extensions require a source directory")
    if declared:
        assert base_dir is not None
        trust.ensure_trusted(base_dir, approve_trust)
        ext.load_harness_extensions(declared, base_dir)
    try:
        return EvalSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(_format_validation_error(exc, source)) from exc


def load_eval_spec(path: str | Path, *, approve_trust=None) -> EvalSpec:
    """Read and validate an eval YAML document."""
    eval_path = Path(path)
    if not eval_path.is_file():
        raise SpecError(f"eval spec not found: {eval_path}")
    try:
        data = yaml.safe_load(eval_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"could not parse eval YAML in {eval_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{eval_path} must contain a YAML mapping at the top level")
    return eval_spec_from_dict(
        data,
        source=str(eval_path),
        base_dir=eval_path.parent,
        approve_trust=approve_trust,
    )


def load_eval_cases(spec: EvalSpec, base_dir: str | Path) -> list[EvalCase]:
    """Run a registered loader and validate every case before execution."""
    base = Path(base_dir)
    try:
        loader = ext.build(
            "datasets", spec.dataset.loader, spec.dataset.params, BuildContext(base=base)
        )
        if hasattr(loader, "load"):
            values = loader.load()
        elif callable(loader):
            values = loader()
        else:
            values = loader
        if not isinstance(values, Iterable):
            raise TypeError("dataset loader must return an iterable")
        cases = [
            item if isinstance(item, EvalCase) else EvalCase.model_validate(item)
            for item in values
        ]
    except SpecError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize extension errors at the boundary
        raise SpecError(
            f"dataset loader '{spec.dataset.loader}' failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not cases:
        raise SpecError(f"dataset loader '{spec.dataset.loader}' returned no cases")
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise SpecError(f"dataset loader '{spec.dataset.loader}' returned duplicate case ids")
    _canonical_digest([case.model_dump(mode="json") for case in cases])
    return cases


def case_model_input(case: EvalCase, dataset: DatasetSpec) -> str:
    """Build the executor input, excluding expected data unless explicitly enabled."""
    if not dataset.include_expected_in_input:
        return case.input
    expected = json.dumps(case.expected, allow_nan=False, sort_keys=True)
    return f"{case.input}\n\n<eval_expected_data>\n{expected}\n</eval_expected_data>"


def eval_identity(spec: EvalSpec, cases: list[EvalCase]) -> EvalIdentity:
    """Hash the document, loaded cases, and registered scorer implementations."""
    spec_digest = _canonical_digest(spec.model_dump(mode="json"))
    dataset_digest = _canonical_digest(
        {
            "component": ext.component_digest("datasets", spec.dataset.loader),
            "cases": [case.model_dump(mode="json") for case in cases],
        }
    )
    scorer_digest = _canonical_digest(
        [
            {
                "ref": scorer.model_dump(mode="json"),
                "component": ext.component_digest("scorers", scorer.name),
            }
            for scorer in spec.scorers
        ]
    )
    eval_id = _canonical_digest(
        {
            "spec_digest": spec_digest,
            "dataset_digest": dataset_digest,
            "scorer_digest": scorer_digest,
        }
    )
    return EvalIdentity(
        spec_digest=spec_digest,
        dataset_digest=dataset_digest,
        scorer_digest=scorer_digest,
        eval_id=eval_id,
    )


def validate_eval_spec(path: str | Path, *, approve_trust=None) -> ValidatedEval:
    """Resolve the harness and components, returning only aggregate receipts."""
    eval_path = Path(path).resolve()
    spec = load_eval_spec(eval_path, approve_trust=approve_trust)
    harness_path = (eval_path.parent / spec.harness).resolve()
    harness_dir = harness_path if harness_path.is_dir() else harness_path.parent
    trust.ensure_trusted(harness_dir, approve_trust)
    load_spec(harness_path)
    cases = load_eval_cases(spec, eval_path.parent)
    for scorer in spec.scorers:
        try:
            ext.build("scorers", scorer.name, scorer.params, BuildContext(base=eval_path.parent))
        except Exception as exc:  # noqa: BLE001 - normalize extension errors
            raise SpecError(
                f"scorer '{scorer.name}' could not be built: {type(exc).__name__}: {exc}"
            ) from exc
    return ValidatedEval(
        spec=spec,
        path=eval_path,
        harness_path=harness_path,
        case_count=len(cases),
        identity=eval_identity(spec, cases),
    )


def _normalize_scorer_output(value: Any) -> ScorerOutput:
    if value is None:
        return ScorerOutput()
    if isinstance(value, ScorerOutput):
        return value
    if isinstance(value, RunMetric):
        return ScorerOutput(metrics=[value])
    if isinstance(value, list):
        return ScorerOutput(metrics=value)
    return ScorerOutput.model_validate(value)


def run_scorers(
    spec: EvalSpec,
    case: EvalCase,
    run_result: RunResult,
    *,
    base_dir: str | Path = ".",
    verification_context: dict[str, Any] | None = None,
    hive: Hive | None = None,
    harness_key: str | None = None,
) -> ScoringResult:
    """Run post-verification scorers and optionally ingest their metrics."""
    context = ScorerContext(
        case_id=case.id,
        case_input=case.input,
        expected=case.expected,
        case_metadata=case.metadata,
        run_result=run_result,
        verification_context=verification_context or {},
        artifacts=run_result.artifacts,
    )
    metrics: list[RunMetric] = []
    diagnostics: list[ScorerDiagnostic] = []
    receipts: list[ScorerReceipt] = []
    for scorer_ref in spec.scorers:
        try:
            scorer = ext.build(
                "scorers",
                scorer_ref.name,
                scorer_ref.params,
                BuildContext(base=Path(base_dir)),
            )
            raw = scorer.score(context) if hasattr(scorer, "score") else scorer(context)
            output = _normalize_scorer_output(raw)
            wrong_run = next(
                (metric.run_id for metric in output.metrics if metric.run_id != run_result.run_id),
                None,
            )
            if wrong_run is not None:
                raise ValueError(
                    f"returned metric for run {wrong_run!r}; expected {run_result.run_id!r}"
                )
            metrics.extend(output.metrics)
            diagnostics.extend(output.diagnostics)
            receipts.append(
                ScorerReceipt(
                    name=scorer_ref.name,
                    status="success",
                    metric_count=len(output.metrics),
                    diagnostic_count=len(output.diagnostics),
                )
            )
        except Exception as exc:  # noqa: BLE001 - one scorer must not hide run status
            message = str(exc)[:1000]
            diagnostic = ScorerDiagnostic(
                code="scorer_exception",
                message=f"{scorer_ref.name}: {type(exc).__name__}: {message}",
                severity="error",
            )
            diagnostics.append(diagnostic)
            receipts.append(
                ScorerReceipt(
                    name=scorer_ref.name,
                    status="error",
                    error_type=type(exc).__name__,
                    error=message,
                )
            )
    ingestion = None
    if hive is not None:
        if harness_key is None:
            raise ValueError("harness_key is required when ingesting scorer metrics")
        ingestion = record_run_metrics(hive, harness_key, metrics)
    errors = sum(receipt.status == "error" for receipt in receipts)
    status = "success" if errors == 0 else "error" if errors == len(receipts) else "partial"
    return ScoringResult(
        run_id=run_result.run_id,
        run_status=run_result.status,
        status=status,
        metrics=metrics,
        diagnostics=diagnostics,
        scorers=receipts,
        ingestion=ingestion,
    )
