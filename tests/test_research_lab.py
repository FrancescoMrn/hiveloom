"""The research-lab demo, end to end and offline, through the `hiveloom research` CLI.

The harness ships a scripted clerk and a scripted director, so this walks the
README exactly: a program starts from its charter, the director finds the
gram-weight defect from the runs behind the signal, the engine confirms and
keeps that fix, does not keep the generic idea, stops at the ceiling (the
contract discounts no prompt can reach), reads the sealed split once, and
queues a promotion that `proposals apply` lands on the live harness.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_LAB = REPO_ROOT / "tests" / "fixtures" / "harnesses" / "research-lab"
GRAMS = "Quote shipping for a 2500 g parcel to zone 2 for customer C-100."


@pytest.fixture()
def lab(tmp_path: Path) -> Path:
    target = tmp_path / "research-lab"
    shutil.copytree(RESEARCH_LAB, target, ignore=shutil.ignore_patterns(".hiveloom", ".venv"))
    return target


def _invoke(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == code, result.stdout
    return json.loads(result.stdout)


def test_a_research_program_improves_the_desk_and_stops_at_the_ceiling(lab: Path):
    live = (lab / "harness.yaml").read_text()
    started = _invoke("research", "init", str(lab), "--name", "quotes",
                      "--charter", str(lab / "research.yaml"), "--approve")
    assert (started["working"], started["holdout"]) == (18, 6)

    finished = _invoke("research", "run", str(lab), "--name", "quotes")
    # Round two finds nothing to try; only then is the ceiling declared.
    assert [s["unit"] for s in finished["steps"]] == [
        "baseline", "survey", "hypothesize", "experiment", "experiment", "interpret",
        "survey", "hypothesize", "interpret", "survey", "confirm", "report",
    ]
    status = finished["status"]
    assert [(e["verdict"], e["kept"]) for e in status["experiments"]] == [
        ("confirmed", True), ("inconclusive", False),
    ]
    assert status["stop_reason"]["condition"] == "ceiling"
    assert "fetch what the model lacks" in status["stop_reason"]["recommendation"]
    assert status["confirmation"]["strength"] == "supported"
    assert status["ledger"]["ok"]
    assert (lab / "harness.yaml").read_text() == live  # the program never touches it

    report = _invoke("research", "report", str(lab), "--name", "quotes")["report"]
    assert "**Stopped:** ceiling" in report and "Calibration" in report

    listed = _invoke("research", "status", str(lab))["programs"]
    assert [(p["name"], p["status"]) for p in listed] == [("quotes", "done")]

    # The desk fails gram requests until the promotion is applied.
    before = CliRunner().invoke(app, ["run", str(lab), "--input-text", GRAMS, "--json"])
    assert before.exit_code == 1
    proposal_id = status["promotion"]["proposal_id"]
    [queued] = _invoke("proposals", "list", str(lab))["proposals"]
    assert (queued["id"], queued["trigger"]) == (proposal_id, "research")
    _invoke("proposals", "apply", str(lab), proposal_id, "--yes")
    after = _invoke("run", str(lab), "--input-text", GRAMS)
    assert after["status"] == "success" and json.loads(after["output"])["price"] == 9.0


def test_the_cli_steps_stops_and_refuses_an_unfinished_report(lab: Path):
    _invoke("research", "init", str(lab), "--name", "q", "--charter",
            str(lab / "research.yaml"), "--approve")
    stepped = _invoke("research", "step", str(lab), "--name", "q")
    assert [s["unit"] for s in stepped["steps"]] == ["baseline"]
    unfinished = _invoke("research", "report", str(lab), "--name", "q", code=3)
    assert "has not finished" in unfinished["error"]

    _invoke("research", "stop", str(lab), "--name", "q", "--reason", "enough")
    done = _invoke("research", "run", str(lab), "--name", "q")["status"]
    assert done["stop_reason"] == {"condition": "user", "detail": "enough"}
    assert done["experiments"] == [] and done["promotion"] is None


def test_an_unclassified_tool_stops_a_program_before_anything_runs(lab: Path, tmp_path: Path):
    charter = (lab / "research.yaml").read_text().replace(
        "execution:\n  tools:\n    rate_quote: allow    # read-only lookup of a local table\n", ""
    )
    (tmp_path / "bare.yaml").write_text(charter)
    failed = _invoke("research", "init", str(lab), "--name", "bare", "--charter",
                     str(tmp_path / "bare.yaml"), "--approve", code=3)
    assert "rate_quote" in failed["error"] and "execution.tools" in failed["error"]
    assert not (lab / ".hiveloom" / "research" / "bare").exists()
