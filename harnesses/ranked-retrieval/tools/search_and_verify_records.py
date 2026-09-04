"""Search synthetic records and enforce eligibility before exposing results."""

from __future__ import annotations

import json
import re
from pathlib import Path

from hiveloom.tools import tool

_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "records.json"
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "about",
    "and",
    "for",
    "find",
    "of",
    "on",
    "records",
    "the",
    "to",
}


def _words(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.lower())) - _STOP_WORDS


@tool(
    description=(
        "Search local knowledge records and return only published, quality-verified "
        "candidates in deterministic rank order."
    ),
    tags=["retrieval", "read", "deterministic"],
    guidelines=(
        "Call search_and_verify_records once with the user's query. Only IDs in its "
        "candidates array may appear in the final answer."
    ),
)
def search_and_verify_records(query: str, limit: int = 5) -> str:
    """Search and verify in one operation so ineligible hits are never exposed."""
    requested_limit = max(1, min(limit, 10))
    records = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    query_words = _words(query)
    candidates: list[dict] = []
    excluded = 0

    for record in records:
        # This is the invariant the composite tool owns. Search results do not
        # cross the tool boundary until both eligibility checks have passed.
        if record["status"] != "published" or not record["quality_verified"]:
            excluded += 1
            continue
        title_words = _words(record["title"])
        tag_words = _words(" ".join(record["tags"]))
        summary_words = _words(record["summary"])
        matched = query_words & (title_words | tag_words | summary_words)
        score = (
            3 * len(query_words & title_words)
            + 2 * len(query_words & tag_words)
            + len(query_words & summary_words)
        )
        if score == 0:
            continue
        candidates.append(
            {
                "record_id": record["record_id"],
                "title": record["title"],
                "summary": record["summary"],
                "score": score,
                "matched_terms": sorted(matched),
            }
        )

    candidates.sort(key=lambda item: (-item["score"], item["record_id"]))
    return json.dumps(
        {
            "query": query,
            "candidates": candidates[:requested_limit],
            "excluded_ineligible": excluded,
        },
        sort_keys=True,
    )
