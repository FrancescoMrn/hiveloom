"""A generic OpenAI-compatible chat-completions provider.

One class unlocks every server speaking the OpenAI chat API: Ollama, vLLM,
LM Studio, Groq, OpenRouter, and friends — declared per-provider in
``~/.hiveloom/models.yaml`` (see :mod:`hiveloom.ext`). Local models are a
natural fit for hiveloom's small-cheap-executor thesis.

Uses only the standard library (urllib), mirroring the builtin ``http_get``
tool: no SDK dependency, and hiveloom's normalized message format converts
cleanly in both directions. Transport errors surface as exceptions; the agent
loop converts them into an ``error`` run status.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from hiveloom.models.provider import (
    ContextOverflowError,
    Message,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    ToolCall,
    Usage,
    estimate_tokens,
)

_STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}
_MAX_RETRIES = 3
_BASE_DELAY = 1.0

# Substrings in an HTTP 400 body that identify a context-window overflow
# ("context_length_exceeded" is OpenAI's error code; the phrases cover servers
# that only return a message, e.g. vLLM's "maximum context length is ...").
_OVERFLOW_MARKERS = ("context_length_exceeded", "maximum context length", "context window")


def _is_overflow(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


class OpenAICompatProvider(ModelProvider):
    """Runs the harness model against an OpenAI-compatible ``/chat/completions``."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: int = 120,
        sleep=time.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._sleep = sleep

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": config.id,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "messages": _to_openai_messages(system, messages),
        }
        if tools:
            payload["tools"] = [_to_openai_tool(t) for t in tools]
        data = self._post("/chat/completions", payload)
        return _normalize(
            data,
            estimated_input_tokens=self.count_tokens(system=system, messages=messages, tools=tools),
        )

    # ------------------------------------------------------------------ #
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urlrequest.Request(
            self._base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with urlrequest.urlopen(request, timeout=self._timeout) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))
            except urlerror.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code == 400 and _is_overflow(body):
                    raise ContextOverflowError(f"provider returned HTTP 400: {body}") from exc
                last_exc = RuntimeError(f"provider returned HTTP {exc.code}: {body}")
                retryable = exc.code == 429 or 500 <= exc.code < 600
            except (urlerror.URLError, TimeoutError, ConnectionError) as exc:
                last_exc = RuntimeError(f"provider unreachable at {self._base_url}: {exc}")
                retryable = True
            if not retryable or attempt == _MAX_RETRIES:
                break
            self._sleep(_BASE_DELAY * (2**attempt))
        raise RuntimeError(f"model call failed on attempt {attempt + 1}: {last_exc}") from last_exc


def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic-style ``{name, description, input_schema}`` → OpenAI function."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Convert hiveloom's content-block history into OpenAI chat messages."""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                texts.append(block.get("text", ""))
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
            elif kind == "tool_result":
                text = str(block.get("content", ""))
                if block.get("is_error"):
                    text = f"ERROR: {text}"
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": text,
                    }
                )
        if role == "assistant":
            # An assistant turn with no text is legal as "", never as null:
            # strict servers (e.g. Ollama) reject content:null with HTTP 400.
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or ""}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:
            # Tool results become individual role:"tool" messages in OpenAI land.
            out.extend(tool_results)
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _normalize(data: dict[str, Any], *, estimated_input_tokens: int = 0) -> ModelResponse:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"provider returned no choices: {json.dumps(data)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    # Reasoning models (DeepSeek-R1 family etc.) may return an empty "content"
    # with the chain-of-thought in "reasoning"/"reasoning_content" instead;
    # fall back so a reasoning-only turn isn't silently normalized to "".
    text = (
        message.get("content")
        or message.get("reasoning")
        or message.get("reasoning_content")
        or ""
    )

    tool_calls: list[ToolCall] = []
    content_blocks: list[dict[str, Any]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                arguments = json.loads(raw_arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        # A lenient backend can emit arguments that parse to a valid-but-non-object
        # JSON value (`"null"`, `"[1]"`, `"42"`); ToolCall.input requires a dict.
        if not isinstance(arguments, dict):
            arguments = {}
        call = ToolCall(id=raw_call.get("id", ""), name=function.get("name", ""), input=arguments)
        tool_calls.append(call)
        content_blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
        )

    raw_usage = data.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}

    def _usage(key: str) -> int | None:
        # Some backends (vLLM/LM Studio) emit an explicit null token count;
        # treat null/missing/non-numeric identically as "absent", so int() of
        # None never raises and the estimate fallback kicks in.
        value = raw_usage.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    input_tokens = _usage("prompt_tokens")
    if input_tokens is None:
        input_tokens = _usage("input_tokens")
    if input_tokens is None:
        input_tokens = estimated_input_tokens
    output_tokens = _usage("completion_tokens")
    if output_tokens is None:
        output_tokens = _usage("output_tokens")
    if output_tokens is None:
        output_tokens = estimate_tokens(text or json.dumps(message.get("tool_calls") or []))
    # OpenAI-style usage counts cached tokens *inside* prompt_tokens; hiveloom's
    # Usage keeps input_tokens uncached-only, so split the cached share out.
    details = raw_usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cache_read = int(cached) if isinstance(cached, (int, float)) else 0
    cache_read = min(cache_read, input_tokens)
    usage = Usage(
        input_tokens=input_tokens - cache_read,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
    )
    finish = choice.get("finish_reason") or "stop"
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=_STOP_REASONS.get(finish, finish),
        usage=usage,
        content_blocks=content_blocks,
    )
