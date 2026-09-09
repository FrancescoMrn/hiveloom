"""Metric objectives reach evolution as bounded, provenance-aware aggregates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import construct
from hiveloom.cli import app
from hiveloom.errors import ExitCode
from hiveloom.evolve.analyzer import analyze
from hiveloom.evolve.evolver import (
    MutationProposal,
    ProposalError,
    build_evolve_prompt,
    gate,
    propose,
)
from hiveloom.evolve.proposals import create_proposal, proposal_payload
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter
from hiveloom.metrics import RunMetric, record_run_metrics
from hiveloom.spec.loader import load_spec

HARNESS_KEY = "metric-evolution-fixture"
BEHAVIOR_HASH = "behavior-metric-v1"
cli = CliRunner()


def _harness(tmp_path: Path, objectives: list[dict]) -> Path:
    harness = tmp_path / "harness"
    construct.init_harness(
        harness,
        name="metric-evolution",
        task="Improve synthetic numeric outcomes.",
    )
    construct.set_value(harness, "id", HARNESS_KEY)
    construct.set_value(harness, "evolution.objectives", objectives)
    return harness


def _run_trace(tmp_path: Path, run_id: str, model: str) -> Path:
    writer = TraceWriter(
        tmp_path / "traces",
        run_id,
        "metric-evolution",
        BEHAVIOR_HASH,
        harness_id=HARNESS_KEY,
    )
    writer.emit("run_started", input="synthetic-private-input")
    writer.emit(
        "run_finished",
        status="success",
        turns=1,
        cost_usd=0.01,
        duration_seconds=0.1,
        execution={
            "requested_provider": "fixture",
            "requested_model": model,
            "effective_provider": "fixture",
            "effective_model": model,
            "execution_fingerprint": f"fingerprint-{run_id}",
        },
    )
    return writer.path


def _eval_manifest(eval_run_id: str, model: str, run_ids: list[str]) -> dict:
    cells = []
    for index, run_id in enumerate(run_ids):
        cells.append(
            {
                "cell_id": f"cell-{index}",
                "case_key": f"case-{index}",
                "repetition": 0,
                "status": "completed",
                "run_id": run_id,
                "run_status": "success",
                "scorer_status": "success",
                "requested_provider": "fixture",
                "requested_model": model,
                "effective_provider": "fixture",
                "effective_model": model,
                "execution_fingerprint": f"fingerprint-{run_id}",
                "duration_ms": 100,
                "cost_usd": 0.01,
                "cost_source": "billed",
                "verification": {
                    "attempts": 1,
                    "first_pass_valid": True,
                    "recovery_attempted": False,
                    "recovered": False,
                    "final_status": "passed",
                },
                "trace_disabled": False,
                "finished_at": "2026-08-29T12:00:00+00:00",
            }
        )
    return {
        "eval_run_id": eval_run_id,
        "status": "completed",
        "eval_identity": {"eval_id": "matching-v1"},
        "harness_id": HARNESS_KEY,
        "harness_behavior_hash": BEHAVIOR_HASH,
        "requested_provider": "fixture",
        "requested_model": model,
        "repetitions": 1,
        "created_at": "2026-08-29T12:00:00+00:00",
        "updated_at": "2026-08-29T12:00:00+00:00",
        "cells": cells,
    }


def _metric(
    run_id: str,
    name: str,
    value: float,
    *,
    direction: str = "maximize",
    unit: str = "ratio",
) -> RunMetric:
    return RunMetric(
        run_id=run_id,
        name=name,
        value=value,
        direction=direction,
        unit=unit,
        source="matching_eval_v1",
        scope="case",
        metadata={"private": "synthetic-private-metric-metadata"},
    )


def _seed_two_cohorts(hive: Hive, tmp_path: Path) -> None:
    for model, run_ids in (
        ("model-a", ["run_a0", "run_a1"]),
        ("model-b", ["run_b0", "run_b1"]),
    ):
        for run_id in run_ids:
            hive.ingest_trace_file(_run_trace(tmp_path, run_id, model))
        hive.upsert_eval_manifest(
            _eval_manifest(f"eval-{model}", model, run_ids),
            str(tmp_path / f"eval-{model}.json"),
        )


def test_objective_schema_validates_bounds_labels_and_unique_metrics(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [
            {
                "metric": "quality",
                "direction": "maximize",
                "unit": "ratio",
                "floor": 0.5,
                "ceiling": 1.0,
            }
        ],
    )
    assert load_spec(harness).evolution.objectives[0].floor == 0.5

    with pytest.raises(Exception, match="floor cannot exceed ceiling"):
        construct.set_value(
            harness,
            "evolution.objectives",
            [
                {
                    "metric": "quality",
                    "direction": "maximize",
                    "floor": 2,
                    "ceiling": 1,
                }
            ],
        )
    with pytest.raises(Exception, match="must be unique"):
        construct.set_value(
            harness,
            "evolution.objectives",
            [
                {"metric": "quality", "direction": "maximize"},
                {"metric": "quality", "direction": "minimize"},
            ],
        )
    with pytest.raises(Exception, match="Input should be 'maximize' or 'minimize'"):
        construct.set_value(
            harness,
            "evolution.objectives",
            [{"metric": "quality", "direction": "sideways"}],
        )
    with pytest.raises(Exception, match="unit cannot exceed 64"):
        construct.set_value(
            harness,
            "evolution.objectives",
            [
                {
                    "metric": "quality",
                    "direction": "maximize",
                    "unit": "x" * 65,
                }
            ],
        )


def test_objectives_have_json_cli_success_error_schema_and_explain(tmp_path: Path):
    harness = tmp_path / "cli-harness"
    initialized = cli.invoke(
        app,
        ["init", str(harness), "--name", "objective-cli", "--task", "T", "--json"],
    )
    assert initialized.exit_code == ExitCode.OK

    configured = cli.invoke(
        app,
        [
            "set",
            "evolution.objectives",
            '[{"metric":"quality","direction":"maximize","unit":"ratio"}]',
            "--dir",
            str(harness),
            "--json",
        ],
    )
    assert configured.exit_code == ExitCode.OK
    invalid = cli.invoke(
        app,
        [
            "set",
            "evolution.objectives",
            '[{"metric":"quality","direction":"maximize","floor":2,"ceiling":1}]',
            "--dir",
            str(harness),
            "--json",
        ],
    )
    assert invalid.exit_code == ExitCode.SPEC_ERROR
    assert "floor cannot exceed ceiling" in json.loads(invalid.stdout)["error"]

    explained = cli.invoke(app, ["explain", "evolution.objectives", "--json"])
    assert explained.exit_code == ExitCode.OK
    assert json.loads(explained.stdout)["path"] == "evolution.objectives"
    schema = cli.invoke(app, ["schema", "--json"])
    assert schema.exit_code == ExitCode.OK
    assert "MetricObjective" in json.loads(schema.stdout)["$defs"]


def test_metric_history_groups_models_pairs_cases_and_reports_missing(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [{"metric": "quality", "direction": "maximize", "unit": "ratio"}],
    )
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        _seed_two_cohorts(hive, tmp_path)
        record_run_metrics(
            hive,
            HARNESS_KEY,
            [
                _metric("run_a0", "quality", 0.2),
                _metric("run_a1", "quality", 0.4),
                _metric("run_b0", "quality", 0.6),
            ],
        )
        report = analyze(
            hive,
            HARNESS_KEY,
            version=BEHAVIOR_HASH,
            objectives=spec.evolution.objectives,
        )

    assert not report.is_empty()
    evidence = report.metric_evidence
    assert evidence is not None
    [objective] = evidence.objectives
    assert objective.observation_count == 3
    [series] = objective.series
    assert series.direction_matches_objective
    assert len(series.cohorts) == 2
    cohorts = {cohort.effective_model: cohort for cohort in series.cohorts}
    assert cohorts["model-a"].mean == pytest.approx(0.3)
    assert cohorts["model-a"].sample_count == 2
    assert cohorts["model-a"].execution_fingerprint_count == 2
    assert cohorts["model-b"].missing_value_count == 1
    [paired] = series.paired_comparisons
    assert paired.sample_count == 1
    assert paired.mean_directional_improvement == pytest.approx(0.4)


def test_prompt_contains_aggregates_not_metric_metadata_or_raw_trace(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [{"metric": "quality", "direction": "maximize"}],
    )
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        _seed_two_cohorts(hive, tmp_path)
        record_run_metrics(hive, HARNESS_KEY, [_metric("run_a0", "quality", 0.7)])
        report = analyze(
            hive,
            HARNESS_KEY,
            version=BEHAVIOR_HASH,
            objectives=spec.evolution.objectives,
        )

    _, prompt = build_evolve_prompt(spec, report)
    assert '"sample_count": 1' in prompt
    assert "run_a0" in prompt
    assert "synthetic-private-metric-metadata" not in prompt
    assert "synthetic-private-input" not in prompt


def test_missing_metric_is_not_coerced_to_zero(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [{"metric": "quality", "direction": "maximize"}],
    )
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(_run_trace(tmp_path, "run_a0", "model-a"))
        report = analyze(
            hive,
            HARNESS_KEY,
            version=BEHAVIOR_HASH,
            objectives=spec.evolution.objectives,
        )

    assert report.is_empty()
    [objective] = report.metric_evidence.objectives
    assert objective.observation_count == 0
    assert objective.series == []


def test_hard_ceiling_cannot_be_ignored_for_a_cost_improvement(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [
            {
                "metric": "hallucination_rate",
                "direction": "minimize",
                "ceiling": 0,
            },
            {"metric": "billed_cost_usd", "direction": "minimize"},
        ],
    )
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(_run_trace(tmp_path, "run_a0", "model-a"))
        record_run_metrics(
            hive,
            HARNESS_KEY,
            [
                _metric(
                    "run_a0",
                    "hallucination_rate",
                    0.2,
                    direction="minimize",
                ),
                _metric(
                    "run_a0",
                    "billed_cost_usd",
                    0.01,
                    direction="minimize",
                    unit="USD",
                ),
            ],
        )
        report = analyze(
            hive,
            HARNESS_KEY,
            version=BEHAVIOR_HASH,
            objectives=spec.evolution.objectives,
        )

    payload = json.dumps(
        {
            "rationale": "Reduce cost.",
            "yaml_changes": [{"path": "loop.max_turns", "value": 5}],
            "objective_expectations": [
                {
                    "metric": "billed_cost_usd",
                    "expected_change": "decrease",
                    "rationale": "Lower expected spend.",
                }
            ],
        }
    )
    with pytest.raises(ProposalError, match="hard metric constraint.*hallucination_rate"):
        propose(spec, report, FakeStrongModel([payload]))


def test_recorded_metric_direction_must_match_the_objective(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [{"metric": "quality", "direction": "maximize"}],
    )
    spec = load_spec(harness)
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(_run_trace(tmp_path, "run_a0", "model-a"))
        record_run_metrics(
            hive,
            HARNESS_KEY,
            [_metric("run_a0", "quality", 0.7, direction="minimize")],
        )
        report = analyze(
            hive,
            HARNESS_KEY,
            version=BEHAVIOR_HASH,
            objectives=spec.evolution.objectives,
        )

    payload = json.dumps(
        {
            "rationale": "Raise quality.",
            "yaml_changes": [{"path": "loop.max_turns", "value": 10}],
            "objective_expectations": [
                {"metric": "quality", "expected_change": "increase"}
            ],
        }
    )
    with pytest.raises(ProposalError, match="direction disagrees"):
        propose(spec, report, FakeStrongModel([payload]))


def test_metric_proposal_records_expectation_and_aggregate_receipt(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [{"metric": "quality", "direction": "maximize", "floor": 0}],
    )
    spec = load_spec(harness)
    payload = json.dumps(
        {
            "rationale": "Raise quality from the observed baseline.",
            "yaml_changes": [{"path": "loop.max_turns", "value": 25}],
            "objective_expectations": [
                {
                    "metric": "quality",
                    "expected_change": "increase",
                    "rationale": "n=1, baseline mean 0.7, evidence run run_a0.",
                }
            ],
        }
    )
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(_run_trace(tmp_path, "run_a0", "model-a"))
        record_run_metrics(hive, HARNESS_KEY, [_metric("run_a0", "quality", 0.7)])
        report = analyze(
            hive,
            HARNESS_KEY,
            version=BEHAVIOR_HASH,
            objectives=spec.evolution.objectives,
        )
        record = create_proposal(
            hive,
            spec,
            harness,
            report,
            FakeStrongModel([payload]),
            trigger="manual",
        )

    shown = proposal_payload(record)
    expectation = shown["proposal"]["objective_expectations"][0]
    assert expectation["metric"] == "quality"
    metric_receipt = shown["evidence"]["metric_history"]
    cohort = metric_receipt["objectives"][0]["series"][0]["cohorts"][0]
    assert cohort["sample_count"] == 1
    assert cohort["mean"] == pytest.approx(0.7)
    assert cohort["evidence_run_ids"] == ["run_a0"]


def test_objectives_are_frozen_and_expectations_follow_direction(tmp_path: Path):
    harness = _harness(
        tmp_path,
        [{"metric": "quality", "direction": "maximize"}],
    )
    spec = load_spec(harness)
    result = gate(
        spec,
        MutationProposal(
            yaml_changes=[
                {
                    "path": "evolution.objectives",
                    "value": [{"metric": "cost", "direction": "minimize"}],
                }
            ],
            objective_expectations=[
                {"metric": "quality", "expected_change": "increase"}
            ],
        ),
    )
    assert result.rejected == [
        {"path": "evolution.objectives", "reason": "frozen path"}
    ]

    wrong_direction = gate(
        spec,
        MutationProposal(
            yaml_changes=[{"path": "loop.max_turns", "value": 10}],
            objective_expectations=[
                {"metric": "quality", "expected_change": "decrease"}
            ],
        ),
    )
    assert wrong_direction.accepted == []
    assert "must be 'increase'" in wrong_direction.rejected[0]["reason"]
