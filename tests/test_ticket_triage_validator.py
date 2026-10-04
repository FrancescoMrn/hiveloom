"""ticket-triage's report check: complete, grounded and legal — judged against the data."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "ticket-triage"
_spec = importlib.util.spec_from_file_location(
    "check_triage", _HARNESS / "validators" / "check_triage.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _open_tickets() -> list[str]:
    rows = (_HARNESS / "data" / "tickets.jsonl").read_text().splitlines()
    return [json.loads(row)["id"] for row in rows if row and json.loads(row)["status"] == "open"]


def _report(ids: list[str], **overrides) -> str:
    entry = {"category": "bug", "priority": "normal", "reason": "Grounded in the body."}
    return json.dumps({"tickets": [{"id": i, **entry, **overrides} for i in ids]})


def test_a_complete_grounded_report_passes():
    assert check.validate(_report(_open_tickets()), {})["passed"]


def test_a_fenced_report_is_still_read():
    assert check.validate("```json\n" + _report(_open_tickets()) + "\n```", {})["passed"]


def test_each_gap_is_named_so_the_retry_can_fix_exactly_that():
    ids = _open_tickets()
    verdict = check.validate(_report([*ids[1:], ids[1], "TCK-9999", "TCK-1002"]), {})
    assert not verdict["passed"]
    feedback = verdict["feedback"]
    assert f"open but missing from the report: {ids[0]}" in feedback
    assert f"listed more than once: {ids[1]}" in feedback
    assert "not in the ticket system: TCK-9999" in feedback
    assert "closed, leave out: TCK-1002" in feedback


def test_labels_outside_the_allowed_sets_fail():
    verdict = check.validate(_report(_open_tickets(), priority="critical"), {})
    assert not verdict["passed"] and "priority must be one of" in verdict["feedback"]
