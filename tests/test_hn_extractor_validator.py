"""Contract tests for the executable validator in the HN demo harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

_VALIDATOR = (
    Path(__file__).resolve().parents[1]
    / "harnesses"
    / "hn-extractor"
    / "validators"
    / "titles_on_page.py"
)
_SCHEMA = _VALIDATOR.parents[1] / "schemas" / "output.json"


def _validate(output: str) -> dict:
    spec = importlib.util.spec_from_file_location("hn_validator", _VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate(output, {})


def test_hn_validator_rejects_mismatched_declared_count_without_network():
    result = _validate(
        '{"fetched_stories": 10, "stories": '
        '[{"rank": 1, "title": "one"}, {"rank": 2, "title": "two"}]}'
    )
    assert result["passed"] is False
    assert "exact number" in result["feedback"]


def test_hn_validator_accepts_documented_empty_fallback_without_network():
    result = _validate('{"fetched_stories": 0, "stories": []}')
    assert result == {"passed": True, "feedback": ""}


def test_hn_validator_rejects_non_sequential_ranks_without_network():
    result = _validate(
        '{"fetched_stories": 2, "stories": '
        '[{"rank": 1, "title": "one"}, {"rank": 3, "title": "two"}]}'
    )
    assert result["passed"] is False
    assert "sequential" in result["feedback"]


def test_hn_schema_allows_documented_empty_fallback_and_caps_story_count():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(
        {"source": "https://news.ycombinator.com/", "fetched_stories": 0, "stories": []},
        schema,
    )
    eleven_stories = [{"rank": i, "title": str(i), "url": "https://x"} for i in range(1, 12)]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "source": "https://news.ycombinator.com/",
                "fetched_stories": 11,
                "stories": eleven_stories,
            },
            schema,
        )
