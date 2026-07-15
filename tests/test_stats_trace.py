"""Tests for run auto-ingest and the `trace` / `stats` CLI commands."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

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
    shutil.copytree(EXAMPLE_HARNESS, target)
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
