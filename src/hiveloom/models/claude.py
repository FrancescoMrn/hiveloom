"""The Anthropic Claude model provider.

Anthropic SDK types are confined to this module; everything returned is a
hiveloom :class:`ModelResponse`. Rate limits and overloads are retried with
exponential backoff (max 3 retries).

Prompt caching is always on: the system prompt, the tool list, and the tail of
the conversation are marked as cache breakpoints, so the stable prefix of an
agent loop (system + tools + earlier turns) is written once and read cheaply
on every subsequent turn. Cache read/write tokens are reported on ``Usage``
and priced by ``ModelProvider.estimated_cost``.
"""

from __future__ import annotations

import time
from typing import Any

from hiveloom.models.provider import (
    ContextOverflowError,
    Message,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    ToolCall,
    Usage,
)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0

_CACHE_CONTROL = {"type": "ephemeral"}

# Models whose API surface rejects sampling parameters: Opus 4.7 onward,
# Sonnet 5, and the Fable/Mythos tier return 400 for a non-default
# `temperature` (the spec default of 0.0 is non-default to the API).
_NO_SAMPLING_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos",
)

# Substrings that identify a BadRequestError as a context-window overflow.
_OVERFLOW_MARKERS = ("prompt is too long", "context window", "maximum context")


def _is_overflow(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _with_cache_breakpoint(messages: list[Message]) -> list[Message]:
    """Copy ``messages`` with a cache breakpoint on the final content block.

    Only the touched path is copied: the history dicts are shared with the
    context manager, and mutating them would accumulate stale breakpoints
    across turns (the API allows at most four).
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, str) and content:
        last["content"] = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
    elif isinstance(content, list) and content:
        blocks = list(content)
        blocks[-1] = {**blocks[-1], "cache_control": _CACHE_CONTROL}
        last["content"] = blocks
    else:
        return messages
    return [*messages[:-1], last]


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
        system_param: Any = system
        if system:
            system_param = [
                {"type": "text", "text": system, "cache_control": _CACHE_CONTROL}
            ]
        tools_param = tools
        if tools:
            tools_param = [*tools[:-1], {**tools[-1], "cache_control": _CACHE_CONTROL}]
        raw = self._call_with_backoff(
            model=config.id,
            max_tokens=config.max_tokens,
            temperature=(
                None if config.id.startswith(_NO_SAMPLING_PREFIXES) else config.temperature
            ),
            system=system_param,
            messages=_with_cache_breakpoint(messages),
            tools=tools_param,
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
            except anthropic.BadRequestError as exc:
                if _is_overflow(str(exc)):
                    raise ContextOverflowError(str(exc)) from exc
                raise
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
            elif hasattr(block, "model_dump"):
                # thinking / redacted_thinking / other model-internal blocks.
                # Adaptive-thinking models (Opus 5, Sonnet 5, ...) require the
                # assistant turn to be replayed with these blocks unchanged;
                # dropping them can 400 on the next call of a tool-use loop.
                content_blocks.append(block.model_dump())
        usage = Usage(
            input_tokens=getattr(raw.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw.usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw.usage, "cache_creation_input_tokens", 0) or 0,
        )
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=raw.stop_reason or "end_turn",
            usage=usage,
            content_blocks=content_blocks,
        )
