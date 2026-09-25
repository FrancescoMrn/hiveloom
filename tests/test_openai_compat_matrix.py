"""Offline endpoint-matrix tests for the OpenAI-compat provider.

Covers response-shape variance across third-party servers that speak the
OpenAI chat-completions API (OpenRouter, Groq, Together, vLLM, Ollama,
mlx_lm.server) — in particular the two confirmed bugs from GitHub issue #5
(reasoning-only replies re-serializing as ``content: null``) and dict-shaped
tool-call arguments from lenient backends. No network, no API keys.
"""

from __future__ import annotations

import io
from urllib import error as urlerror

import pytest

from hiveloom.loop.agent_loop import AgentLoop
from hiveloom.models.openai_compat import (
    OpenAICompatProvider,
    _normalize,
    _to_openai_messages,
)
from hiveloom.models.provider import ContextOverflowError, ModelConfig, ModelResponse

# --------------------------------------------------------------------------- #
# Per-server response-shape fixtures
# --------------------------------------------------------------------------- #
openrouter_reasoning = {
    "choices": [
        {
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning": "Let me think step by step... the answer is 42.",
            },
        }
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 15},
}

vllm_dict_arguments = {
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": {"city": "Rome"}},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 30, "completion_tokens": 8},
}

vllm_list_arguments = {
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_list",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ["Rome"]},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 30, "completion_tokens": 8},
}

groq_standard = {
    "choices": [
        {"finish_reason": "stop", "message": {"role": "assistant", "content": "The answer is 4."}}
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}

together_standard = {
    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "42."}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}

ollama_standard = {
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_o1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 18, "completion_tokens": 6},
}

mlx_lm_standard = {
    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Done."}}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 2},
}

_FIXTURES = {
    "openrouter_reasoning": openrouter_reasoning,
    "vllm_dict_arguments": vllm_dict_arguments,
    "vllm_list_arguments": vllm_list_arguments,
    "groq_standard": groq_standard,
    "together_standard": together_standard,
    "ollama_standard": ollama_standard,
    "mlx_lm_standard": mlx_lm_standard,
}


@pytest.mark.parametrize("name", list(_FIXTURES))
def test_normalize_produces_sane_response_across_servers(name):
    result = _normalize(_FIXTURES[name])
    assert isinstance(result, ModelResponse)
    assert isinstance(result.text, str)
    assert isinstance(result.tool_calls, list)
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


# --------------------------------------------------------------------------- #
# Issue #5: reasoning-only replies must never re-serialize as content:null
# --------------------------------------------------------------------------- #
def test_reasoning_only_response_is_not_silently_empty():
    result = _normalize(openrouter_reasoning)
    assert result.text == "Let me think step by step... the answer is 42."


def test_reasoning_only_response_round_trips_without_null_content():
    """Structural regression for issue #5.

    Normalize a reasoning-only reply, build the assistant history entry the
    way AgentLoop does, then re-run it through _to_openai_messages: the
    resulting assistant entry's content must be a str, never None, even
    though there are no tool_calls in this turn.
    """
    response = _normalize(openrouter_reasoning)
    assistant_content = AgentLoop.assistant_blocks(response)
    messages = [{"role": "assistant", "content": assistant_content}]

    out = _to_openai_messages("", messages)

    assert len(out) == 1
    assert isinstance(out[0]["content"], str)
    assert "tool_calls" not in out[0]


def test_to_openai_messages_never_emits_null_content_for_empty_assistant_turn():
    """Direct lock-in of the null-content fix, independent of the reasoning fallback."""
    messages = [{"role": "assistant", "content": [{"type": "text", "text": ""}]}]
    out = _to_openai_messages("", messages)
    assert out[0]["content"] == ""


# --------------------------------------------------------------------------- #
# Dict-shaped tool-call arguments (lenient Ollama/vLLM tool-calling paths)
# --------------------------------------------------------------------------- #
def test_openai_compat_tool_call_arguments_as_dict_are_tolerated():
    result = _normalize(vllm_dict_arguments)
    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].input == {"city": "Rome"}


