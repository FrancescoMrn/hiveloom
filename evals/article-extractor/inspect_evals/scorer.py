"""Deterministic scorer shared by all four arms.

task_success = schema_valid AND no hallucination AND title matches golden.
The hallucination gate is the canonical harness validator (re-fetches the live
page). The expected-404 edge sample is scored as: emitted exactly the
failed-fetch fallback JSON (the schema deliberately rejects it — a failed
scrape must not pass as a normal run).
"""

from __future__ import annotations

import asyncio
import difflib
import json
from collections import Counter

import jsonschema
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer, stderr
from inspect_ai.solver import TaskState

from inspect_evals._shared import CANONICAL_HARNESS, load_page_validator, norm

_SCHEMA = json.loads((CANONICAL_HARNESS / "schemas" / "output.json").read_text())
_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMA)
_HALLUC_RETRIES = 3


_MISSING = object()


def _strip_fences(text: str) -> str:
    # The harness has a strip_json_fence hook; raw arms don't. Stripping one
    # fence layer uniformly keeps the grading bar identical across arms
    # (disclosed leniency in RESULTS.md). An unclosed fence (truncated output)
    # is stripped too, so truncation is judged on the JSON, not the fence.
    t = text.strip()
    if t.startswith("```"):
        t = t.partition("\n")[2]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    return t


def _norm_or_none(value) -> str | None:
    return norm(value).lower() if isinstance(value, str) else None


def _field_match(data: dict, key: str, golden_value) -> bool:
    got = data.get(key, _MISSING)
    if got is _MISSING:
        return False  # key absent is a schema violation, never a match
    if key == "description":
        if golden_value is None or got is None:
            return golden_value is None and got is None
        a, b = norm(got).lower(), norm(golden_value).lower()
        return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6
    if key == "published_date":
        return got == golden_value
    return _norm_or_none(got) == _norm_or_none(golden_value)


def _headings_f1(got, golden) -> float:
    # Multisets, not sets: duplicate golden headings (deliberately present in
    # the dataset) must be matched copy-for-copy.
    got_counts = Counter(norm(h).lower() for h in got if isinstance(h, str))
    golden_counts = Counter(norm(h).lower() for h in golden)
    if not got_counts and not golden_counts:
        return 1.0
    if not got_counts or not golden_counts:
        return 0.0
    tp = sum((got_counts & golden_counts).values())
    precision = tp / sum(got_counts.values())
    recall = tp / sum(golden_counts.values())
    return 2 * precision * recall / (precision + recall) if tp else 0.0


async def _hallucination_check(validate, raw: str, url: str) -> dict:
    # The validator re-fetches the live page; retry so scoring-time network
    # flakiness doesn't manufacture a false failure.
    for attempt in range(_HALLUC_RETRIES):
        result = await asyncio.to_thread(validate, raw, {"input": url})
        if not result["feedback"].startswith("Could not re-fetch"):
            break
        if attempt < _HALLUC_RETRIES - 1:
            await asyncio.sleep(2**attempt)
    return result


@scorer(metrics=[accuracy(), stderr()])
def article_extractor_scorer():
    validate = load_page_validator()

    async def score(state: TaskState, target: Target) -> Score:
        golden = state.metadata["golden"]
        url = golden["source_url"]
        raw = _strip_fences(state.output.completion or "")

        try:
            data = json.loads(raw)
            json_valid = isinstance(data, dict)
        except json.JSONDecodeError:
            data, json_valid = None, False

        base_metadata = {
            "json_valid": json_valid,
            "hiveloom_result": state.metadata.get("hiveloom_result"),
            "latency_seconds": state.metadata.get("latency_seconds"),
        }

        if golden["title"] is None:
            # Expected-404 sample: graceful failure means emitting exactly the
            # failed-fetch fallback JSON.
            success = json_valid and data == golden
            return Score(
                value=CORRECT if success else INCORRECT,
                metadata={**base_metadata, "edge_404": True},
            )

        schema_valid = json_valid and _VALIDATOR.is_valid(data)
        halluc = (
            await _hallucination_check(validate, raw, url)
            if json_valid
            else {"passed": False, "feedback": "output is not valid JSON"}
        )
        title_match = json_valid and _field_match(data, "title", golden["title"])
        task_success = schema_valid and halluc["passed"] and title_match

        return Score(
            value=CORRECT if task_success else INCORRECT,
            metadata={
                **base_metadata,
                "schema_valid": schema_valid,
                "hallucination_passed": halluc["passed"],
                "hallucination_feedback": halluc["feedback"][:200],
                "title_match": title_match,
                "author_match": json_valid and _field_match(data, "author", golden["author"]),
                "date_match": json_valid
                and _field_match(data, "published_date", golden["published_date"]),
                "description_match": json_valid
                and _field_match(data, "description", golden["description"]),
                "headings_f1": _headings_f1(data.get("headings", []), golden["headings"])
                if json_valid
                else 0.0,
            },
        )

    return score
