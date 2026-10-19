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
from http.client import HTTPException
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from hiveloom.models.provider import (
    PROVIDER_REASONING_MAX_BYTES,
    ContextOverflowError,
    Message,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    ToolCall,
    Usage,
    estimate_tokens,
    validate_model_params,
)


def _first_nonblank(*values: Any) -> str:
    """The first value carrying actual characters, else ""."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


# How much of an over-large reasoning payload is kept as transcript text. The
# model cannot remember what the transcript does not carry, so this is state,
# not logging; the context manager bounds it further from here.
_SALVAGED_REASONING_MAX_CHARS = 200_000

# Per-value clamp for provider_metadata's scalar fields. Three fields well
# under this can never reach PROVIDER_METADATA_MAX_BYTES between them.
_METADATA_VALUE_MAX_CHARS = 1024

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


def _embedded_error(body: Any) -> tuple[int, str] | None:
    """``(code, message)`` when a 200 response carries an error instead of a result.

    Returns None for a normal response, so the caller keeps its fast path.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) else 0
    return code, str(error.get("message") or error)[:300]


def _is_overflow(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


class _UndecodableBody(RuntimeError):
    """A 200 whose JSON body broke off before its end, or whose bytes are not UTF-8."""


def _decode_body(raw: bytes) -> dict[str, Any]:
    """The JSON object a 200 must carry.

    JSON that started well and broke off before its end, or bytes that are
    not UTF-8, are a transport failure and raise :class:`_UndecodableBody`,
    which the retry loop treats like a dropped connection. A body that is not
    JSON at all (an HTML login page or a plain ``Unauthorized`` behind a wrong
    ``base_url``) or JSON that is not an object (``null``, a list) is an
    endpoint answering something else, and it will again, so it fails at once
    with a readable message. Every message carries the start of the body: that
    is what the run's reason has to go on.
    """
    try:
        body = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        if exc.pos == 0:
            raise RuntimeError(f"provider returned a body that is not JSON: {raw[:200]!r}") from exc
        raise _UndecodableBody(f"{exc}: {raw[:200]!r}") from exc
    except UnicodeDecodeError as exc:
        raise _UndecodableBody(f"{exc}: {raw[:200]!r}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(
            f"provider returned a JSON {type(body).__name__} instead of an object: {raw[:200]!r}"
        )
    return body


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
            # Spec-declared provider fields first, so the keys the harness owns
            # always win even if `model.params` validation is somehow bypassed.
            **validate_model_params(config.params),
            "model": config.id,
            "max_tokens": config.max_tokens,
            **({} if config.temperature is None else {"temperature": config.temperature}),
            "messages": to_openai_messages(system, messages),
        }
        if tools:
            payload["tools"] = [to_openai_tool(t) for t in tools]
        data = self._post("/chat/completions", payload)
        return normalize_openai_response(
            data,
            estimated_input_tokens=self.count_tokens(system=system, messages=messages, tools=tools),
            max_tokens=config.max_tokens,
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
                    body = _decode_body(resp.read())
                embedded = _embedded_error(body)
                if embedded is None:
                    return body
                # An aggregator can answer HTTP 200 and put the upstream's
                # failure in the body ("temporarily at capacity", code 502).
                # Status-only classification never sees it, so a transient blip
                # surfaced as an unretryable "no choices" and killed the run —
                # more likely with a pinned upstream, which has no sibling to
                # absorb it.
                code, message = embedded
                if _is_overflow(message):
                    raise ContextOverflowError(message)
                last_exc = RuntimeError(f"provider returned an error body: {message}")
                retryable = code == 429 or 500 <= code < 600
            except urlerror.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code == 400 and _is_overflow(body):
                    raise ContextOverflowError(f"provider returned HTTP 400: {body}") from exc
                last_exc = RuntimeError(f"provider returned HTTP {exc.code}: {body}")
                retryable = exc.code == 429 or 500 <= exc.code < 600
            except (urlerror.URLError, TimeoutError, ConnectionError, HTTPException) as exc:
                # HTTPException covers http.client.IncompleteRead: a response
                # cut off mid-body. It inherits from neither URLError nor
                # ConnectionError, so it used to escape this handler and kill
                # the run. Long generations make it routine — a reasoning model
                # streams hundreds of KB over several minutes, and any dropped
                # connection in that window lands here.
                last_exc = RuntimeError(f"provider unreachable at {self._base_url}: {exc}")
                retryable = True
            except _UndecodableBody as exc:
                # The request produced no result, any more than a dropped
                # connection did, so it is retried the same way.
                last_exc = RuntimeError(f"provider returned an undecodable body: {exc}")
                retryable = True
            if not retryable or attempt == _MAX_RETRIES:
                break
            self._sleep(_BASE_DELAY * (2**attempt))
        raise RuntimeError(f"model call failed on attempt {attempt + 1}: {last_exc}") from last_exc


def to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic-style ``{name, description, input_schema}`` → OpenAI function."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
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
        provider_fields: dict[str, Any] = {}
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
            elif kind == "provider_data" and block.get("provider") == "openai_compat":
                data = block.get("data")
                if isinstance(data, dict):
                    provider_fields.update(data)
            elif kind == "provider_reasoning":
                data = block.get("data")
                if isinstance(data, dict):
                    provider_fields.update(data)
        if role == "assistant":
            # An assistant turn with no text is legal as "", never as null:
            # strict servers (e.g. Ollama) reject content:null with HTTP 400.
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or ""}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            entry.update(provider_fields)
            out.append(entry)
        else:
            # Tool results become individual role:"tool" messages in OpenAI land.
            out.extend(tool_results)
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def normalize_openai_response(
    data: dict[str, Any], *, estimated_input_tokens: int = 0, max_tokens: int | None = None
) -> ModelResponse:
    """Normalize a chat-completions body into a :class:`ModelResponse`.

    ``max_tokens`` is the output ceiling the request carried. vLLM reports
    ``finish_reason: tool_calls`` for a call it managed to parse even when
    generation stopped at that cap, and executing such a call acts on
    arguments the model never finished writing; a reply carrying tool calls
    whose reported completion count reached the ceiling is therefore
    normalized as ``max_tokens``, whatever finish reason the server put on
    it. A text-only reply keeps the server's verdict: one cut off at the cap
    already arrives as ``length``.
    """
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"provider returned no choices: {json.dumps(data)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    # Reasoning models (DeepSeek-R1 family etc.) may return an empty "content"
    # with the chain-of-thought in "reasoning"/"reasoning_content" instead;
    # fall back so a reasoning-only turn isn't silently normalized to "".
    # Blank-but-not-empty counts as empty: servers in this family have been
    # seen returning content " " alongside a full reasoning field, and a
    # truthiness test on that single space discards the entire turn.
    text = _first_nonblank(
        message.get("content"),
        message.get("reasoning"),
        message.get("reasoning_content"),
    )

    tool_calls: list[ToolCall] = []
    content_blocks: list[dict[str, Any]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    # A reasoning model can emit more reasoning than the bound on provider-owned
    # payloads allows, and rejecting the response over that would throw away a
    # turn whose text and tool calls are perfectly good. But the model is
    # stateless: what it knows next turn is only what the transcript carries, so
    # silently dropping its thinking is the harness losing the agent's state and
    # then wondering why it starts over. Drop only the *opaque* replay payload —
    # which cannot be truncated without risking an invalid replay — and keep the
    # thinking itself as text, bounded, for the transcript to hold.
    replay_fields: dict[str, Any] | None = {
        key: message[key]
        for key in ("reasoning", "reasoning_content", "reasoning_details")
        if message.get(key) is not None
    }
    reasoning_dropped_bytes = 0
    salvaged_reasoning = ""
    if replay_fields:
        encoded = len(
            json.dumps(replay_fields, separators=(",", ":"), default=str).encode("utf-8")
        )
        if encoded > PROVIDER_REASONING_MAX_BYTES:
            reasoning_dropped_bytes = encoded
            salvaged_reasoning = _first_nonblank(
                message.get("reasoning"), message.get("reasoning_content")
            )
            replay_fields = None

    if reasoning_dropped_bytes:
        # If reasoning was used as the visible-text fallback, bound that copy
        # too. Otherwise a blank-content response bypasses the salvage limit.
        if text == salvaged_reasoning:
            text = text[:_SALVAGED_REASONING_MAX_CHARS]
            if len(salvaged_reasoning) > _SALVAGED_REASONING_MAX_CHARS:
                text += "\n[... reasoning truncated by the harness ...]"
            content_blocks = [{"type": "text", "text": text}] if text else []
        elif salvaged_reasoning:
            kept = salvaged_reasoning[:_SALVAGED_REASONING_MAX_CHARS]
            if len(salvaged_reasoning) > _SALVAGED_REASONING_MAX_CHARS:
                kept += "\n[... reasoning truncated by the harness ...]"
            content_blocks.append({"type": "text", "text": kept})
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        raw_arguments = function.get("arguments")
        arguments_error = ""
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif raw_arguments is not None and not isinstance(raw_arguments, str):
            arguments, arguments_error = {}, "not_object"
        else:
            try:
                arguments = json.loads(raw_arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments, arguments_error = {}, "invalid_json"
        # A lenient backend can emit arguments that parse to a valid-but-non-object
        # JSON value (`"null"`, `"[1]"`, `"42"`); ToolCall.input requires a dict.
        if not isinstance(arguments, dict):
            arguments, arguments_error = {}, "not_object"
        call = ToolCall(id=raw_call.get("id", ""), name=function.get("name", ""),
                        input=arguments, arguments_error=arguments_error)
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
    reported_output_tokens = _usage("completion_tokens")
    if reported_output_tokens is None:
        reported_output_tokens = _usage("output_tokens")
    output_tokens = (
        reported_output_tokens
        if reported_output_tokens is not None
        else estimate_tokens(text or json.dumps(message.get("tool_calls") or []))
    )
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
    if (
        tool_calls
        and max_tokens is not None
        and reported_output_tokens is not None
        and reported_output_tokens >= max_tokens
    ):
        # Only a count the server reported: the estimate above stands in for
        # cost, never for a truncation verdict.
        finish = "length"
    raw_cost = raw_usage.get("cost")
    billed_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    billed_currency = ""
    billed_cost_usd = None
    if billed_cost is not None:
        raw_currency = raw_usage.get("currency") or data.get("currency") or "USD"
        billed_currency = str(raw_currency).upper()
        if billed_currency == "USD":
            billed_cost_usd = billed_cost
    # Provenance, like reasoning: worth recording, never worth losing a turn
    # over. These are scalar routing fields, so clamping each one keeps the
    # payload under the bound no matter what a misbehaving provider returns —
    # the validator on ModelResponse would otherwise reject the whole response.
    provider_metadata = {
        key: (value[:_METADATA_VALUE_MAX_CHARS] if isinstance(value, str) else value)
        for key in ("provider", "service_tier", "system_fingerprint")
        if isinstance((value := data.get(key)), str)
    }
    if reasoning_dropped_bytes:
        provider_metadata["reasoning_dropped_bytes"] = reasoning_dropped_bytes
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=_STOP_REASONS.get(finish, finish),
        usage=usage,
        content_blocks=content_blocks,
        model=str(data.get("model") or ""),
        provider_request_id=str(data.get("id") or ""),
        billed_cost=billed_cost,
        billed_currency=billed_currency,
        billed_cost_usd=billed_cost_usd,
        reasoning=replay_fields or None,
        provider_metadata=provider_metadata,
    )


# Compatibility aliases for extensions written against the pre-1.1 private
# helpers. New extension code should import the public names above.
_to_openai_tool = to_openai_tool
_to_openai_messages = to_openai_messages
_normalize = normalize_openai_response

__all__ = [
    "OpenAICompatProvider",
    "normalize_openai_response",
    "to_openai_messages",
    "to_openai_tool",
]
