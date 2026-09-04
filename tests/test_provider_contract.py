"""Public provider response metadata and OpenAI-compatible codec tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hiveloom import construct, runner
from hiveloom.loop.agent_loop import AgentLoop
from hiveloom.models.fake import FakeModelProvider
from hiveloom.models.openai_compat import (
    normalize_openai_response,
    to_openai_messages,
    to_openai_tool,
)
from hiveloom.models.provider import (
    PROVIDER_METADATA_MAX_BYTES,
    ModelResponse,
    ToolCall,
    Usage,
)


def _openai_response(**overrides) -> dict:
    response = {
        "id": "req_fixture",
        "model": "served/model-v2",
        "provider": "fixture-router",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "reasoning_details": [{"type": "encrypted", "data": "opaque"}],
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.0042},
    }
    response.update(overrides)
    return response


def test_model_response_old_minimum_remains_valid():
    response = ModelResponse(text="done")

    assert response.model == ""
    assert response.provider_request_id == ""
    assert response.billed_cost is None
    assert response.provider_metadata == {}
    assert response.resolved_cost_usd(0.25) == (0.25, "estimated")


def test_public_openai_codecs_round_trip_reasoning_and_provenance():
    response = normalize_openai_response(_openai_response())
    assistant = AgentLoop.assistant_blocks(response)
    replay = to_openai_messages("", [{"role": "assistant", "content": assistant}])

    assert response.model == "served/model-v2"
    assert response.provider_request_id == "req_fixture"
    assert response.billed_cost == pytest.approx(0.0042)
    assert response.billed_currency == "USD"
    assert response.billed_cost_usd == pytest.approx(0.0042)
    assert response.provider_metadata == {"provider": "fixture-router"}
    assert response.reasoning == {
        "reasoning_details": [{"type": "encrypted", "data": "opaque"}]
    }
    assert replay[0]["reasoning_details"] == [{"type": "encrypted", "data": "opaque"}]
    assert to_openai_tool({"name": "lookup"})["function"]["name"] == "lookup"


def test_reasoning_survives_a_real_tool_turn_for_provider_replay(tmp_path: Path):
    harness = tmp_path / "reasoning"
    construct.init_harness(harness, name="reasoning-replay", task="Read a file.")
    construct.add_tool(harness, builtin="file_read")
    (harness / "notes.txt").write_text("fixture", encoding="utf-8")
    reasoning = {"reasoning_details": [{"type": "encrypted", "data": "opaque"}]}
    provider = FakeModelProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="file_read", input={"path": "notes.txt"})],
                stop_reason="tool_use",
                content_blocks=[
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "file_read",
                        "input": {"path": "notes.txt"},
                    }
                ],
                reasoning=reasoning,
            ),
            ModelResponse(text="done", content_blocks=[{"type": "text", "text": "done"}]),
        ]
    )

    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )
    replay = to_openai_messages("", provider.calls[1]["messages"])
    assistant = next(message for message in replay if message["role"] == "assistant")

    assert result.status == "success"
    assert assistant["reasoning_details"] == reasoning["reasoning_details"]


def test_non_usd_billed_cost_is_retained_but_not_guessed_as_usd():
    payload = _openai_response()
    payload["usage"] = {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "cost": 0.40,
        "currency": "EUR",
    }
    response = normalize_openai_response(payload)

    assert response.billed_cost == pytest.approx(0.40)
    assert response.billed_currency == "EUR"
    assert response.billed_cost_usd is None
    assert response.resolved_cost_usd(0.03) == (0.03, "estimated")


def test_optional_provider_payloads_are_json_safe_and_bounded():
    with pytest.raises(ValidationError, match="JSON-safe"):
        ModelResponse(provider_metadata={"bad": {"not", "json"}})
    with pytest.raises(ValidationError, match="byte limit"):
        ModelResponse(provider_metadata={"blob": "x" * PROVIDER_METADATA_MAX_BYTES})
    with pytest.raises(ValidationError, match="billed_currency is required"):
        ModelResponse(billed_cost=0.1)
    with pytest.raises(ValidationError, match="billed_cost is required"):
        ModelResponse(billed_cost_usd=0.1)
    with pytest.raises(ValidationError, match="finite"):
        ModelResponse(billed_cost=float("inf"), billed_currency="USD")


def test_billed_cost_and_redacted_metadata_reach_public_receipts(tmp_path: Path):
    harness = tmp_path / "h"
    construct.init_harness(harness, name="provider-receipt", task="Return a short answer.")
    construct.set_value(harness, "logging.redact", ["secret-[a-z]+"])
    response = ModelResponse(
        text="done",
        content_blocks=[{"type": "text", "text": "done"}],
        usage=Usage(input_tokens=10, output_tokens=2),
        model="served-model",
        provider_request_id="request-17",
        billed_cost=0.25,
        billed_currency="USD",
        provider_metadata={"routing": "fast", "credential": "secret-value"},
    )

    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider([response]),
        literal_input=True,
        ingest=False,
    )

    assert result.cost_usd == pytest.approx(0.25)
    assert result.provider_calls == [
        {
            "turn": 1,
            "phase": "act",
            "provider": "claude",
            "requested_model": "claude-haiku-4-5",
            "effective_model": "served-model",
            "provider_request_id": "request-17",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            "cost_usd": 0.25,
            "cost_source": "billed",
            "billed_cost": 0.25,
            "billed_currency": "USD",
        }
    ]
    trace_text = Path(result.trace_path).read_text(encoding="utf-8")
    assert "secret-value" not in trace_text
    assert "[REDACTED]" in trace_text
    events = [json.loads(line) for line in trace_text.splitlines()]
    model_response = next(event for event in events if event["type"] == "model_response")
    assert model_response["payload"]["effective_model"] == "served-model"
    assert model_response["payload"]["provider_request_id"] == "request-17"
    assert model_response["payload"]["cost_source"] == "billed"
    assert model_response["payload"]["provider_metadata"]["credential"] == "[REDACTED]"
