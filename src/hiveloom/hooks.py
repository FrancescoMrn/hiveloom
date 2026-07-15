"""Small deterministic builtin event hooks.

These are deliberately conservative: they only repair a known presentation
wrapper, never attempt to extract JSON from arbitrary prose. Harnesses opt in
through ``hooks:`` so their output contract remains explicit.
"""

from __future__ import annotations

import re
from typing import Any

from hiveloom import ext

_JSON_FENCE = re.compile(
    r"^\s*```(?:json)?\s*\n?(\{.*\})\s*```\s*$", re.IGNORECASE | re.DOTALL
)


def strip_json_fence(event: dict[str, Any]) -> dict[str, str] | None:
    """Unwrap a final JSON object when Markdown fencing is its only wrapper."""
    output = event.get("output")
    if not isinstance(output, str):
        return None
    match = _JSON_FENCE.match(output)
    if match is None:
        return None
    return {"output": match.group(1).strip()}


def _register_factories() -> None:
    ext.register_builtin_factory(
        "hooks", "strip_json_fence", lambda _params, _ctx: strip_json_fence
    )


_register_factories()
