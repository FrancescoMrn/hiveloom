"""A run that ended as "error" says what kind of failure it was, by exception type."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, eval_runner, runner
from hiveloom.loop import policies
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import ContextOverflowError, ProviderError, ProviderUnreachable


def _no_verify(harness_dir: Path) -> None:
    construct.set_field(harness_dir, "loop.require_verification", "false")


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (ProviderUnreachable("provider unreachable at http://x: refused"), "provider_unreachable"),
        (ProviderError("provider returned HTTP 500: boom"), "provider_error"),
        (ContextOverflowError("too long"), "context_overflow"),
        (RuntimeError("something else"), "unknown"),
    ],
)
def test_a_model_failure_names_its_kind(harness_dir: Path, exc, kind):
    _no_verify(harness_dir)
    result = runner.run_harness(harness_dir, "go", provider=FakeModelProvider([exc]))
    assert result.status == "error"
    assert result.failure_kind == kind


def test_a_tool_abort_is_its_own_kind(harness_dir: Path):
    _no_verify(harness_dir)
    construct.add_tool(harness_dir, builtin="file_read")
    construct.set_field(harness_dir, "loop.on_tool_error", "abort")
    result = runner.run_harness(
        harness_dir,
        "go",
        provider=FakeModelProvider([tool_response("file_read", {"path": "missing.txt"})]),
    )
    assert result.status == "error"
    assert result.failure_kind == "tool_abort"


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (KeyError("a hook bug"), "policy"),
        # A policy's own model call (a plan phase) fails like the main loop's.
        (ProviderUnreachable("provider unreachable at http://x: refused"), "provider_unreachable"),
    ],
)
def test_a_policy_failure_is_its_own_kind_unless_a_typed_one(
    harness_dir: Path, monkeypatch, exc, kind
):
    _no_verify(harness_dir)

    def explode(self, loop):
        raise exc

    monkeypatch.setattr(policies.ReactPolicy, "on_run_start", explode)
    provider = FakeModelProvider([text_response("42")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "error"
    assert result.reason.startswith(f"policy failed: {type(exc).__name__}")
    assert result.failure_kind == kind


def test_a_run_that_did_not_fail_has_no_kind(harness_dir: Path):
    _no_verify(harness_dir)
    provider = FakeModelProvider([text_response("42")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert result.failure_kind == ""


def test_the_kind_is_in_the_journal_and_survives_trace_recovery(harness_dir: Path):
    _no_verify(harness_dir)
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([ProviderUnreachable("refused")])
    )
    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    finished = next(e for e in events if e["type"] == "run_finished")
    assert finished["payload"]["failure_kind"] == "provider_unreachable"
    recovered = eval_runner._result_from_trace(result.trace_path)
    assert recovered is not None
    assert recovered.failure_kind == "provider_unreachable"
    assert runner.run_result_payload(result)["failure_kind"] == "provider_unreachable"