def test_openai_compat_tool_call_arguments_malformed_string_falls_back_to_empty_dict():
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "broken", "arguments": "{not json"},
                        }
                    ],
                },
            }
        ],
    }
    result = _normalize(response)
    assert result.tool_calls[0].input == {}


@pytest.mark.parametrize("arguments", ["null", "42", '"hi"', "[1, 2]"])
def test_openai_compat_tool_call_arguments_valid_json_non_object_falls_back(arguments):
    """A lenient backend can emit arguments that parse to a valid-but-non-object
    JSON value; ToolCall.input requires a dict, so these must coerce to {} rather
    than raise a pydantic ValidationError."""
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": "f", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
    }
    result = _normalize(response)
    assert result.tool_calls[0].input == {}


def test_openai_compat_null_usage_counts_do_not_crash():
    """Some backends emit explicit null token counts; int(None) must not raise —
    the estimate fallback should kick in instead."""
    response = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": None, "completion_tokens": None},
    }
    result = _normalize(response, estimated_input_tokens=123)
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens > 0


# --------------------------------------------------------------------------- #
# Usage fallback: input_tokens/output_tokens when prompt_tokens/completion_tokens absent
# --------------------------------------------------------------------------- #
def test_openai_compat_usage_falls_back_to_input_output_tokens():
    response = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"input_tokens": 33, "output_tokens": 7},
    }
    result = _normalize(response, estimated_input_tokens=999)
    assert result.usage.input_tokens == 33
    assert result.usage.output_tokens == 7


# --------------------------------------------------------------------------- #
# content_filter stop reason (telemetry-only)
# --------------------------------------------------------------------------- #
def test_content_filter_stop_reason_maps_to_end_turn():
    response = {
        "choices": [
            {
                "finish_reason": "content_filter",
                "message": {"role": "assistant", "content": "redacted"},
            }
        ],
    }
    result = _normalize(response)
    assert result.stop_reason == "end_turn"


# --------------------------------------------------------------------------- #
# Prompt-cache usage: cached tokens are split out of prompt_tokens
# --------------------------------------------------------------------------- #
def test_openai_compat_cached_tokens_split_out_of_prompt_tokens():
    response = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    result = _normalize(response)
    assert result.usage.input_tokens == 20
    assert result.usage.cache_read_tokens == 80


def test_openai_compat_cached_tokens_never_exceed_prompt_tokens():
    """A lenient backend reporting cached > prompt must not yield negative input."""
    response = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    result = _normalize(response)
    assert result.usage.input_tokens == 0
    assert result.usage.cache_read_tokens == 50


# --------------------------------------------------------------------------- #
# Context-window overflow: HTTP 400 classified, not retried
# --------------------------------------------------------------------------- #
def _http_error(code: int, body: str) -> urlerror.HTTPError:
    return urlerror.HTTPError("http://x", code, "Bad Request", None, io.BytesIO(body.encode()))


def test_openai_compat_overflow_400_raises_context_overflow(monkeypatch):
    provider = OpenAICompatProvider("http://localhost:9")
    calls = {"n": 0}

    def fake_urlopen(request, timeout=0):
        calls["n"] += 1
        raise _http_error(
            400, '{"error": {"code": "context_length_exceeded", "message": "too long"}}'
        )

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", fake_urlopen)
    with pytest.raises(ContextOverflowError):
        provider.complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            config=ModelConfig(id="m"),
        )
    assert calls["n"] == 1  # overflow is terminal for the request, never retried


def test_openai_compat_other_400_stays_a_runtime_error(monkeypatch):
    provider = OpenAICompatProvider("http://localhost:9")

    def fake_urlopen(request, timeout=0):
        raise _http_error(400, '{"error": {"message": "unknown field"}}')

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as excinfo:
        provider.complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            config=ModelConfig(id="m"),
        )
    assert not isinstance(excinfo.value, ContextOverflowError)


