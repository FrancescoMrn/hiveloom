"""Bounded aggregate and paired metric evidence for evolution."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive
from hiveloom.spec.schema import MetricObjective

MAX_OBSERVATIONS_PER_OBJECTIVE = 2000
MAX_SERIES_PER_OBJECTIVE = 5
MAX_COHORTS_PER_SERIES = 5
MAX_FINGERPRINTS_PER_COHORT = 10
MAX_RUN_IDS_PER_COHORT = 10
MAX_PAIR_COMPARISONS_PER_SERIES = 10


class MetricCohortAggregate(BaseModel):
    """One metric series without mixing behavior or model identity."""

    cohort_id: str
    behavior_hash: str
    requested_provider: str
    requested_model: str
    effective_provider: str
    effective_model: str
    sample_count: int
    observed_run_count: int
    population_count: int
    missing_value_count: int
    mean: float
    min: float
    max: float
    execution_fingerprint_count: int
    execution_fingerprints: list[str] = Field(default_factory=list)
    fingerprints_truncated: bool = False
    evidence_run_ids: list[str] = Field(default_factory=list)
    run_ids_truncated: bool = False
    hard_constraint_violated: bool = False
    constraint_violations: list[str] = Field(default_factory=list)


class PairedMetricComparison(BaseModel):
    """Case/repetition-matched values across two explicit execution cohorts."""

    left_cohort_id: str
    right_cohort_id: str
    sample_count: int
    missing_value_count: int
    left_mean: float
    right_mean: float
    mean_delta: float
    mean_directional_improvement: float
    pair_keys: list[str] = Field(default_factory=list)
    pair_keys_truncated: bool = False


class MetricSeriesEvidence(BaseModel):
    """A unit/source/scope/direction series kept separate from every other series."""

    source: str
    scope: str
    unit: str
    recorded_direction: str
    direction_matches_objective: bool
    cohort_count: int
    cohorts: list[MetricCohortAggregate] = Field(default_factory=list)
    cohorts_truncated: bool = False
    paired_comparisons: list[PairedMetricComparison] = Field(default_factory=list)


class MetricObjectiveEvidence(BaseModel):
    """Aggregate evidence for one configured objective."""

    metric: str
    direction: str
    source: str | None = None
    scope: str | None = None
    unit: str | None = None
    floor: float | None = None
    ceiling: float | None = None
    observation_count: int = 0
    eligible_run_count: int = 0
    missing_value_count: int = 0
    observations_truncated: bool = False
    series_count: int = 0
    series_truncated: bool = False
    series: list[MetricSeriesEvidence] = Field(default_factory=list)


class MetricEvidence(BaseModel):
    """All configured objectives under one deterministic evidence budget."""

    selection_rules: dict[str, Any]
    objectives: list[MetricObjectiveEvidence] = Field(default_factory=list)
    digest: str = ""

    def has_observations(self) -> bool:
        return any(objective.observation_count for objective in self.objectives)

    def receipt(self) -> dict[str, Any]:
        """Proposal-safe aggregates, constraints, fingerprints, and evidence run IDs."""
        return self.model_dump(mode="json")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _cohort_material(row: dict[str, Any]) -> dict[str, str]:
    return {
        "behavior_hash": str(row.get("behavior_hash") or ""),
        "requested_provider": str(row.get("requested_provider") or ""),
        "requested_model": str(row.get("requested_model") or ""),
        "effective_provider": str(row.get("effective_provider") or ""),
        "effective_model": str(row.get("effective_model") or ""),
    }


def _cohort_id(row: dict[str, Any]) -> str:
    return "cohort_" + hashlib.sha256(_canonical(_cohort_material(row))).hexdigest()[:12]


def _series_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record["source"]),
        str(record["scope"]),
        str(record["unit"]),
        str(record["direction"]),
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _constraint_violations(
    objective: MetricObjective, values: list[float]
) -> list[str]:
    violations: list[str] = []
    observed_min = min(values)
    observed_max = max(values)
    if objective.floor is not None and observed_min < objective.floor:
        violations.append(f"observed min {observed_min} is below floor {objective.floor}")
    if objective.ceiling is not None and observed_max > objective.ceiling:
        violations.append(
            f"observed max {observed_max} is above ceiling {objective.ceiling}"
        )
    return violations


def _cohort_aggregate(
    objective: MetricObjective,
    records: list[dict[str, Any]],
    population: int,
) -> MetricCohortAggregate:
    first = records[0]
    values = [float(record["value"]) for record in records]
    run_ids = sorted({str(record["run_id"]) for record in records})
    fingerprints = sorted(
        {
            str(record["execution_fingerprint"])
            for record in records
            if record.get("execution_fingerprint")
        }
    )
    violations = _constraint_violations(objective, values)
    material = _cohort_material(first)
    return MetricCohortAggregate(
        cohort_id=_cohort_id(first),
        **material,
        sample_count=len(values),
        observed_run_count=len(run_ids),
        population_count=population,
        missing_value_count=max(0, population - len(run_ids)),
        mean=_mean(values),
        min=min(values),
        max=max(values),
        execution_fingerprint_count=len(fingerprints),
        execution_fingerprints=fingerprints[:MAX_FINGERPRINTS_PER_COHORT],
        fingerprints_truncated=len(fingerprints) > MAX_FINGERPRINTS_PER_COHORT,
        evidence_run_ids=run_ids[:MAX_RUN_IDS_PER_COHORT],
        run_ids_truncated=len(run_ids) > MAX_RUN_IDS_PER_COHORT,
        hard_constraint_violated=bool(violations),
        constraint_violations=violations,
    )


def _paired_comparisons(
    objective: MetricObjective,
    records: list[dict[str, Any]],
) -> list[PairedMetricComparison]:
    by_cohort: dict[str, dict[tuple[str, int], list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in records:
        if record.get("case_key") is None or record.get("repetition") is None:
            continue
        pair = (str(record["case_key"]), int(record["repetition"]))
        by_cohort[_cohort_id(record)][pair].append(float(record["value"]))

    comparisons: list[PairedMetricComparison] = []
    for left_id, right_id in combinations(sorted(by_cohort), 2):
        left = {key: _mean(values) for key, values in by_cohort[left_id].items()}
        right = {key: _mean(values) for key, values in by_cohort[right_id].items()}
        matched = sorted(left.keys() & right.keys())
        if not matched:
            continue
        deltas = [right[key] - left[key] for key in matched]
        direction_sign = 1 if objective.direction == "maximize" else -1
        pair_keys = [f"{case_key}:{repetition}" for case_key, repetition in matched]
        comparisons.append(
            PairedMetricComparison(
                left_cohort_id=left_id,
                right_cohort_id=right_id,
                sample_count=len(matched),
                missing_value_count=len(left.keys() | right.keys()) - len(matched),
                left_mean=_mean([left[key] for key in matched]),
                right_mean=_mean([right[key] for key in matched]),
                mean_delta=_mean(deltas),
                mean_directional_improvement=_mean(
                    [delta * direction_sign for delta in deltas]
                ),
                pair_keys=pair_keys[:MAX_RUN_IDS_PER_COHORT],
                pair_keys_truncated=len(pair_keys) > MAX_RUN_IDS_PER_COHORT,
            )
        )
        if len(comparisons) >= MAX_PAIR_COMPARISONS_PER_SERIES:
            break
    return comparisons


def build_metric_evidence(
    hive: Hive,
    harness_key: str,
    *,
    objectives: list[MetricObjective],
    version: str | None,
) -> MetricEvidence | None:
    """Build deterministic aggregates without metric metadata or raw trace content."""
    if not objectives:
        return None

    population_rows = hive.execution_cohort_populations(harness_key, version=version)
    populations = {_cohort_id(row): int(row["run_count"]) for row in population_rows}
    eligible_run_count = sum(populations.values())
    objective_evidence: list[MetricObjectiveEvidence] = []
    for objective in objectives:
        snapshot = hive.metric_history(
            harness_key,
            name=objective.metric,
            source=objective.source,
            scope=objective.scope,
            unit=objective.unit,
            version=version,
            limit=MAX_OBSERVATIONS_PER_OBJECTIVE,
        )
        records = snapshot["records"]
        grouped: dict[
            tuple[tuple[str, str, str, str], str], list[dict[str, Any]]
        ] = defaultdict(list)
        for record in records:
            grouped[(_series_key(record), _cohort_id(record))].append(record)

        all_series_keys = sorted({key[0] for key in grouped})
        series_values: list[MetricSeriesEvidence] = []
        for series_key in all_series_keys[:MAX_SERIES_PER_OBJECTIVE]:
            series_records = [
                record
                for (key, _cohort), group in grouped.items()
                if key == series_key
                for record in group
            ]
            all_cohort_ids = sorted(
                cohort for key, cohort in grouped if key == series_key
            )
            selected_cohort_ids = all_cohort_ids[:MAX_COHORTS_PER_SERIES]
            cohorts = [
                _cohort_aggregate(
                    objective,
                    grouped[(series_key, cohort_id)],
                    populations.get(cohort_id, len(grouped[(series_key, cohort_id)])),
                )
                for cohort_id in selected_cohort_ids
            ]
            selected_records = [
                record
                for record in series_records
                if _cohort_id(record) in selected_cohort_ids
            ]
            source, scope, unit, recorded_direction = series_key
            series_values.append(
                MetricSeriesEvidence(
                    source=source,
                    scope=scope,
                    unit=unit,
                    recorded_direction=recorded_direction,
                    direction_matches_objective=(
                        recorded_direction == objective.direction
                    ),
                    cohort_count=len(all_cohort_ids),
                    cohorts=cohorts,
                    cohorts_truncated=(
                        len(all_cohort_ids) > MAX_COHORTS_PER_SERIES
                    ),
                    paired_comparisons=_paired_comparisons(
                        objective, selected_records
                    ),
                )
            )
        objective_evidence.append(
            MetricObjectiveEvidence(
                **objective.model_dump(),
                observation_count=len(records),
                eligible_run_count=eligible_run_count,
                missing_value_count=max(
                    0,
                    eligible_run_count
                    - len({str(record["run_id"]) for record in records}),
                ),
                observations_truncated=bool(snapshot["truncated"]),
                series_count=len(all_series_keys),
                series_truncated=len(all_series_keys) > MAX_SERIES_PER_OBJECTIVE,
                series=series_values,
            )
        )

    selection_rules = {
        "version": version,
        "max_observations_per_objective": MAX_OBSERVATIONS_PER_OBJECTIVE,
        "max_series_per_objective": MAX_SERIES_PER_OBJECTIVE,
        "max_cohorts_per_series": MAX_COHORTS_PER_SERIES,
        "max_fingerprints_per_cohort": MAX_FINGERPRINTS_PER_COHORT,
        "max_run_ids_per_cohort": MAX_RUN_IDS_PER_COHORT,
        "max_pair_comparisons_per_series": MAX_PAIR_COMPARISONS_PER_SERIES,
        "cohort_fields": [
            "behavior_hash",
            "requested_provider",
            "requested_model",
            "effective_provider",
            "effective_model",
        ],
        "pairing": "eval case_key plus repetition",
        "private_metric_metadata_included": False,
    }
    evidence = MetricEvidence(
        selection_rules=selection_rules,
        objectives=objective_evidence,
    )
    evidence.digest = hashlib.sha256(
        _canonical(evidence.model_dump(mode="json", exclude={"digest"}))
    ).hexdigest()
    return evidence
