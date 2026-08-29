"""Offline tests for the Claude provider: prompt caching and overflow classification.

The ``anthropic`` SDK is faked in ``sys.modules`` (the provider imports it
lazily), so these run with no SDK installed and no API key.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from hiveloom.models.provider import ContextOverflowError, ModelConfig


def _response(
    text: str = "ok",
    *,
    cache_read: int = 0,
    cache_write: int = 0,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id="msg_fixture",
        model="claude-served-fixture",
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


def _make_provider(monkeypatch: pytest.MonkeyPatch, create):
    """Install a fake ``anthropic`` module and return a ClaudeProvider over it."""
    module = types.ModuleType("anthropic")

    class BadRequestError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class InternalServerError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class Anthropic:
        def __init__(self, api_key: str | None = None):
            self.messages = types.SimpleNamespace(create=create)

    module.BadRequestError = BadRequestError
    module.RateLimitError = RateLimitError
    module.InternalServerError = InternalServerError
    module.APIConnectionError = APIConnectionError
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)

    from hiveloom.models.claude import ClaudeProvider

    return ClaudeProvider(api_key="test"), module


def test_cache_breakpoints_on_system_tools_and_last_message(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**kwargs):
        captured.update(kwargs)
        return _response()

    provider, _ = _make_provider(monkeypatch, create)
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}]
    provider.complete(
        system="sys", messages=messages, tools=tools, config=ModelConfig(id="m")
    )

    assert captured["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in captured["tools"][0]
    assert captured["tools"][1]["cache_control"] == {"type": "ephemeral"}
    sent_block = captured["messages"][-1]["content"][0]
    assert sent_block["cache_control"] == {"type": "ephemeral"}
    # The shared history and tool dicts must not accumulate breakpoints.
    assert messages == [{"role": "user", "content": "hello"}]
    assert "cache_control" not in tools[1]


def test_cache_breakpoint_copies_block_content_without_mutating_history(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**kwargs):
        captured.update(kwargs)
        return _response()

    provider, _ = _make_provider(monkeypatch, create)
    block = {"type": "tool_result", "tool_use_id": "t1", "content": "r"}
    tool_use = {"type": "tool_use", "id": "t1", "name": "a", "input": {}}
    messages = [
        {"role": "assistant", "content": [tool_use]},
        {"role": "user", "content": [block]},
    ]
    provider.complete(system="", messages=messages, tools=[], config=ModelConfig(id="m"))

    assert captured["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in block
    # Earlier messages pass through as the very same objects (no deep copy).
    assert captured["messages"][0] is messages[0]


def test_usage_reports_cache_tokens(monkeypatch):
    provider, _ = _make_provider(
        monkeypatch, lambda **kw: _response(cache_read=800, cache_write=200)
    )
    result = provider.complete(
        system="s", messages=[{"role": "user", "content": "x"}], tools=[],
        config=ModelConfig(id="m"),
    )
    assert result.usage.cache_read_tokens == 800
    assert result.usage.cache_write_tokens == 200
    assert result.usage.input_tokens == 10
    assert result.model == "claude-served-fixture"
    assert result.provider_request_id == "msg_fixture"


def test_overflow_bad_request_maps_to_context_overflow(monkeypatch):
    def create(**kwargs):
        raise sys.modules["anthropic"].BadRequestError(
            "prompt is too long: 250000 tokens > 200000 maximum"
        )

    provider, _ = _make_provider(monkeypatch, create)
    with pytest.raises(ContextOverflowError):
        provider.complete(
            system="s", messages=[{"role": "user", "content": "x"}], tools=[],
            config=ModelConfig(id="m"),
        )


def test_other_bad_request_propagates_unchanged(monkeypatch):
    def create(**kwargs):
        raise sys.modules["anthropic"].BadRequestError("invalid tool schema")

    provider, module = _make_provider(monkeypatch, create)
    with pytest.raises(module.BadRequestError):
        provider.complete(
            system="s", messages=[{"role": "user", "content": "x"}], tools=[],
            config=ModelConfig(id="m"),
        )


def test_temperature_omitted_for_models_that_reject_sampling_params(monkeypatch):
    captured: dict[str, Any] = {}

    def create(**kwargs):
        captured.update(kwargs)
        return _response()

    provider, _ = _make_provider(monkeypatch, create)
    provider.complete(
        system="s", messages=[{"role": "user", "content": "x"}], tools=[],
        config=ModelConfig(id="claude-opus-5"),
    )
    assert "temperature" not in captured

    provider.complete(
        system="s", messages=[{"role": "user", "content": "x"}], tools=[],
        config=ModelConfig(id="claude-haiku-4-5", temperature=0.0),
    )
    assert captured["temperature"] == 0.0


def test_thinking_blocks_are_preserved_on_the_assistant_turn(monkeypatch):
    thinking = types.SimpleNamespace(
        type="thinking",
        model_dump=lambda: {"type": "thinking", "thinking": "…", "signature": "sig"},
    )
    raw = types.SimpleNamespace(
        content=[thinking, types.SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
    )

    provider, _ = _make_provider(monkeypatch, lambda **kw: raw)
    result = provider.complete(
        system="s", messages=[{"role": "user", "content": "x"}], tools=[],
        config=ModelConfig(id="claude-opus-5"),
    )
    assert result.content_blocks[0] == {
        "type": "thinking", "thinking": "…", "signature": "sig",
    }
    assert result.text == "ok"
