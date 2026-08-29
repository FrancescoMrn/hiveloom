"""Tests for run auto-ingest and the `trace` / `stats` CLI commands."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import runner
from hiveloom.cli import app
from hiveloom.errors import ExitCode
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"
cli = CliRunner()

_VALID = json.dumps({"title": "T", "summary": "short.", "key_points": ["a"]})


def _harness(tmp_path: Path) -> Path:
    target = tmp_path / "h"
    # Example folders are runnable workspaces and may legitimately carry local
    # journals. A fixture starts from the harness definition, not a developer's
    # prior run history, or its version counts depend on ambient state.
    shutil.copytree(EXAMPLE_HARNESS, target, ignore=shutil.ignore_patterns(".hiveloom"))
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 20)
    return target


def _run_success(harness: Path):
    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "notes.txt"}), text_response(_VALID)]
    )
    return runner.run_harness(harness, "notes.txt", provider=provider)


def test_run_auto_ingests_into_hive(tmp_path: Path):
    harness = _harness(tmp_path)
    result = _run_success(harness)
    # The autouse conftest fixture points HIVELOOM_DB at a throwaway db.
    with Hive() as hive:
        run = hive.get_run(result.run_id)
    assert run is not None
    assert run["status"] == "success"
    assert run["harness_name"] == "example-summarizer"


def test_stats_ingests_dir_and_reports(tmp_path: Path):
    harness = _harness(tmp_path)
    _run_success(harness)
    r = cli.invoke(app, ["stats", str(harness), "--json"])
    assert r.exit_code == ExitCode.OK
    payload = json.loads(r.stdout)
    assert payload["harness_name"] == "example-summarizer"
    assert payload["total_runs"] >= 1
    assert payload["success_rate"] == 1.0
    assert len(payload["versions"]) == 1


def test_trace_shows_run_events(tmp_path: Path):
    harness = _harness(tmp_path)
    result = _run_success(harness)
    r = cli.invoke(app, ["trace", result.run_id, "--json"])
    assert r.exit_code == ExitCode.OK
    payload = json.loads(r.stdout)
    assert payload["run"]["run_id"] == result.run_id
    types = [e["type"] for e in payload["events"]]
    assert types[0] == "run_started" and types[-1] == "run_finished"


def test_trace_unknown_run_is_error(tmp_path: Path):
    r = cli.invoke(app, ["trace", "run_does_not_exist", "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert json.loads(r.stdout)["ok"] is False


def test_stats_by_dir_ingests_infolder_traces(tmp_path: Path):
    """A copied-back harness dir is ingested on the fly by `stats`."""
    harness = _harness(tmp_path)
    _run_success(harness)
    # Copy the harness (with its .hiveloom/traces) elsewhere, fresh Hive.
    copied = tmp_path / "copied"
    shutil.copytree(harness, copied)
    r = cli.invoke(app, ["stats", str(copied), "--json"])
    assert r.exit_code == ExitCode.OK
    assert json.loads(r.stdout)["total_runs"] >= 1


# --------------------------------------------------------------------------- #
# Version comparison (workbench support)
# --------------------------------------------------------------------------- #
def _seed_version(hive, name, version, *, runs, successes, cost=0.01, feedback=None):
    for index in range(runs):
        run_id = f"{version}-{index}"
        status = "success" if index < successes else "verify_failed"
        hive._conn.execute(
            "INSERT INTO runs (run_id, harness_name, harness_key, harness_version_hash, status, "
            "turns, cost_usd, duration_seconds, started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, name, name, version, status, 2, cost, 1.0, f"2026-01-0{index % 9 + 1}"),
        )
        if status != "success" and feedback:
            hive._conn.execute(
                "INSERT INTO verifications (run_id, seq, verifier, passed, feedback) "
                "VALUES (?,?,?,?,?)",
                (run_id, 1, "check", 0, feedback),
            )
    hive._conn.commit()


def test_compare_versions_reports_the_delta_in_the_right_direction(tmp_path):
    from hiveloom.logging.hive import Hive

    with Hive(tmp_path / "hive.db") as hive:
        _seed_version(hive, "h", "old", runs=10, successes=5, cost=0.02)
        _seed_version(hive, "h", "new", runs=10, successes=8, cost=0.01)

        report = hive.compare_versions("h", "old", "new")

    # right minus left: positive success_rate means the right side is better.
    assert report["delta"]["success_rate"] == pytest.approx(0.3)
    assert report["delta"]["avg_cost_usd"] == pytest.approx(-0.01)
    assert report["left"]["version"] == "old"
    assert report["right"]["version"] == "new"
    assert report["underpowered"] is False


def test_compare_versions_names_which_failures_stopped_and_started(tmp_path):
    from hiveloom.logging.hive import Hive

    with Hive(tmp_path / "hive.db") as hive:
        _seed_version(hive, "h", "old", runs=6, successes=0, feedback="not valid JSON")
        _seed_version(hive, "h", "new", runs=6, successes=0, feedback="summary too long")

        report = hive.compare_versions("h", "old", "new")

    assert any("JSON" in item for item in report["fixed_failures"])
    assert any("long" in item for item in report["new_failures"])


def test_compare_versions_flags_an_underpowered_comparison(tmp_path):
    """A confident delta over a sample of two is worse than no delta."""
    from hiveloom.logging.hive import Hive

    with Hive(tmp_path / "hive.db") as hive:
        _seed_version(hive, "h", "old", runs=2, successes=0)
        _seed_version(hive, "h", "new", runs=2, successes=2)

        assert hive.compare_versions("h", "old", "new")["underpowered"] is True


def test_comparing_against_a_version_with_no_runs_is_not_an_error(tmp_path):
    from hiveloom.logging.hive import Hive

    with Hive(tmp_path / "hive.db") as hive:
        _seed_version(hive, "h", "old", runs=6, successes=3)
        report = hive.compare_versions("h", "old", "never-ran")

    assert report["right"]["runs"] == 0
    assert report["underpowered"] is True
