"""Validated numeric signals attached to indexed runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hiveloom.errors import SpecError
from hiveloom.logging.hive import Hive

METRIC_METADATA_MAX_BYTES = 16 * 1024
_METRIC_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")


class RunMetric(BaseModel):
    """One immutable numeric observation anchored to a run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=200)
    name: str
    value: float
    direction: Literal["maximize", "minimize"]
    unit: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=128)
    scope: Literal["case", "run", "eval"] = "run"
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _METRIC_NAME_RE.fullmatch(value):
            raise ValueError("metric name must match [A-Za-z][A-Za-z0-9_.-]{0,127}")
        return value

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
        return value

    @field_validator("unit", "source")
    @classmethod
    def _trim_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("metric unit and source cannot be blank")
        return normalized

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                value,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("metric metadata must contain only JSON-safe values") from exc
        if len(encoded) > METRIC_METADATA_MAX_BYTES:
            raise ValueError(
                f"metric metadata exceeds the {METRIC_METADATA_MAX_BYTES}-byte limit"
            )
        return value

    def resolved_idempotency_key(self) -> str:
        """Caller key, or a stable logical key for one metric per run/source/scope."""
        if self.idempotency_key is not None:
            return self.idempotency_key
        material = json.dumps(
            {
                "run_id": self.run_id,
                "name": self.name,
                "source": self.source,
                "scope": self.scope,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "metric_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def storage_row(self, *, recorded_at: str) -> dict[str, Any]:
        """Canonical SQLite row used for equality checks and ingestion."""
        return {
            "idempotency_key": self.resolved_idempotency_key(),
            "run_id": self.run_id,
            "name": self.name,
            "value": self.value,
            "direction": self.direction,
            "unit": self.unit,
            "source": self.source,
            "scope": self.scope,
            "metadata_json": json.dumps(
                self.metadata,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "recorded_at": recorded_at,
        }


def record_run_metrics(
    hive: Hive,
    harness_key: str,
    metrics: list[RunMetric],
) -> dict[str, int]:
    """Validate-first, transactional metric ingestion for SDK callers."""
    recorded_at = datetime.now(UTC).isoformat()
    rows = [metric.storage_row(recorded_at=recorded_at) for metric in metrics]
    return hive.record_metrics(harness_key, rows)


def load_metrics_ndjson(path: str | Path) -> list[RunMetric]:
    """Parse a complete NDJSON file before the caller opens a transaction."""
    source = Path(path)
    if not source.is_file():
        raise SpecError(f"metrics import file not found: {source}")
    metrics: list[RunMetric] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SpecError(
                f"invalid metrics NDJSON at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise SpecError(
                f"invalid metrics NDJSON at line {line_number}: expected an object"
            )
        try:
            metrics.append(RunMetric.model_validate(value))
        except ValidationError as exc:
            problem = exc.errors()[0]
            location = ".".join(str(part) for part in problem["loc"])
            raise SpecError(
                f"invalid metric at line {line_number} ({location}): {problem['msg']}"
            ) from exc
    return metrics
