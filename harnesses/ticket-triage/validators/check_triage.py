"""The triage report must account for the real queue — every open ticket, once.

It reads the same `data/tickets.jsonl` the MCP server serves, so it checks the
report against the system of record rather than against what the model says
it saw: an open ticket left out, a closed or unknown id, a duplicate, or a
label outside the allowed sets each fail with feedback that names the tickets,
which is what the retry needs to fix exactly that and nothing else.

Judgement stays with the model: which category and priority a ticket deserves
is not checked here — only that the report is complete, grounded and legal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parents[1] / "data" / "tickets.jsonl"
_CATEGORIES = {"bug", "security", "billing", "how-to", "feature-request"}
_PRIORITIES = {"urgent", "high", "normal", "low"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _queue() -> tuple[set[str], set[str]]:
    rows = [json.loads(line) for line in _DATA.read_text(encoding="utf-8").splitlines() if line]
    return {row["id"] for row in rows}, {row["id"] for row in rows if row["status"] == "open"}


def _ids(items: list[str]) -> str:
    return ", ".join(sorted(items))


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    text = _FENCE.sub("", (run_output or "").strip())
    try:
        report = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"passed": False, "feedback": "Emit one raw JSON object: {\"tickets\": [...]}."}
    entries = report.get("tickets") if isinstance(report, dict) else None
    if not isinstance(entries, list):
        return {"passed": False, "feedback": 'The report needs a "tickets" list.'}

    known, open_ids = _queue()
    seen: list[str] = []
    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            problems.append("every entry needs a string id")
            continue
        ticket = entry["id"]
        seen.append(ticket)
        if entry.get("category") not in _CATEGORIES:
            problems.append(f"{ticket}: category must be one of {sorted(_CATEGORIES)}")
        if entry.get("priority") not in _PRIORITIES:
            problems.append(f"{ticket}: priority must be one of {sorted(_PRIORITIES)}")
        if not str(entry.get("reason") or "").strip():
            problems.append(f"{ticket}: give a one-sentence reason")

    invented = {ticket for ticket in seen if ticket not in known}
    closed = {ticket for ticket in seen if ticket in known and ticket not in open_ids}
    duplicated = {ticket for ticket in seen if seen.count(ticket) > 1}
    missing = open_ids - set(seen)
    if invented:
        problems.append(f"not in the ticket system: {_ids(invented)}")
    if closed:
        problems.append(f"closed, leave out: {_ids(closed)}")
    if duplicated:
        problems.append(f"listed more than once: {_ids(duplicated)}")
    if missing:
        problems.append(f"open but missing from the report: {_ids(missing)}")

    if problems:
        return {"passed": False, "feedback": "; ".join(problems)}
    return {"passed": True}
