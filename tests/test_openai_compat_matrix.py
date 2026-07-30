"""Offline endpoint-matrix tests for the OpenAI-compat provider.

Covers response-shape variance across third-party servers that speak the
OpenAI chat-completions API (OpenRouter, Groq, Together, vLLM, Ollama,
mlx_lm.server) — in particular the two confirmed bugs from GitHub issue #5
(reasoning-only replies re-serializing as ``content: null``) and dict-shaped
tool-call arguments from lenient backends. No network, no API keys.
"""

from __future__ import annotations

import pytest

from hiveloom.loop.agent_loop import AgentLoop
from hiveloom.models.openai_compat import _normalize, _to_openai_messages
from hiveloom.models.provider import ModelResponse

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
