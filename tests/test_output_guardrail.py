"""A blocked output is never what a run hands back."""

from __future__ import annotations

from pathlib import Path

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response

LEAK = "Here it is: AKIA1234567890ABCDEF"


def _harness(harness_dir: Path, max_turns: int) -> Path:
    construct.add_guardrail(harness_dir, builtin="regex_output_filter", pattern=r"AKIA[0-9A-Z]{16}")
    construct.set_field(harness_dir, "loop.max_turns", str(max_turns))
    return harness_dir


def test_a_run_that_never_complies_returns_no_output(harness_dir: Path):
    result = runner.run_harness(
        _harness(harness_dir, 2), "give me a key",
        provider=FakeModelProvider([text_response(LEAK), text_response(LEAK)]),
    )
    assert result.status == "max_turns"
    assert result.output == ""
    assert "last output blocked" in result.reason
    assert "AKIA1234567890ABCDEF" not in result.reason


def test_a_compliant_rewrite_is_returned(harness_dir: Path):
    result = runner.run_harness(
        _harness(harness_dir, 3), "give me a key",
        provider=FakeModelProvider(
            [text_response(LEAK), text_response("Keys start with AKIA; I won't invent one.")]
        ),
    )
    assert result.status == "success"
    assert result.output == "Keys start with AKIA; I won't invent one."
