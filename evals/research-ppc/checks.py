"""Deterministic pilot checks. Expected records never enter executor input."""

import json
from pathlib import Path


def measurements(output, expected):
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        value = None
    shape = isinstance(value, dict) and set(value) == {"title", "quote", "url"}
    return {
        "json_correct": float(shape),
        "title_correct": float(shape and value.get("title") == expected["title"]),
        "quote_complete": float(shape and value.get("quote") == expected["quote"]),
        "source_correct": float(shape and value.get("url") == expected["url"]),
    }


def validate(run_output, run_context):
    task = str(run_context.get("input", ""))
    records = json.loads(Path(__file__).with_name("pages.json").read_text())
    record = next((r for r in records if task == r["input"]), None)
    if record is None:
        return {"passed": False, "feedback": "Unknown snapshot input"}
    metrics = measurements(run_output, record["expected"])
    failed = [name for name, value in metrics.items() if not value]
    return {"passed": not failed, "feedback": "Failed checks: " + ", ".join(failed)}
