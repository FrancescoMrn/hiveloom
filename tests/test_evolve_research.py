"""`hiveloom evolve --research`: evolution's autonomous mode, through the CLI."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.spec.loader import load_spec

RESEARCH_LAB = Path(__file__).resolve().parent / "fixtures" / "harnesses" / "research-lab"


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    target = tmp_path / "research-lab"
    shutil.copytree(RESEARCH_LAB, target, ignore=shutil.ignore_patterns(".hiveloom"))
    return target


def _evolve(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, ["evolve", *args, "--json"])
    assert result.exit_code == code, result.stdout
    return json.loads(result.stdout)


def test_research_mode_runs_unattended_and_queues_one_proposal(lab: Path):
    live = (lab / "harness.yaml").read_text()
    out = _evolve(str(lab), "--research", "--program", "auto")
    assert out["mode"] == "research" and out["program"] == "auto" and not out["resumed"]
    status = out["status"]
    assert status["status"] == "done" and status["stop_reason"]["condition"] == "ceiling"
    assert status["promotion"]["status"] == "pending"
    assert out["applied"] is None and (lab / "harness.yaml").read_text() == live
    assert Path(out["report"]).is_file()


def test_research_mode_with_yes_applies_a_supported_result(lab: Path):
    out = _evolve(str(lab), "--research", "--yes")
    assert out["applied"]["changed"]
    assert "convert grams to kilograms" in load_spec(lab).system_prompt


def test_research_mode_builds_a_charter_when_the_harness_has_none(lab: Path):
    (lab / "research.yaml").unlink()
    missing = _evolve(str(lab), "--research", "--model", "research_lab/director", code=3)
    assert "rate_quote" in missing["error"] and "--tool NAME=allow" in missing["error"]
    out = _evolve(str(lab), "--research", "--model", "research_lab/director",
                  "--tool", "rate_quote=allow", "--budget", "0.5", "--rounds", "3",
                  "--program", "built")
    assert out["status"]["status"] == "done"
    charter = (lab / ".hiveloom" / "research" / "built" / "research.yaml").read_text()
    assert "research_lab/director" in charter and "usd: 0.5" in charter
    assert "rounds: 3" in charter and "rate_quote: allow" in charter


def test_research_mode_resumes_by_name_and_refuses_a_finished_program(lab: Path):
    from hiveloom.research import service
    from hiveloom.research.program import Program

    # One unit only, as if the terminal closed; then resume by name.
    service.autonomous(lab, program="p", until="unit")
    assert Program(lab, "p").load_state()["unit"] == "survey"
    out = _evolve(str(lab), "--research", "--program", "p")
    assert out["resumed"] and out["status"]["status"] == "done"
    again = _evolve(str(lab), "--research", "--program", "p", code=3)
    assert "already finished" in again["error"]
    clash = _evolve(str(lab), "--research", "--program", "p", "--budget", "2", code=3)
    assert "resuming it uses its own charter" in clash["error"]


def test_research_mode_refuses_combinations_that_mean_something_else(lab: Path):
    mixed = _evolve(str(lab), "--research", "--experiment", "eval.yaml", "--yes", code=3)
    assert "cannot be combined" in mixed["error"]
    stray = _evolve(str(lab), "--budget", "1", code=3)
    assert "only apply with --research" in stray["error"]


def test_research_mode_options_layer_on_top_of_the_harness_charter(lab: Path):
    import yaml

    _evolve(str(lab), "--research", "--program", "big", "--budget", "3", "--rounds", "2")
    charter = yaml.safe_load(
        (lab / ".hiveloom" / "research" / "big" / "research.yaml").read_text())
    assert charter["budget"]["usd"] == 3.0 and charter["budget"]["rounds"] == 2
    assert charter["execution"]["tools"]["rate_quote"] == "allow"  # kept from research.yaml
    assert charter["models"]["director"] == "research_lab/director"
