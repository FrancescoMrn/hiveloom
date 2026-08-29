"""Contract tests for the executable validator in the article-extractor demo.

The validator re-fetches the page to catch invented headings, so most of it
needs the network. Everything tested here is a branch that returns *before*
the fetch — the cross-field and shape checks — plus the schema beside it. Those
are the parts a harness author is most likely to break while editing, and they
are checkable offline, so they are the ones under test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "article-extractor"
_VALIDATOR = _HARNESS / "validators" / "article_on_page.py"
_SCHEMA = _HARNESS / "schemas" / "output.json"

_URL = "https://example.com/post"


def _validate(output: str, context: dict | None = None) -> dict:
    spec = importlib.util.spec_from_file_location("article_validator", _VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate(output, context if context is not None else {})


def test_non_json_output_is_refused_before_any_fetch():
    result = _validate("Here is the article metadata: ...")
    assert result["passed"] is False
    assert "JSON" in result["feedback"]


def test_a_json_array_is_not_an_object():
    result = _validate('[{"source_url": "x"}]')
    assert result["passed"] is False
    assert "object" in result["feedback"]


def test_source_url_must_be_the_run_input_character_for_character():
    """The commonest silent wrong answer: the right page, a rewritten URL."""
    result = _validate(
        json.dumps({"source_url": _URL + "/", "title": "T", "headings": []}),
        {"input": _URL},
    )
    assert result["passed"] is False
    assert "source_url" in result["feedback"]


def test_a_null_title_fails_rather_than_passing_as_a_scrape_failure():
    """The fallback shape is for the model to emit; it is not a passing run."""
    result = _validate(
        json.dumps({"source_url": _URL, "title": None, "headings": []}),
        {"input": _URL},
    )
    assert result["passed"] is False
    assert "title" in result["feedback"]


def test_headings_must_be_an_array():
    result = _validate(
        json.dumps({"source_url": _URL, "title": "T", "headings": "one, two"}),
        {"input": _URL},
    )
    assert result["passed"] is False
    assert "headings" in result["feedback"]


def test_schema_accepts_the_documented_null_fields_and_caps_headings():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(
        {
            "source_url": _URL,
            "title": "A post",
            "description": None,
            "author": None,
            "published_date": None,
            "headings": [],
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "source_url": _URL,
                "title": "A post",
                "description": None,
                "author": None,
                "published_date": None,
                "headings": [str(i) for i in range(16)],
            },
            schema,
        )


def test_schema_refuses_a_date_that_was_not_normalised():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "source_url": _URL,
                "title": "A post",
                "description": None,
                "author": None,
                "published_date": "Jul 6, 2026",
                "headings": [],
            },
            schema,
        )