# --------------------------------------------------------------------------- #
# Oversized reasoning degrades; it must never cost the answer
# --------------------------------------------------------------------------- #
def test_huge_reasoning_is_dropped_rather_than_failing_the_response():
    """A reasoning model can out-write the bound on provider-owned payloads.

    Reasoning is replay metadata, not the answer. Rejecting the whole response
    over its size threw away a turn whose text and tool calls were fine — the
    model did the work and hiveloom discarded it over bookkeeping.
    """
    from hiveloom.models.provider import PROVIDER_REASONING_MAX_BYTES

    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "the answer is 42",
                    "reasoning": "x" * (PROVIDER_REASONING_MAX_BYTES + 1),
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    response = _normalize(payload, estimated_input_tokens=10)

    assert response.text == "the answer is 42"
    assert response.reasoning is None
    assert response.provider_metadata["reasoning_dropped_bytes"] > PROVIDER_REASONING_MAX_BYTES
    # The opaque replay payload is gone, but the thinking itself stays in the
    # transcript: the model is stateless, so dropping it outright would be the
    # harness losing the agent's state, not merely losing a replay convenience.
    kept = [b for b in response.content_blocks if b["type"] == "text"]
    assert any(b["text"].startswith("x" * 100) for b in kept)
    assert any("truncated by the harness" in b["text"] for b in kept)


def test_reasoning_within_the_bound_is_still_carried_for_replay():
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "42", "reasoning": "short thought"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    response = _normalize(payload, estimated_input_tokens=10)

    assert response.reasoning == {"reasoning": "short thought"}
    assert "reasoning_dropped_bytes" not in response.provider_metadata


def test_a_dropped_reasoning_payload_still_keeps_tool_calls():
    from hiveloom.models.provider import PROVIDER_REASONING_MAX_BYTES

    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "y" * (PROVIDER_REASONING_MAX_BYTES + 1),
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    response = _normalize(payload, estimated_input_tokens=10)

    assert [c.name for c in response.tool_calls] == ["get_weather"]
    assert response.reasoning is None


def test_an_absurd_provider_metadata_value_is_clamped_not_fatal():
    """Provenance is worth recording, never worth losing a turn over."""
    payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": "42"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "provider": "x" * 100_000,
    }

    response = _normalize(payload, estimated_input_tokens=10)

    assert response.text == "42"
    assert len(response.provider_metadata["provider"]) == 1024


def test_blank_content_falls_through_to_reasoning():
    """A server returning content " " with a full reasoning field is not empty-handed.

    Truthiness says a single space is content, so the whole reasoning-only turn
    was discarded and the run recorded a one-character answer. Observed from a
    vLLM-backed OpenRouter upstream returning 17k reasoning tokens beside " ".
    """
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": " ",
                    "reasoning": "the rule is a diagonal flip; the answer is [[1,2],[3,4]]",
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 17279},
    }

    response = _normalize(payload, estimated_input_tokens=10)

    assert response.text.startswith("the rule is a diagonal flip")


def test_real_content_still_wins_over_reasoning():
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "42", "reasoning": "thinking out loud"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    assert _normalize(payload, estimated_input_tokens=10).text == "42"


def test_a_response_cut_off_mid_body_is_retried_not_fatal():
    """http.client.IncompleteRead inherits from HTTPException, not ConnectionError.

    It therefore escaped the retry handler and killed the run. A reasoning model
    streams hundreds of KB over minutes, so a dropped connection in that window
    is routine rather than exotic.
    """
    from http.client import IncompleteRead

    provider = OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None)
    calls = {"n": 0}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IncompleteRead(b"12345")
            import json as _json

            return _json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "42"}}]}
            ).encode()

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(
        "hiveloom.models.openai_compat.urlrequest.urlopen",
        lambda request, timeout=0: _Response(),
    )
    try:
        response = provider.complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            config=ModelConfig(id="m"),
        )
    finally:
        monkeypatch.undo()

    assert response.text == "42"
    assert calls["n"] == 2  # first attempt raised, second succeeded


