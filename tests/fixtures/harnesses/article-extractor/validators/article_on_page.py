"""Deterministic cross-field and anti-hallucination validator for article-extractor.

Covers what the JSON-schema validator cannot express: ``source_url`` must be
exactly the run input, and the extracted title/headings must actually occur on
the live page (re-fetched here, independently of the model's own fetch). One
missing heading is tolerated — dynamic content can shift between the run's
fetch and this check; more than one means the model fabricated text. Every
check is rule-based: no model calls, no randomness.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from typing import Any


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _page_text(url: str) -> str:
    """Fetch the page and return entity-unescaped text, raw and tag-stripped.

    Both variants are needed: titles usually match the raw HTML, while
    headings with inline tags (<h2>Foo <em>bar</em></h2>) only match after
    tags are replaced by whitespace.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hiveloom validator)"})
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    unescaped = html.unescape(raw)
    stripped = re.sub(r"<[^>]+>", " ", unescaped)
    return _norm(unescaped) + " \x00 " + _norm(stripped)


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {
            "passed": False,
            "feedback": "Output is not valid JSON. Emit a single raw JSON object.",
        }
    if not isinstance(data, dict):
        return {"passed": False, "feedback": "Output must be a single JSON object."}

    expected_url = _norm(str(run_context.get("input", "")))
    source_url = data.get("source_url")
    if expected_url and source_url != expected_url:
        return {
            "passed": False,
            "feedback": (
                f"source_url must be exactly the input URL {expected_url!r}, "
                f"character for character; got {source_url!r}."
            ),
        }

    title = data.get("title")
    headings = data.get("headings")
    if not isinstance(title, str) or not title.strip() or not isinstance(headings, list):
        # Shape/type enforcement belongs to output_schema; failing here too
        # keeps this validator sound if it is ever run standalone.
        return {
            "passed": False,
            "feedback": (
                "title must be a non-empty string and headings an array "
                "(a null title means the fetch failed — the run must fail)."
            ),
        }

    try:
        page = _page_text(str(source_url))
    except OSError as exc:
        return {
            "passed": False,
            "feedback": f"Could not re-fetch {source_url} to verify the extraction: {exc}",
        }

    if _norm(title) not in page:
        return {
            "passed": False,
            "feedback": (
                f"Title {title!r} does not appear on the live page. Copy the title "
                "VERBATIM from the fetched HTML — never invent or paraphrase it."
            ),
        }

    missing = [h for h in headings if not isinstance(h, str) or _norm(h) not in page]
    if len(missing) > 1:
        return {
            "passed": False,
            "feedback": (
                "These headings do not appear on the live page — they look fabricated: "
                f"{missing!r}. Copy heading text verbatim from the HTML; omit anything "
                "you cannot find there."
            ),
        }
    return {"passed": True, "feedback": ""}
