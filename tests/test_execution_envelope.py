"""The public run envelope distinguishes clean, recovered, and failed execution."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response


def _harness(tmp_path: Path, *, validator: bool = False) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="execution-fixture", task="Return an answer.")
    if validator:
        construct.add_validator(directory, builtin="regex_match", pattern="^good$")
    return directory


def _response(text: str, *, model: str = "served-model", request_id: str = "req-1"):
    response = text_response(text, input_tokens=10, output_tokens=2)
    return response.model_copy(update={"model": model, "provider_request_id": request_id})


def test_clean_first_pass_has_complete_execution_receipt(tmp_path: Path):
    harness = _harness(tmp_path, validator=True)
    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider([_response("good")]),
        literal_input=True,
        run_id="case-1",
        ingest=False,
    )

    execution = result.execution
    assert execution is not None
    assert execution.run_id == "case-1"
    assert execution.status == "success"
    assert execution.harness_id
    assert execution.harness_name == "execution-fixture"
    assert execution.schema_version == "0.2.0"
    assert len(execution.behavior_hash) == 12
    assert len(execution.execution_fingerprint) == 64
    assert execution.hiveloom_version == "1.0.0"
    assert execution.requested_provider == "claude"
    assert execution.requested_model == "claude-haiku-4-5"
    assert execution.resolved_provider == "claude"
    assert execution.resolved_model == "claude-haiku-4-5"
    assert execution.effective_provider == "claude"
    assert execution.effective_model == "served-model"
    assert execution.usage.input_tokens == 10
    assert execution.usage.output_tokens == 2
    assert execution.cost_source == "estimated"
    assert execution.verification.model_dump() == {
        "attempts": 1,
        "first_pass_valid": True,
        "recovery_attempted": False,
        "recovered": False,
        "final_status": "passed",
    }
    assert datetime.fromisoformat(execution.started_at)
    assert datetime.fromisoformat(execution.finished_at)
    assert execution.duration_ms >= 0
    assert execution.trace_path == result.trace_path

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    assert events[0]["payload"]["runtime_config"] == result.runtime_config
    assert events[-1]["payload"]["execution"] == execution.model_dump(mode="json")
    assert runner.run_result_payload(result)["execution"] == execution.model_dump(mode="json")


def test_recovered_output_is_not_reported_as_clean(tmp_path: Path):
    harness = _harness(tmp_path, validator=True)
    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider([_response("bad"), _response("good", request_id="req-2")]),
        literal_input=True,
        ingest=False,
    )

    assert result.status == "success"
    assert result.execution is not None
    assert result.execution.verification.model_dump() == {
        "attempts": 2,
        "first_pass_valid": False,
        "recovery_attempted": True,
        "recovered": True,
        "final_status": "passed",
    }
    assert result.execution.usage.input_tokens == 20
    assert len(result.provider_calls) == 2


def test_final_verifier_failure_stays_distinct(tmp_path: Path):
    harness = _harness(tmp_path, validator=True)
    construct.set_value(harness, "verify.on_fail.max_retries", 0)

    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider([_response("bad")]),
        literal_input=True,
        ingest=False,
    )

    assert result.status == "verify_failed"
    assert result.execution is not None
    assert result.execution.verification.first_pass_valid is False
    assert result.execution.verification.recovery_attempted is False
    assert result.execution.verification.final_status == "failed"


@pytest.mark.parametrize(
    ("provider", "expected_status"),
    [
        (FakeModelProvider([text_response("expensive", output_tokens=300_000)]), "guardrail_halt"),
        (FakeModelProvider([RuntimeError("provider down")]), "error"),
    ],
)
def test_pre_verification_terminal_states_report_not_run(
    tmp_path: Path, provider: FakeModelProvider, expected_status: str
):
    harness = _harness(tmp_path, validator=True)

    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == expected_status
    assert result.execution is not None
    assert result.execution.verification.final_status == "not_run"
    assert result.execution.verification.first_pass_valid is None


def test_provider_reported_model_changes_execution_identity(tmp_path: Path):
    harness = _harness(tmp_path)
    requested = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider([_response("done", model="requested-model")]),
        literal_input=True,
        ingest=False,
    )
    substituted = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider([_response("done", model="other-family")]),
        literal_input=True,
        ingest=False,
    )

    assert requested.execution is not None
    assert substituted.execution is not None
    assert requested.execution.behavior_hash == substituted.execution.behavior_hash
    assert requested.execution.effective_model == "requested-model"
    assert substituted.execution.effective_model == "other-family"
    assert (
        requested.execution.execution_fingerprint
        != substituted.execution.execution_fingerprint
    )


def test_effective_model_is_the_last_call_even_when_a_model_repeats(tmp_path: Path):
    """A router path like A -> B -> A must report A, not the last *unique* model."""
    harness = _harness(tmp_path, validator=True)
    construct.set_value(harness, "verify.on_fail.max_retries", 2)

    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [
                _response("bad", model="model-a"),
                _response("worse", model="model-b", request_id="req-2"),
                _response("good", model="model-a", request_id="req-3"),
            ]
        ),
        literal_input=True,
        ingest=False,
    )

    assert result.status == "success"
    assert result.execution is not None
    assert len(result.provider_calls) == 3
    assert result.execution.effective_model == "model-a"
