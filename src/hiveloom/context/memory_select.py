"""Relevance-selected durable memory.

With ``memory.selection: relevant`` a run is not shown every lesson the
harness has learned, only the pinned ones plus those that match its task, up
to ``memory.max_selected``. A store can then keep growing as the harness
meets new cases without every run paying for — and being distracted by —
lessons about other cases.

Ranking is lexical and deterministic: tf-idf over each entry's title and
content against the task text, title matches weighted up, ties broken by
declaration order. Deterministic matters twice over: the same task always
gets the same prompt (a resumed or forked run rebuilds exactly the selection
its parent saw), and the selection is journaled as ``memory_selected`` so the
signal locator can contrast runs that were shown a lesson with runs that were
not — which is how a lesson that does not help becomes visible.

``search_memory`` is the way back to what was not selected: read-only, over
the same entries, ranked the same way.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from hiveloom.spec.schema import MemoryConfig, MemoryEntry
from hiveloom.tools.registry import Tool, ToolResult

SEARCH_MEMORY_TOOL = "search_memory"

_WORD = re.compile(r"[a-z0-9]+")
# Words too common to say anything about which lesson applies.
_STOPWORDS = frozenset(
    "the and for with that this from into your you are was were will have has had not "
    "but all any can each its it's their them then than there these those when what "
    "which who how why use used using out one two per via about over under only also "
    "must should never always".split()
)


def _fold(word: str) -> str:
    """Fold a plural onto its singular, so 'dates' matches 'date'. Deliberately
    minimal: a stemmer would conflate words a lesson means to keep apart."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _terms(text: str) -> list[str]:
    return [
        _fold(word) for word in _WORD.findall(text.casefold())
        if len(word) > 2 and word not in _STOPWORDS
    ]


def rank_entries(entries: list[MemoryEntry], query: str) -> list[tuple[MemoryEntry, float]]:
    """Entries with a positive match score for ``query``, best first."""
    query_terms = set(_terms(query))
    if not entries or not query_terms:
        return []
    documents = [
        (Counter(_terms(entry.title)), Counter(_terms(entry.content))) for entry in entries
    ]
    frequency = Counter(
        term for title, content in documents for term in set(title) | set(content)
    )
    total = len(entries)
    scored: list[tuple[int, MemoryEntry, float]] = []
    for position, (entry, (title, content)) in enumerate(zip(entries, documents, strict=True)):
        score = 0.0
        for term in query_terms:
            if term not in title and term not in content:
                continue
            idf = math.log(1 + total / frequency[term])
            score += idf * (1.5 if term in title else 1.0)
        if score > 0:
            scored.append((position, entry, score))
    scored.sort(key=lambda item: (-item[2], item[0]))
    return [(entry, score) for _, entry, score in scored]


def select_entries(config: MemoryConfig, task: str) -> tuple[list[MemoryEntry], dict[str, float]]:
    """The entries one run is shown, in declaration order, and their match scores.

    Pinned entries always; then the best matches for ``task`` until
    ``max_selected``. An entry that matches nothing is not shown, so a task
    unlike anything the harness has learned about gets a short memory section
    rather than a list of irrelevant rules.
    """
    pinned = [entry for entry in config.entries if entry.pinned]
    chosen = {entry.id for entry in pinned}
    scores: dict[str, float] = {}
    room = max(0, config.max_selected - len(pinned))
    for entry, score in rank_entries(
        [entry for entry in config.entries if not entry.pinned], task
    ):
        if room <= 0:
            break
        chosen.add(entry.id)
        scores[entry.id] = round(score, 4)
        room -= 1
    return [entry for entry in config.entries if entry.id in chosen], scores


class SearchMemoryTool(Tool):
    """Look up stored lessons that were not selected for this run (read-only)."""

    def __init__(self, config: MemoryConfig):
        self._config = config
        self.name = SEARCH_MEMORY_TOOL
        self.description = (
            "Search this harness's stored lessons from earlier runs by keywords. The "
            "memory section shows only the lessons that matched the task; use this "
            "when the work turns to something they do not cover."
        )
        self.tags = ["memory"]
        self.input_schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Most lessons to return (default 5).",
                },
            },
            "required": ["query"],
        }

    def run(self, query: str = "", limit: int = 5, **_: Any) -> ToolResult:
        try:
            count = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            count = 5
        ranked = rank_entries(self._config.entries, query)[:count]
        if not ranked:
            return ToolResult(content=f"no stored lesson matches {query!r}")
        lines = [
            f"- [{entry.kind}] {' '.join(entry.title.split())}: "
            f"{' '.join(entry.content.split())}"
            for entry, _score in ranked
        ]
        return ToolResult(content="\n".join(lines))
