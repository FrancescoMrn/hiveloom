"""Deferred outcome labels: what the world said after the run finished."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveloom import runner
from hiveloom.evolve.analyzer import analyze
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response


def _run(harness_dir: Path, hive_path: Path, text: str = "done") -> str:
    result = runner.run_harness(
        harness_dir,
        "go",
        provider=FakeModelProvider([text_response(text)]),
        literal_input=True,
        hive_path=hive_path,
    )
    return result.run_id


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_records_and_reads_back_an_outcome(harness_dir: Path, tmp_path: Path):
    hive_path = tmp_path / "hive.db"
    run_id = _run(harness_dir, hive_path)
    with Hive(hive_path) as hive:
        hive.record_outcome(
            run_id, "failure", source="operator_ui", detail="proposal dismissed"
        )
        stored = hive.get_outcome(run_id)

    assert stored["outcome"] == "failure"
    assert stored["source"] == "operator_ui"
    assert stored["detail"] == "proposal dismissed"
    assert stored["recorded_at"]


def test_a_later_label_replaces_an_earlier_one(harness_dir: Path, tmp_path: Path):
    hive_path = tmp_path / "hive.db"
    run_id = _run(harness_dir, hive_path)
    with Hive(hive_path) as hive:
        hive.record_outcome(run_id, "failure")
        hive.record_outcome(run_id, "success", detail="corrected")
        assert hive.get_outcome(run_id)["outcome"] == "success"
        assert hive.outcome_summary("test-harness")["labelled_runs"] == 1


def test_the_run_row_is_never_rewritten(harness_dir: Path, tmp_path: Path):
    """A late label is a separate fact from what the run actually did."""
    hive_path = tmp_path / "hive.db"
    run_id = _run(harness_dir, hive_path)
    with Hive(hive_path) as hive:
        hive.record_outcome(run_id, "failure")
        assert hive.get_run(run_id)["status"] == "success"


def test_invalid_outcomes_and_unknown_runs_are_rejected(
    harness_dir: Path, tmp_path: Path
):
    hive_path = tmp_path / "hive.db"
    run_id = _run(harness_dir, hive_path)
    with Hive(hive_path) as hive:
        with pytest.raises(ValueError, match="must be 'success' or 'failure'"):
            hive.record_outcome(run_id, "maybe")
        with pytest.raises(KeyError, match="unknown run_id"):
            hive.record_outcome("run_nope", "success")


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_outcome_rate_is_independent_of_validator_success(
    harness_dir: Path, tmp_path: Path
):
    """Passing your own validators and being right are different things."""
    hive_path = tmp_path / "hive.db"
    ids = [_run(harness_dir, hive_path) for _ in range(4)]
    with Hive(hive_path) as hive:
        hive.record_outcome(ids[0], "success")
        hive.record_outcome(ids[1], "failure")
        hive.record_outcome(ids[2], "failure")
        summary = hive.summary("test-harness")
        outcomes = hive.outcome_summary("test-harness")

    assert summary["success_rate"] == 1.0  # every run satisfied the harness
    assert outcomes["labelled_runs"] == 3  # the fourth was never labelled
    assert outcomes["failures"] == 2
    assert outcomes["outcome_success_rate"] == pytest.approx(1 / 3)


def test_failed_outcome_traces_are_available_for_analysis(
    harness_dir: Path, tmp_path: Path
):
    hive_path = tmp_path / "hive.db"
    ids = [_run(harness_dir, hive_path) for _ in range(2)]
    with Hive(hive_path) as hive:
        hive.record_outcome(ids[0], "success")
        hive.record_outcome(ids[1], "failure", detail="targeted the wrong cohort")
        failures = hive.failed_outcome_traces("test-harness")

    assert len(failures) == 1
    assert failures[0]["run_id"] == ids[1]
    assert failures[0]["detail"] == "targeted the wrong cohort"
    assert failures[0]["trace_path"]


def test_analyzer_surfaces_outcomes_to_the_evolving_model(
    harness_dir: Path, tmp_path: Path
):
    hive_path = tmp_path / "hive.db"
    ids = [_run(harness_dir, hive_path) for _ in range(2)]
    with Hive(hive_path) as hive:
        hive.record_outcome(ids[0], "failure", detail="dismissed by the operator")
        hive.record_outcome(ids[1], "failure", detail="wrong segment")
        report = analyze(hive, "test-harness")

    assert report.outcomes["failures"] == 2
    assert len(report.outcome_failures) == 2
    # Runs that satisfied their validators but failed in the world are still
    # evidence worth evolving on.
    assert not report.is_empty()


def test_pruning_removes_outcome_labels_too(harness_dir: Path, tmp_path: Path):
    from datetime import UTC, datetime, timedelta

    hive_path = tmp_path / "hive.db"
    run_id = _run(harness_dir, hive_path)
    with Hive(hive_path) as hive:
        hive.record_outcome(run_id, "failure")
        hive.prune_runs(1, now=datetime.now(UTC) + timedelta(days=3))
        assert hive.get_outcome(run_id) is None
