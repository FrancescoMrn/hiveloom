"""Trace-free eval reports and paired comparisons from the Hive index."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any

from hiveloom.logging.hive import Hive

MetricKey = tuple[str, str, str, str, str]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rate(count: int, population: int) -> float | None:
    return count / population if population else None


def _metric_key(metric: dict[str, Any]) -> MetricKey:
    return (
        metric["name"],
        metric["source"],
        metric["scope"],
        metric["unit"],
        metric["direction"],
    )


def _metric_identity(key: MetricKey) -> dict[str, str]:
    name, source, scope, unit, direction = key
    return {
        "name": name,
        "source": source,
        "scope": scope,
        "unit": unit,
        "direction": direction,
    }


def _metric_aggregates(
    metrics: list[dict[str, Any]], cells: list[dict[str, Any]], repetitions: int
) -> list[dict[str, Any]]:
    groups: dict[MetricKey, list[dict[str, Any]]] = defaultdict(list)
    for metric in metrics:
        groups[_metric_key(metric)].append(metric)
    total = len(cells)
    total_cases = len({cell["case_key"] for cell in cells})
    output: list[dict[str, Any]] = []
    for key, records in sorted(groups.items()):
        values = [float(record["value"]) for record in records]
        observed = len({record["run_id"] for record in records})
        aggregate: dict[str, Any] = {
            **_metric_identity(key),
            "sample_count": len(values),
            "observed_run_count": observed,
            "population_count": len(values) if key[2] == "eval" else total,
            "missing_value_count": 0 if key[2] == "eval" else max(0, total - observed),
            "mean": _mean(values),
            "min": min(values),
            "max": max(values),
        }
        if repetitions > 1:
            by_case: dict[str, list[float]] = defaultdict(list)
            for record in records:
                by_case[record["case_key"]].append(float(record["value"]))
            deviations = [
                statistics.pstdev(case_values)
                for case_values in by_case.values()
                if len(case_values) > 1
            ]
            if deviations:
                aggregate["stability"] = {
                    "method": "mean_within_case_population_stddev",
                    "case_count": len(deviations),
                    "missing_case_count": total_cases - len(deviations),
                    "value": _mean(deviations),
                }
        output.append(aggregate)
    return output


def _report_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    metadata = snapshot["eval"]
    cells = snapshot["cells"]
    total = len(cells)
    completed = [cell for cell in cells if cell["status"] == "completed"]
    final_success = sum(cell["run_status"] == "success" for cell in completed)
    first_pass_values = [
        bool(cell["first_pass_valid"])
        for cell in completed
        if cell["first_pass_valid"] is not None
    ]
    recovery_attempts = [cell for cell in completed if cell["recovery_attempted"]]
    recovered = sum(bool(cell["recovered"]) for cell in recovery_attempts)

    latency_values = [
        float(cell["duration_ms"])
        for cell in completed
        if cell["run_status"]
    ]
    costs: list[dict[str, Any]] = []
    for source in ("billed", "estimated", "mixed", "none"):
        values = [
            float(cell["cost_usd"])
            for cell in completed
            if cell["run_status"] and cell["cost_source"] == source
        ]
        if values or source in {"billed", "estimated"}:
            costs.append(
                {
                    "source": source,
                    "sample_count": len(values),
                    "missing_value_count": total - len(values),
                    "total_usd": sum(values),
                    "mean_usd": _mean(values),
                }
            )

    status_counts = Counter(cell["run_status"] or cell["status"] for cell in cells)
    effective_models = sorted(
        {
            cell["effective_model"]
            for cell in cells
            if cell.get("effective_model")
        }
    )
    return {
        "schema_version": 1,
        "eval_run_id": metadata["eval_run_id"],
        "eval_id": metadata["eval_id"],
        "status": metadata["status"],
        "harness_key": metadata["harness_key"],
        "harness_behavior_hash": metadata["harness_behavior_hash"],
        "requested_provider": metadata["requested_provider"],
        "requested_model": metadata["requested_model"],
        "effective_models": effective_models,
        "repetitions": metadata["repetitions"],
        "cells": {
            "sample_count": total,
            "completed_count": len(completed),
            "missing_count": total - len(completed),
            "status_counts": dict(sorted(status_counts.items())),
        },
        "final_success": {
            "sample_count": len(completed),
            "missing_value_count": total - len(completed),
            "count": final_success,
            "rate": _rate(final_success, len(completed)),
        },
        "first_pass": {
            "sample_count": len(first_pass_values),
            "missing_value_count": total - len(first_pass_values),
            "valid_count": sum(first_pass_values),
            "rate": _rate(sum(first_pass_values), len(first_pass_values)),
        },
        "recovery": {
            "sample_count": len(completed),
            "missing_value_count": total - len(completed),
            "attempted_count": len(recovery_attempts),
            "recovered_count": recovered,
            "rate": _rate(recovered, len(recovery_attempts)),
            "rate_denominator": "recovery_attempts",
        },
        "latency_ms": {
            "sample_count": len(latency_values),
            "missing_value_count": total - len(latency_values),
            "mean": _mean(latency_values),
            "min": min(latency_values) if latency_values else None,
            "max": max(latency_values) if latency_values else None,
        },
        "cost": costs,
        "metrics": _metric_aggregates(
            snapshot["metrics"], cells, int(metadata["repetitions"])
        ),
    }


def build_eval_report(eval_run_id: str, *, hive: Hive | None = None) -> dict[str, Any]:
    """Build the canonical JSON report from indexed state, never raw traces."""
    owns_hive = hive is None
    hive = hive or Hive()
    try:
        snapshot = hive.get_eval_snapshot(eval_run_id)
        if snapshot is None:
            raise ValueError(f"eval run not found in Hive: {eval_run_id}")
        return _report_from_snapshot(snapshot)
    finally:
        if owns_hive:
            hive.close()


def _cell_map(snapshot: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (cell["case_key"], int(cell["repetition"])): cell
        for cell in snapshot["cells"]
    }


def _metric_cell_map(
    snapshot: dict[str, Any],
) -> dict[tuple[str, int], dict[MetricKey, float]]:
    values: dict[tuple[str, int], dict[MetricKey, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for metric in snapshot["metrics"]:
        pair = (metric["case_key"], int(metric["repetition"]))
        values[pair][_metric_key(metric)].append(float(metric["value"]))
    return {
        pair: {key: sum(group) / len(group) for key, group in groups.items()}
        for pair, groups in values.items()
    }


def _paired_delta(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    field: str,
) -> dict[str, Any]:
    values = [
        (float(candidate[field]), float(baseline[field]))
        for baseline, candidate in pairs
        if baseline.get(field) is not None and candidate.get(field) is not None
    ]
    return {
        "sample_count": len(values),
        "missing_value_count": len(pairs) - len(values),
        "baseline_mean": _mean([baseline for _, baseline in values]),
        "candidate_mean": _mean([candidate for candidate, _ in values]),
        "mean_delta": _mean([candidate - baseline for candidate, baseline in values]),
    }


def _paired_cost(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    groups = []
    comparable = 0
    for source in ("billed", "estimated", "mixed"):
        values = [
            (float(candidate["cost_usd"]), float(baseline["cost_usd"]))
            for baseline, candidate in pairs
            if baseline.get("run_status")
            and candidate.get("run_status")
            and baseline.get("cost_source") == source
            and candidate.get("cost_source") == source
        ]
        comparable += len(values)
        if values or source in {"billed", "estimated"}:
            groups.append(
                {
                    "source": source,
                    "sample_count": len(values),
                    "missing_value_count": len(pairs) - len(values),
                    "baseline_mean": _mean([baseline for _, baseline in values]),
                    "candidate_mean": _mean([candidate for candidate, _ in values]),
                    "mean_delta": _mean(
                        [candidate - baseline for candidate, baseline in values]
                    ),
                }
            )
    return {
        "by_source": groups,
        "comparable_count": comparable,
        "incomparable_or_missing_count": len(pairs) - comparable,
    }


def compare_evals(
    baseline_id: str, candidate_id: str, *, hive: Hive | None = None
) -> dict[str, Any]:
    """Compare only matching case/repetition cells and label unmatched cells."""
    owns_hive = hive is None
    hive = hive or Hive()
    try:
        baseline = hive.get_eval_snapshot(baseline_id)
        candidate = hive.get_eval_snapshot(candidate_id)
        if baseline is None:
            raise ValueError(f"eval run not found in Hive: {baseline_id}")
        if candidate is None:
            raise ValueError(f"eval run not found in Hive: {candidate_id}")
        baseline_cells = _cell_map(baseline)
        candidate_cells = _cell_map(candidate)
        matched_keys = sorted(baseline_cells.keys() & candidate_cells.keys())
        pairs = [(baseline_cells[key], candidate_cells[key]) for key in matched_keys]
        baseline_only = sorted(baseline_cells.keys() - candidate_cells.keys())
        candidate_only = sorted(candidate_cells.keys() - baseline_cells.keys())

        baseline_metrics = _metric_cell_map(baseline)
        candidate_metrics = _metric_cell_map(candidate)
        metric_values: dict[MetricKey, list[tuple[float, float]]] = defaultdict(list)
        metric_presence: Counter[MetricKey] = Counter()
        for pair in matched_keys:
            left = baseline_metrics.get(pair, {})
            right = candidate_metrics.get(pair, {})
            for key in left.keys() | right.keys():
                metric_presence[key] += 1
                if key in left and key in right:
                    metric_values[key].append((left[key], right[key]))
        metric_comparisons = []
        for key in sorted(metric_presence):
            values = metric_values[key]
            deltas = [
                candidate_value - baseline_value
                for baseline_value, candidate_value in values
            ]
            sign = 1 if key[4] == "maximize" else -1
            metric_comparisons.append(
                {
                    **_metric_identity(key),
                    "sample_count": len(values),
                    "missing_value_count": len(matched_keys) - len(values),
                    "baseline_mean": _mean([value[0] for value in values]),
                    "candidate_mean": _mean([value[1] for value in values]),
                    "mean_delta": _mean(deltas),
                    "mean_directional_improvement": _mean(
                        [delta * sign for delta in deltas]
                    ),
                }
            )

        outcome_pairs = [
            (
                {
                    **left,
                    "success": (
                        left["run_status"] == "success"
                        if left["status"] == "completed" and left["run_status"]
                        else None
                    ),
                },
                {
                    **right,
                    "success": (
                        right["run_status"] == "success"
                        if right["status"] == "completed" and right["run_status"]
                        else None
                    ),
                },
            )
            for left, right in pairs
        ]
        latency_pairs = [
            (
                {
                    **left,
                    "duration_value": left["duration_ms"] if left["run_status"] else None,
                },
                {
                    **right,
                    "duration_value": (
                        right["duration_ms"] if right["run_status"] else None
                    ),
                },
            )
            for left, right in pairs
        ]
        comparison = {
            "schema_version": 1,
            "baseline_id": baseline_id,
            "candidate_id": candidate_id,
            "pairing": {
                "matched_count": len(matched_keys),
                "baseline_unmatched_count": len(baseline_only),
                "candidate_unmatched_count": len(candidate_only),
                "baseline_unmatched": [f"{case}:{rep}" for case, rep in baseline_only],
                "candidate_unmatched": [f"{case}:{rep}" for case, rep in candidate_only],
            },
            "final_success": _paired_delta(outcome_pairs, "success"),
            "latency_ms": _paired_delta(latency_pairs, "duration_value"),
            "cost_usd": _paired_cost(pairs),
            "metrics": metric_comparisons,
            "baseline": _report_from_snapshot(baseline),
            "candidate": _report_from_snapshot(candidate),
        }
        return comparison
    finally:
        if owns_hive:
            hive.close()


def render_report_markdown(report: dict[str, Any]) -> str:
    """Render a canonical eval report without changing its calculations."""
    final = report["final_success"]
    first = report["first_pass"]
    recovery = report["recovery"]
    lines = [
        f"# Eval report: {report['eval_run_id']}",
        "",
        f"Status: {report['status']}",
        "",
        "| Receipt | Value | n | Missing |",
        "|---|---:|---:|---:|",
        (
            f"| Final success | {final['rate']} | {final['sample_count']} | "
            f"{final['missing_value_count']} |"
        ),
        (
            f"| First-pass valid | {first['rate']} | {first['sample_count']} | "
            f"{first['missing_value_count']} |"
        ),
        (
            f"| Recovery | {recovery['rate']} | {recovery['sample_count']} | "
            f"{recovery['missing_value_count']} |"
        ),
        (
            f"| Mean latency (ms) | {report['latency_ms']['mean']} | "
            f"{report['latency_ms']['sample_count']} | "
            f"{report['latency_ms']['missing_value_count']} |"
        ),
    ]
    if report["metrics"]:
        lines += [
            "",
            "## Metrics",
            "",
            "| Metric | Source | Scope | Mean | n | Missing |",
            "|---|---|---|---:|---:|---:|",
        ]
        for metric in report["metrics"]:
            lines.append(
                f"| {metric['name']} | {metric['source']} | {metric['scope']} | "
                f"{metric['mean']} | {metric['sample_count']} | "
                f"{metric['missing_value_count']} |"
            )
    return "\n".join(lines) + "\n"


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    """Render a paired comparison and keep unmatched cells visible."""
    pairing = comparison["pairing"]
    lines = [
        f"# Eval comparison: {comparison['baseline_id']} vs {comparison['candidate_id']}",
        "",
        (
            f"Paired cells: {pairing['matched_count']}. Baseline-only: "
            f"{pairing['baseline_unmatched_count']}. Candidate-only: "
            f"{pairing['candidate_unmatched_count']}."
        ),
        "",
        "| Receipt | Baseline | Candidate | Delta | n | Missing |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, key in (("Final success", "final_success"), ("Latency (ms)", "latency_ms")):
        value = comparison[key]
        lines.append(
            f"| {label} | {value['baseline_mean']} | {value['candidate_mean']} | "
            f"{value['mean_delta']} | {value['sample_count']} | "
            f"{value['missing_value_count']} |"
        )
    for cost in comparison["cost_usd"]["by_source"]:
        lines.append(
            f"| Cost USD ({cost['source']}) | {cost['baseline_mean']} | "
            f"{cost['candidate_mean']} | {cost['mean_delta']} | "
            f"{cost['sample_count']} | {cost['missing_value_count']} |"
        )
    if comparison["metrics"]:
        lines += [
            "",
            "## Paired metrics",
            "",
            "| Metric | Baseline | Candidate | Delta | Directional improvement | n | Missing |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for metric in comparison["metrics"]:
            lines.append(
                f"| {metric['name']} ({metric['source']}) | {metric['baseline_mean']} | "
                f"{metric['candidate_mean']} | {metric['mean_delta']} | "
                f"{metric['mean_directional_improvement']} | {metric['sample_count']} | "
                f"{metric['missing_value_count']} |"
            )
    return "\n".join(lines) + "\n"
