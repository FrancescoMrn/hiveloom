"""Code-hook validator for the example summarizer harness.

Checks that the run output is a well-formed JSON summary that is genuinely
shorter than the source text. Returns actionable feedback so the harness model
can self-correct on retry.
"""

from __future__ import annotations

import json
from typing import Any


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    """Validate the structured summary.

    Args:
        run_output: The model's final output (expected to be JSON text).
        run_context: Runtime context; ``run_context["input"]`` holds the source.

    Returns:
        A dict with ``passed`` (bool) and ``feedback`` (str).
    """
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {"passed": False, "feedback": "Output is not valid JSON. Emit only a JSON object."}

    if not isinstance(data, dict):
        return {"passed": False, "feedback": "Top-level JSON must be an object."}

    for key in ("title", "summary", "key_points"):
        if key not in data:
            return {"passed": False, "feedback": f"Missing required key '{key}'."}

    if not isinstance(data["summary"], str) or not data["summary"].strip():
        return {"passed": False, "feedback": "'summary' must be a non-empty string."}
    if not isinstance(data["key_points"], list) or not data["key_points"]:
        return {"passed": False, "feedback": "'key_points' must be a non-empty array."}

    source = run_context.get("input", "") or ""
    if len(data["summary"]) >= len(source) and source:
        return {
            "passed": False,
            "feedback": "The summary must be shorter than the source text.",
        }

    return {"passed": True, "feedback": ""}
