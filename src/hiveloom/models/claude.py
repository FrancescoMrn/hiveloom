"""The Anthropic Claude model provider.

Anthropic SDK types are confined to this module; everything returned is a
hiveloom :class:`ModelResponse`. Rate limits and overloads are retried with
exponential backoff (max 3 retries).
"""

from __future__ import annotations

import time
from typing import Any

from hiveloom.models.provider import (
    Message,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    ToolCall,
    Usage,
)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0


class ClaudeProvider(ModelProvider):
    """Runs the harness model via the official ``anthropic`` SDK."""

    def __init__(self, api_key: str | None = None, *, sleep=time.sleep):
        import anthropic  # imported lazily so tests never need the SDK/key

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._sleep = sleep

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        raw = self._call_with_backoff(
            model=config.id,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            system=system,
            messages=messages,
            tools=tools,
        )
        return self._normalize(raw)

    def count_tokens(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        try:
            result = self._client.messages.count_tokens(
                model="claude-haiku-4-5",
                system=system or None,
                messages=messages,
                tools=tools or [],
            )
            return int(result.input_tokens)
        except Exception:  # noqa: BLE001 - fall back to the heuristic on any error
            return super().count_tokens(system=system, messages=messages, tools=tools)

    # ------------------------------------------------------------------ #
    def _call_with_backoff(self, **kwargs):
        anthropic = self._anthropic
        retriable = (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
            *(
                (anthropic.OverloadedError,)
                if hasattr(anthropic, "OverloadedError")
                else ()
            ),
        )
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                params = {k: v for k, v in kwargs.items() if v not in (None, "")}
                return self._client.messages.create(**params)
            except retriable as exc:  # type: ignore[misc]
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    break
                self._sleep(_BASE_DELAY * (2**attempt))
        raise RuntimeError(f"model call failed after {_MAX_RETRIES} retries: {last_exc}")

    def _normalize(self, raw: Any) -> ModelResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: list[dict[str, Any]] = []
        for block in raw.content:
            if block.type == "text":
                text_parts.append(block.text)
                content_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input),
                    }
                )
        usage = Usage(
            input_tokens=getattr(raw.usage, "input_tokens", 0),
            output_tokens=getattr(raw.usage, "output_tokens", 0),
        )
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=raw.stop_reason or "end_turn",
            usage=usage,
            content_blocks=content_blocks,
        )
