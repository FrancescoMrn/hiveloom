"""The delegation-lab demo, end to end and offline.

Two harnesses with scripted providers: a front desk with no ledger and a
ledger-desk specialist. The walkthrough is exercised through the CLI exactly
as its README runs it: referral while the peer is unmeasured, hand-off once it
has earned a record, verification by the desk, lineage, and routing that
declines a general question.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.logging.journal import read_events

LAB = Path(__file__).resolve().parents[1] / "harnesses" / "delegation-lab"
INVOICE = "What is the amount of invoice INV-1003?"


@pytest.fixture()
def lab(tmp_path: Path) -> Path:
    target = tmp_path / "delegation-lab"
    shutil.copytree(LAB, target, ignore=shutil.ignore_patterns(".hiveloom", "__pycache__"))
    peer = target / "peers" / "ledger-desk"
    _invoke("trust", str(peer))
    _invoke("registry", "add", str(peer))
    return target


def _invoke(*args: str) -> dict:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def _referrals(result: dict) -> list[tuple[str, str]]:
    return [(item["harness"], item["reason"]) for item in result["referrals"]]


def test_an_unmeasured_peer_is_referred_not_used(lab: Path):
    run = _invoke("run", str(lab), "--input-text", INVOICE)
    assert run["status"] == "success"
    assert "can't answer" in json.loads(run["output"])["answer"]
    assert run["delegations"] == []
    assert _referrals(run) == [("ledger-desk", "below_fitness")]


def test_a_measured_peer_takes_the_task_and_the_desk_verifies_it(lab: Path):
    peer = lab / "peers" / "ledger-desk"
    for invoice in ("INV-1001", "inv-1005", "INV-1009"):
        assert _invoke("run", str(peer), "--input-text", f"Amount of invoice {invoice}?")[
            "status"
        ] == "success"

    run = _invoke("run", str(lab), "--input-text", INVOICE)
    assert json.loads(run["output"]) == {
        "answer": "Invoice INV-1003 for Cobalt BV is 4200.00 EUR."
    }
    [delegation] = run["delegations"]
    assert (delegation["harness"], delegation["status"]) == ("ledger-desk", "success")
    events = [e["type"] for e in read_events(run["trace_path"])]
    assert {"delegation_selected", "delegation_started", "delegation_finished"} <= set(events)
    # The desk re-verified the adopted answer with its own validator.
    assert "verification_result" in events[events.index("delegation_finished"):]

    lineage = _invoke("lineage", run["run_id"])
    [child] = lineage["children"]
    assert (child["run_id"], child["lineage_kind"]) == (delegation["run_id"], "delegation")

    general = _invoke("run", str(lab), "--input-text", "How long does delivery take?")
    assert "three to five working days" in json.loads(general["output"])["answer"]
    assert general["delegations"] == []
    assert _referrals(general) == [("ledger-desk", "none_fit")]


def test_a_blocked_peer_answer_is_not_handed_back(lab: Path):
    peer = lab / "peers" / "ledger-desk"
    for invoice in ("INV-1001", "inv-1005", "INV-1009"):
        _invoke("run", str(peer), "--input-text", f"Amount of invoice {invoice}?")
    _invoke("add", "guardrail", "--builtin", "regex_output_filter", "--pattern", "Cobalt",
            "--dir", str(lab))
    result = CliRunner().invoke(app, ["run", str(lab), "--input-text", INVOICE, "--json"])
    run = json.loads(result.stdout)
    assert run["status"] == "guardrail_halt"
    assert run["output"] == ""
    assert "delegated output blocked" in run["reason"]
