"""Run-local verification evidence and grounded-reference validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.errors import SpecError
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import load_spec
from hiveloom.verify.base import ToolEvidenceRecord, VerificationContext
from hiveloom.verify.builtin import CodeVerifier, GroundedReferencesVerifier


def _context(*records: ToolEvidenceRecord) -> VerificationContext:
    return VerificationContext(run_id="run-current", tool_calls=records)


def _verifier() -> GroundedReferencesVerifier:
    return GroundedReferencesVerifier(
        output_path="$.selected[*].talent_id",
        evidence_paths=[
            {"tool": "search_candidates", "path": "$.candidates[*].talent_id"}
        ],
    )


def test_grounded_references_accept_observed_ids_and_normalize_scalars():
    verifier = _verifier()
    context = _context(
        ToolEvidenceRecord(
            id="search-1",
            name="search_candidates",
            result={
                "candidates": [
                    {"talent_id": 7},
                    {"talent_id": "8"},
                    {"talent_id": None},
                ]
            },
        )
    )

    verdict = verifier.validate(
        json.dumps(
            {
                "selected": [
                    {"talent_id": "7"},
                    {"talent_id": 8},
                    {"talent_id": "7"},
                    {"talent_id": None},
                    {},
                ]
            }
        ),
        {},
        context,
    )

    assert verdict.passed


def test_grounded_references_reject_unseen_ids_without_exposing_evidence():
    verifier = _verifier()
    context = _context(
        ToolEvidenceRecord(
            id="search-1",
            name="search_candidates",
            result={
                "candidates": [
                    {
                        "talent_id": "known",
                        "private_profile": "synthetic-private-profile",
                    }
                ]
            },
        )
    )

    verdict = verifier.validate(
        '{"selected": [{"talent_id": "invented"}]}', {}, context
    )

    assert not verdict.passed
    assert '"invented"' in verdict.feedback
    assert "synthetic-private-profile" not in verdict.feedback


def test_grounding_ignores_wrong_tools_and_failed_calls():
    verifier = _verifier()
    selected = '{"selected": [{"talent_id": "not-approved"}]}'
    context = _context(
        ToolEvidenceRecord(
            id="wrong-tool",
            name="read_deal",
            result={"candidates": [{"talent_id": "not-approved"}]},
        ),
        ToolEvidenceRecord(
            id="failed-search",
            name="search_candidates",
            result={"candidates": [{"talent_id": "not-approved"}]},
            is_error=True,
        ),
    )

    assert not verifier.validate(selected, {}, context).passed
    assert not verifier.validate(selected, {}, None).passed


def test_code_verifier_can_use_context_without_parsing_a_trace():
    received: list[VerificationContext] = []

    def check(_output, run_context, verification_context):
        assert run_context["verification_context"] is verification_context
        received.append(verification_context)
        return {"passed": verification_context.tool_calls[0].name == "search_candidates"}

    context = _context(
        ToolEvidenceRecord(id="search-1", name="search_candidates", result={})
    )
    verdict = CodeVerifier(check, name="check").validate(
        "{}", {"verification_context": context}, context
    )

    assert verdict.passed
    assert received == [context]


def _grounded_harness(tmp_path: Path) -> Path:
    harness = tmp_path / "grounded"
    construct.init_harness(harness, name="grounded", task="Select grounded records.")
    construct.add_tool(harness, builtin="file_read")
    construct.add_validator(
        harness,
        builtin="grounded_references",
        output_path="$.selected[*].talent_id",
        evidence_paths=[
            {"tool": "file_read", "path": "$.candidates[*].talent_id"}
        ],
        normalize="string",
    )
    construct.set_value(harness, "verify.on_fail.action", "abort")
    construct.set_value(
        harness,
        "loop.steps",
        [
            {
                "id": "search",
                "instruction": "Read the synthetic candidates.",
                "tools": ["file_read"],
                "require_tool_calls": ["file_read"],
            },
            {"id": "answer", "instruction": "Select candidates.", "tools": []},
        ],
    )
    construct.set_value(harness, "loop.policy", "sequential_steps")
    (harness / "candidates.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "talent_id": "candidate-1",
                        "profile": "synthetic-secret@example.com",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return harness


def test_grounding_uses_redacted_current_run_tool_evidence(tmp_path: Path):
    harness = _grounded_harness(tmp_path)
    construct.set_value(harness, "logging.redact", [r"synthetic-secret@example\.com"])
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "candidates.json"}),
            text_response('{"selected": [{"talent_id": "candidate-1"}]}'),
        ]
    )

    result = runner.run_harness(harness, "select", provider=provider)

    assert result.status == "success"
    assert [step.id for step in result.steps] == ["search", "answer"]
    trace_bytes = Path(result.trace_path).read_bytes()
    assert b"synthetic-secret@example.com" not in trace_bytes
    assert b"[REDACTED]" in trace_bytes


def test_schema_valid_but_ungrounded_output_fails_verification(tmp_path: Path):
    harness = _grounded_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "candidates.json"}),
            text_response('{"selected": [{"talent_id": "invented"}]}'),
        ]
    )

    result = runner.run_harness(harness, "select", provider=provider)

    assert result.status == "verify_failed"
    assert result.verdicts[0].verifier == "grounded_references"
    assert '"invented"' in result.verdicts[0].feedback


def test_invalid_grounding_path_rolls_back_construct_change(tmp_path: Path):
    harness = tmp_path / "invalid-grounding"
    construct.init_harness(harness, name="invalid-grounding", task="Validate paths.")
    construct.add_tool(harness, builtin="file_read")
    before = (harness / "harness.yaml").read_bytes()

    with pytest.raises(SpecError, match="JSON path"):
        construct.add_validator(
            harness,
            builtin="grounded_references",
            output_path="selected[*]",
            evidence_paths=[{"tool": "file_read", "path": "$.ids[*]"}],
        )

    assert (harness / "harness.yaml").read_bytes() == before
    assert load_spec(harness).verify.validators == []


def test_unknown_grounding_tool_rolls_back_construct_change(tmp_path: Path):
    harness = tmp_path / "invalid-tool"
    construct.init_harness(harness, name="invalid-tool", task="Validate tools.")
    before = (harness / "harness.yaml").read_bytes()

    with pytest.raises(SpecError, match="unknown evidence tool"):
        construct.add_validator(
            harness,
            builtin="grounded_references",
            output_path="$.ids[*]",
            evidence_paths=[{"tool": "prior_run_lookup", "path": "$.ids[*]"}],
        )

    assert (harness / "harness.yaml").read_bytes() == before