def test_an_error_inside_a_200_body_is_retried():
    """Aggregators answer HTTP 200 and put the upstream's failure in the body.

    Observed: `{"error": {"message": "Upstream error from Wafer: The model is
    temporarily at capacity. Please retry shortly.", "code": 502}}` returned with
    status 200. Status-only classification never saw it, so a transient blip
    became an unretryable "provider returned no choices" and killed the run at
    turn 0 — more likely with a pinned upstream, which has no sibling to absorb
    a capacity blip.
    """
    import json as _json

    provider = OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None)
    calls = {"n": 0}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            calls["n"] += 1
            if calls["n"] == 1:
                return _json.dumps(
                    {
                        "error": {
                            "message": "Upstream error: temporarily at capacity.",
                            "code": 502,
                        }
                    }
                ).encode()
            return _json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "42"}}]}
            ).encode()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "hiveloom.models.openai_compat.urlrequest.urlopen",
        lambda request, timeout=0: _Response(),
    )
    try:
        response = provider.complete(
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=[],
            config=ModelConfig(id="m"),
        )
    finally:
        monkeypatch.undo()

    assert response.text == "42"
    assert calls["n"] == 2


def test_a_non_transient_error_body_is_not_retried_forever():
    """A 400-class error in the body is the caller's problem, not a blip."""
    import json as _json

    provider = OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None)
    calls = {"n": 0}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            calls["n"] += 1
            return _json.dumps(
                {"error": {"message": "invalid model id", "code": 400}}
            ).encode()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "hiveloom.models.openai_compat.urlrequest.urlopen",
        lambda request, timeout=0: _Response(),
    )
    try:
        with pytest.raises(RuntimeError, match="invalid model id"):
            provider.complete(
                system="s",
                messages=[{"role": "user", "content": "x"}],
                tools=[],
                config=ModelConfig(id="m"),
            )
    finally:
        monkeypatch.undo()

    assert calls["n"] == 1  # not retried


def test_oversized_reasoning_only_response_obeys_the_salvage_limit():
    from hiveloom.models.openai_compat import (
        _SALVAGED_REASONING_MAX_CHARS,
        normalize_openai_response,
    )
    from hiveloom.models.provider import PROVIDER_REASONING_MAX_BYTES

    response = normalize_openai_response(
        {"choices": [{"finish_reason": "length", "message": {
            "content": " ", "reasoning": "x" * (PROVIDER_REASONING_MAX_BYTES + 100),
        }}]}, estimated_input_tokens=1,
    )
    assert len(response.text) < _SALVAGED_REASONING_MAX_CHARS + 100
    assert "reasoning truncated" in response.text
    assert response.reasoning is None
    assert response.content_blocks[0]["text"] == response.text


def test_non_scalar_provider_metadata_does_not_reject_an_answer():
    from hiveloom.models.openai_compat import normalize_openai_response

    response = normalize_openai_response(
        {"provider": {"unexpected": "x" * 100_000},
         "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        estimated_input_tokens=1,
    )
    assert response.text == "ok"
    assert "provider" not in response.provider_metadata


def test_embedded_context_overflow_is_recoverable(monkeypatch):
    import json

    from hiveloom.models.openai_compat import OpenAICompatProvider
    from hiveloom.models.provider import ContextOverflowError

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def read(self):
            return json.dumps({"error": {"code": 400,
                                        "message": "maximum context length exceeded"}}).encode()

    monkeypatch.setattr(
        "hiveloom.models.openai_compat.urlrequest.urlopen", lambda *a, **k: Response()
    )
    with pytest.raises(ContextOverflowError):
        OpenAICompatProvider("http://localhost:9").complete(
            system="s", messages=[], tools=[], config=ModelConfig(id="m"),
        )
