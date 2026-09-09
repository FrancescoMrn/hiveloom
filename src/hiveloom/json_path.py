"""Small, deterministic JSON-path subset for declarative verification."""

from __future__ import annotations

import re
from typing import Any

_SEGMENT = re.compile(r"(?:\.([A-Za-z_][A-Za-z0-9_-]*))|(?:\[(\*|\d+)\])")


def parse_json_path(path: str) -> list[tuple[str, str | int | None]]:
    """Parse ``$``, dotted keys, list wildcards, and numeric list indexes."""
    if not path.startswith("$"):
        raise ValueError("JSON path must start with '$'")
    position = 1
    segments: list[tuple[str, str | int | None]] = []
    while position < len(path):
        match = _SEGMENT.match(path, position)
        if match is None:
            raise ValueError(
                "JSON path supports only .name, [*], and non-negative [index] segments"
            )
        key, index = match.groups()
        if key is not None:
            segments.append(("key", key))
        elif index == "*":
            segments.append(("wildcard", None))
        else:
            segments.append(("index", int(index)))
        position = match.end()
    return segments


def extract_json_path(value: Any, path: str) -> list[Any]:
    """Return every value selected by the supported path subset."""
    current = [value]
    for kind, argument in parse_json_path(path):
        selected: list[Any] = []
        for item in current:
            if kind == "key" and isinstance(item, dict) and argument in item:
                selected.append(item[argument])
            elif kind == "wildcard" and isinstance(item, list):
                selected.extend(item)
            elif (
                kind == "index"
                and isinstance(item, list)
                and isinstance(argument, int)
                and argument < len(item)
            ):
                selected.append(item[argument])
        current = selected
    return current
