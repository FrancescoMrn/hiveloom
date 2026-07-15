"""Semantic and anti-hallucination validator for the hn-extractor harness.

Re-fetches the Hacker News front page and requires every extracted story title
to actually appear there. One unmatched title is tolerated (front-page churn
between the run's fetch and this check); more than one means the model
fabricated stories. It also enforces that ``fetched_stories`` is the exact
number of returned stories and that ranks are sequential.
"""

from __future__ import annotations

import html
import json
import urllib.request
from typing import Any

HN = "https://news.ycombinator.com/"


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {"passed": False, "feedback": "Output is not valid JSON. Emit only a JSON object."}

    stories = data.get("stories")
    count = data.get("fetched_stories")
    if not isinstance(stories, list) or isinstance(count, bool) or not isinstance(count, int):
        return {
            "passed": False,
            "feedback": "'fetched_stories' must be an integer and 'stories' must be an array.",
        }
    if count != len(stories):
        return {
            "passed": False,
            "feedback": (
                f"fetched_stories is {count}, but stories contains {len(stories)} items. "
                "Set fetched_stories to the exact number of returned stories."
            ),
        }
    ranks = [story.get("rank") if isinstance(story, dict) else None for story in stories]
    expected_ranks = list(range(1, len(stories) + 1))
    if ranks != expected_ranks:
        return {
            "passed": False,
            "feedback": (
                f"story ranks must be sequential from 1 to {len(stories)}; got {ranks!r}."
            ),
        }
    # The documented fallback is valid after an http_get failure. Do not make
    # a second network request merely to reject an empty, honest fallback.
    if count == 0:
        return {"passed": True, "feedback": ""}

    try:
        page = urllib.request.urlopen(HN, timeout=15).read().decode("utf-8")
    except OSError as exc:
        return {"passed": False, "feedback": f"Could not re-fetch {HN} to verify titles: {exc}"}
    page_text = html.unescape(page)

    missing = [
        s.get("title", "")
        for s in stories
        if s.get("title", "").strip() not in page_text
    ]
    if len(missing) > 1:
        return {
            "passed": False,
            "feedback": (
                "These titles do NOT appear on the live Hacker News front page — "
                f"they look fabricated: {missing!r}. Copy titles VERBATIM from the "
                "fetched HTML only; never invent stories. If you cannot find 10 "
                "real stories in the HTML, return fewer."
            ),
        }
    return {"passed": True, "feedback": ""}
