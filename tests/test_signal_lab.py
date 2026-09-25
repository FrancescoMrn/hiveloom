"""The signal-lab demo, end to end and offline.

The harness ships its own scripted provider, so this exercises signal-driven
evolution through the CLI exactly as its README walks through it: the signal
map locates the failing tool, reflection drafts a lesson for review, and
`evolve --experiment` reverts a refuted change and keeps a confirmed one,
which `assess` then reports against each change's own prediction.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.logging.journal import read_events

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNAL_LAB = REPO_ROOT / "harnesses" / "signal-lab"
UPPER = "Look up invoice INV-1003 and report its amount."
LOWER = "Look up invoice inv-1004 and report its amount."


@pytest.fixture()
def lab(tmp_path: Path) -> Path:
    target = tmp_path / "signal-lab"
    shutil.copytree(SIGNAL_LAB, target, ignore=shutil.ignore_patterns(".hiveloom", ".venv"))
    return target


def _invoke(*args: str) -> dict:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def test_the_clerk_answers_from_the_tool_and_fails_on_lowercase_ids(lab: Path):
    found = _invoke("run", str(lab), "--input-text", UPPER)
    assert found["status"] == "success"
    assert json.loads(found["output"]) == {
        "invoice_id": "INV-1003", "status": "found", "amount": 4200.0,
    }
    missed = CliRunner().invoke(app, ["run", str(lab), "--input-text", LOWER, "--json"])
    assert missed.exit_code == 1  # verify_failed
    assert json.loads(json.loads(missed.stdout)["output"])["status"] == "not_found"


def test_a_failed_run_is_reflected_into_a_lesson_for_review(lab: Path):
    CliRunner().invoke(app, ["run", str(lab), "--input-text", LOWER, "--json"])
    [queued] = _invoke("proposals", "list", str(lab))["proposals"]
    assert (queued["trigger"], queued["status"]) == ("reflect", "pending")
    lesson = queued["proposal"]["yaml_changes"][0]["value"]
    assert "uppercase" in lesson["content"] and "'inv-1004'" in lesson["evidence"]
    # Reviewed, never applied by itself.
    assert len(_invoke("memory", "list", str(lab))["entries"]) == 2


def test_the_measured_loop_reverts_the_refuted_idea_and_keeps_the_lesson(lab: Path):
    _invoke("eval", "run", str(lab / "eval.yaml"), "--approve")
    signal = _invoke("signal", str(lab))
    assert signal["verdict"] == "actionable"
    top = signal["signals"][0]
    assert top["feature"] == "tool_error:lookup_invoice"
    assert (top["failures_with"], top["successes_with"]) == (8, 0)
    assert signal["loss"]["classes"] == {"tooling": 8}

    result = _invoke(
        "evolve", str(lab), "--experiment", str(lab / "eval.yaml"), "--yes",
        "--rounds", "2", "--model", "signal_lab/qa-evolver",
    )
    first, second = result["rounds"]
    assert (first["status"], first["assessment"]["verdict"]) == ("reverted", "refuted")
    assert first["changed_paths"] == ["system_prompt"]
    assert (second["status"], second["assessment"]["verdict"]) == ("kept", "confirmed")
    assert second["changed_paths"] == ["memory.entries.+"]
    measure = second["assessment"]["success"]
    assert (measure["improved_pairs"], measure["worsened_pairs"]) == (8, 0)
    # Round two was chosen from round one's measured verdict.
    assert first["target"]["signal"] == second["target"]["signal"]

    verdicts = [
        (a["verdict"], a["decision"]["action"]) for a in _invoke("assess", str(lab))["assessments"]
    ]
    assert verdicts == [("confirmed", "kept"), ("refuted", "reverted")]

    after = _invoke(
        "run", str(lab), "--input-text", "Look up invoice inv-1010 and report its amount."
    )
    assert json.loads(after["output"])["amount"] == 618.0
    selected = [e["payload"] for e in read_events(after["trace_path"])
                if e["type"] == "memory_selected"]
    assert selected[0]["ids"] == ["amounts-are-in-eur", "invoice-ids-are-uppercase"]
    assert selected[0]["stored"] == 3
