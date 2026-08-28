"""Code validator for example-summarizer: is this a real summary?

The JSON-schema check beside it proves the *shape*. This proves the two things
a schema cannot express: that the fields carry content, and that the summary is
actually shorter than what it summarised. Feedback is written to be read by the
model on retry, so each message says what to change rather than what was wrong.
"""

from __future__ import annotations

import json
from typing import Any


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"passed": bool, "feedback": str}`` for one run's output."""
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {
            "passed": False,
            "feedback": "Output is not valid JSON. Emit a single JSON object and nothing else.",
        }
    if not isinstance(data, dict):
        return {"passed": False, "feedback": "The top-level JSON value must be an object."}

    missing = [key for key in ("title", "summary", "key_points") if key not in data]
    if missing:
        return {"passed": False, "feedback": f"Add the missing key(s): {', '.join(missing)}."}

    if not isinstance(data["title"], str) or not data["title"].strip():
        return {"passed": False, "feedback": "'title' must be a non-empty string."}
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        return {"passed": False, "feedback": "'summary' must be a non-empty string."}
    if not isinstance(data["key_points"], list) or not data["key_points"]:
        return {"passed": False, "feedback": "'key_points' must be a non-empty array of strings."}
    if any(not isinstance(point, str) or not point.strip() for point in data["key_points"]):
        return {
            "passed": False,
            "feedback": "Every entry in 'key_points' must be a non-empty string.",
        }

    # `input` is the run input: the path when the harness was given a file, so
    # the length check only applies once there is a source to compare against.
    source = str(run_context.get("input") or "")
    if source and len(data["summary"]) >= len(source):
        return {
            "passed": False,
            "feedback": (
                "The summary is not shorter than the source text. Condense it: "
                "keep the claims, drop the restatement."
            ),
        }
    return {"passed": True, "feedback": ""}
