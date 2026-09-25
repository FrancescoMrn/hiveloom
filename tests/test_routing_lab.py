"""The routing-lab demo: a pinned plan, playbook routing, and a measured evolution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.logging.journal import read_events

LAB = Path(__file__).resolve().parents[1] / "harnesses" / "routing-lab"
FORCED = "FORCE_FAIL: handle incident.txt"


@pytest.fixture()
def lab(tmp_path: Path) -> Path:
    target = tmp_path / "routing-lab"
    shutil.copytree(LAB, target, ignore=shutil.ignore_patterns(".hiveloom", "__pycache__"))
    return target


def _run(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == code, result.stdout
    return json.loads(result.stdout)


def test_a_pinned_plan_then_declared_routing(lab: Path):
    run = _run("run", str(lab), "--input", str(lab / "incident.txt"))
    assert run["status"] == "success"
    events = read_events(run["trace_path"])
    calls = [e["payload"].get("phase") for e in events if e["type"] == "model_call"]
    assert calls[0] == "plan"
    systems = [e["payload"]["system"] for e in events if e["type"] == "context_system"]
    assert "# Plan" in systems[-1] and "decide playbook" in systems[-1]
    [swap] = [e["payload"] for e in events if e["type"] == "model_swap"]
    assert swap["source"] == "playbook"
    # Declared routing is the spec: the run stays in its fitness bucket.
    [bucket] = _run("stats", str(lab))["versions"]
    assert (bucket["runs"], bucket["swapped_runs"]) == (1, 0)


def test_an_aimed_evolution_is_confirmed(lab: Path):
    _run("run", str(lab), "--input", str(lab / "incident.txt"))
    for _ in range(3):
        _run("run", str(lab), "--input-text", FORCED, code=1)
    signal = _run("signal", str(lab))
    assert signal["statuses"] == {"verify_failed": 3}
    proposal = _run(
        "evolve", str(lab), "--propose", "--model", "routing_lab/qa-evolver"
    )
    assert proposal["proposal"]["target"]["signal"] == "status:verify_failed"
    _run("proposals", "apply", str(lab), proposal["id"], "--yes")
    for _ in range(5):
        assert _run("run", str(lab), "--input-text", FORCED)["status"] == "success"
    [assessment] = _run("assess", str(lab))["assessments"]
    assert assessment["verdict"] == "confirmed"
    measure = assessment["target_measure"]
    assert (measure["before_count"], measure["after_count"], measure["after_n"]) == (3, 0, 5)
