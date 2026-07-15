"""A deterministic fake model provider for tests.

Scripted with a queue of :class:`ModelResponse` objects (or helpers below); each
``complete`` call pops the next one. This lets the full agent loop run — tool
calls, verification, retries, trace emission — with no API key.
"""

from __future__ import annotations

from typing import Any

from hiveloom.models.provider import (
    Message,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    ToolCall,
    Usage,
)


def text_response(text: str, *, output_tokens: int = 20, input_tokens: int = 100) -> ModelResponse:
    """A response with only assistant text (signals completion)."""
    return ModelResponse(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        content_blocks=[{"type": "text", "text": text}],
    )


def tool_response(
    name: str,
    tool_input: dict[str, Any],
    *,
    call_id: str = "call_1",
    text: str = "",
    output_tokens: int = 20,
    input_tokens: int = 100,
) -> ModelResponse:
    """A response requesting a single tool call."""
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
    return ModelResponse(
        text=text,
        tool_calls=[ToolCall(id=call_id, name=name, input=tool_input)],
        stop_reason="tool_use",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        content_blocks=blocks,
    )


class FakeModelProvider(ModelProvider):
    """Returns scripted responses in order, one per ``complete`` call."""

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self._responses:
            # Default to a benign completion if the script runs dry.
            return text_response("done")
        return self._responses.pop(0)
