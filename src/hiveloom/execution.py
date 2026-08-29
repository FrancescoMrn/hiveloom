"""Public, serializable provenance for one completed harness run."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from hiveloom.models.provider import Usage


class VerificationSummary(BaseModel):
    """Whether the first answer was clean, recovered, or remained invalid."""

    attempts: int = 0
    first_pass_valid: bool | None = None
    recovery_attempted: bool = False
    recovered: bool = False
    final_status: Literal["passed", "failed", "not_run"] = "not_run"


class RunExecutionEnvelope(BaseModel):
    """What was requested, what executed, and what the run consumed."""

    run_id: str
    status: str
    harness_id: str = ""
    harness_name: str = ""
    schema_version: str = ""
    behavior_hash: str = ""
    execution_fingerprint: str = ""
    hiveloom_version: str = ""
    requested_provider: str = ""
    requested_model: str = ""
    resolved_provider: str = ""
    resolved_model: str = ""
    effective_provider: str | None = None
    effective_model: str | None = None
    models_used: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    cost_source: Literal["billed", "estimated", "mixed", "none"] = "none"
    verification: VerificationSummary = Field(default_factory=VerificationSummary)
    trace_path: str = ""


def execution_fingerprint(
    *,
    behavior_hash: str,
    hiveloom_version: str,
    schema_version: str,
    runtime_config: dict[str, Any],
    input_value: str,
    models_used: list[dict[str, Any]],
    effective_models: list[str],
    lineage: dict[str, Any] | None,
) -> str:
    """Hash reproducible execution inputs without exposing the task text."""
    payload = {
        "behavior_hash": behavior_hash,
        "hiveloom_version": hiveloom_version,
        "schema_version": schema_version,
        "runtime_config": runtime_config,
        "input_hash": hashlib.sha256(input_value.encode("utf-8")).hexdigest(),
        "models_used": models_used,
        "effective_models": effective_models,
        "lineage": lineage or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
