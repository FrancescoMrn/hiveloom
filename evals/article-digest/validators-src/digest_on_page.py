"""Deterministic anti-hallucination validator for the article-digest task.

The digest is output-heavy: a written summary, five verbatim quotes, and a
heading outline. Everything checkable is checked in code, with no model calls:

- ``source_url`` must equal the run input exactly.
- Every quote must occur verbatim on the live page (re-fetched here,
  independently of the model's own fetch). One missing quote is tolerated —
  dynamic content can shift between the run's fetch and this check; more than
  one means the model fabricated text. Quotes must be distinct and
  sentence-sized (6-60 words).
- The summary must be original prose: 100-260 words, and NOT a verbatim copy
  of page text.
- Outline headings must occur verbatim on the page (one miss tolerated).

``check`` is pure (no I/O) so the eval scorer can reuse it with a cached page;
``validate`` is the hiveloom entry point that re-fetches the page itself.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from typing import Any

_SUMMARY_MIN_WORDS = 100
_SUMMARY_MAX_WORDS = 260
_QUOTE_MIN_WORDS = 6
_QUOTE_MAX_WORDS = 60


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _page_text(url: str) -> str:
    """Fetch the page; return entity-unescaped text, raw and tag-stripped."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hiveloom validator)"})
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    unescaped = html.unescape(raw)
    stripped = re.sub(r"<[^>]+>", " ", unescaped)
    return _norm(unescaped) + " \x00 " + _norm(stripped)


def check(data: Any, expected_url: str, page: str) -> tuple[list[str], dict[str, Any]]:
    """Return (problems, stats). Empty problems means the digest passes."""
    problems: list[str] = []
    stats: dict[str, Any] = {
        "summary_words": 0,
        "summary_verbatim": False,
        "missing_quotes": 0,
        "missing_outline": 0,
    }
    if not isinstance(data, dict):
        return (["Output must be a single JSON object."], stats)

    expected = _norm(str(expected_url))
    if expected and data.get("source_url") != expected:
        problems.append(
            f"source_url must be exactly the input URL {expected!r}, character for "
            f"character; got {data.get('source_url')!r}."
        )

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary must be a non-empty string of original prose.")
    else:
        words = len(summary.split())
        stats["summary_words"] = words
        if not (_SUMMARY_MIN_WORDS <= words <= _SUMMARY_MAX_WORDS):
            problems.append(
                f"summary must be {_SUMMARY_MIN_WORDS}-{_SUMMARY_MAX_WORDS} words; "
                f"it has {words}."
            )
        if _norm(summary) in page:
            stats["summary_verbatim"] = True
            problems.append(
                "summary appears verbatim on the page — write it in your own words, "
                "do not copy page text."
            )

    quotes = data.get("key_quotes")
    if not isinstance(quotes, list) or len(quotes) != 5 or not all(
        isinstance(q, str) and q.strip() for q in quotes
    ):
        problems.append("key_quotes must be exactly 5 non-empty strings.")
    else:
        normed = [_norm(q) for q in quotes]
        if len(set(normed)) != 5:
            problems.append("key_quotes must be 5 distinct passages.")
        bad_len = [
            q for q in quotes
            if not (_QUOTE_MIN_WORDS <= len(q.split()) <= _QUOTE_MAX_WORDS)
        ]
        if bad_len:
            problems.append(
                f"each quote must be {_QUOTE_MIN_WORDS}-{_QUOTE_MAX_WORDS} words; "
                f"offending: {bad_len!r}."
            )
        missing = [q for q, n in zip(quotes, normed, strict=True) if n not in page]
        stats["missing_quotes"] = len(missing)
        if len(missing) > 1:
            problems.append(
                "These quotes do not appear on the live page — they look fabricated "
                f"or paraphrased: {missing!r}. Copy quotes CHARACTER-FOR-CHARACTER "
                "from the fetched text; never edit or invent them."
            )

    outline = data.get("outline")
    if not isinstance(outline, list):
        problems.append("outline must be an array of heading strings.")
    else:
        missing_h = [h for h in outline if not isinstance(h, str) or _norm(h) not in page]
        stats["missing_outline"] = len(missing_h)
        if len(missing_h) > 1:
            problems.append(
                "These outline entries do not appear on the live page — they look "
                f"fabricated: {missing_h!r}. Copy heading text verbatim; omit "
                "anything you cannot find."
            )

    return (problems, stats)


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {
            "passed": False,
            "feedback": "Output is not valid JSON. Emit a single raw JSON object.",
        }

    url = str(run_context.get("input", ""))
    source_url = data.get("source_url") if isinstance(data, dict) else ""
    try:
        page = _page_text(str(source_url or url))
    except OSError as exc:
        return {
            "passed": False,
            "feedback": f"Could not re-fetch {source_url or url} to verify the digest: {exc}",
        }

    problems, _stats = check(data, url, page)
    return {"passed": not problems, "feedback": " ".join(problems)}
