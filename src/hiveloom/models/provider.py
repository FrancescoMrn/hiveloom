"""The ModelProvider ABC and hiveloom's normalized model I/O types.

Messages are represented as a provider-neutral list of dicts:

    {"role": "user" | "assistant", "content": str | list[block]}

where a block is one of::

    {"type": "text", "text": "..."}
    {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": bool}

This mirrors the Anthropic content-block shape but uses only plain dicts, so no
provider SDK type escapes this package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

Message = dict[str, Any]


class Usage(BaseModel):
    """Token usage for a single model call."""

    input_tokens: int = 0
    output_tokens: int = 0


class ToolCall(BaseModel):
    """A normalized tool-use request from the model."""

    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """A normalized model response (no Anthropic types)."""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = Field(default_factory=Usage)
    content_blocks: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Assistant content blocks to append to history verbatim.",
    )


class ModelConfig(BaseModel):
    """The subset of model settings a provider needs for a call."""

    id: str
    max_tokens: int = 4096
    temperature: float = 0.0


# Fallback per-1M-token pricing (input, output) in USD when a model id is not
# in the registry: assume Haiku-class pricing rather than zero, so budget
# guardrails stay conservative. Real pricing lives in the model registry
# (builtin Claude models plus anything from extensions or models.yaml).
_FALLBACK_PRICE: tuple[float, float] = (1.00, 5.00)


def estimate_tokens(text: str) -> int:
    """Cheap, offline token estimate (~4 chars/token). Used for budgeting."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _estimate_messages_tokens(system: str, messages: list[Message]) -> int:
    total = estimate_tokens(system)
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                for value in block.values():
                    if isinstance(value, str):
                        total += estimate_tokens(value)
                    elif isinstance(value, dict):
                        total += estimate_tokens(str(value))
    return total


class ModelProvider(ABC):
    """Abstract model provider. Implementations normalize to hiveloom types."""

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        """Run one model turn and return a normalized response."""

    def count_tokens(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """Estimate the input token count for a prospective call.

        The default is a fast offline heuristic; providers may override with a
        real token-counting API.
        """
        return _estimate_messages_tokens(system, messages)

    def estimated_cost(self, usage: Usage, model_id: str) -> float:
        """Return the USD cost of ``usage`` for ``model_id`` (registry-priced)."""
        from hiveloom import ext

        in_price, out_price = ext.model_pricing(model_id) or _FALLBACK_PRICE
        return (usage.input_tokens / 1_000_000) * in_price + (
            usage.output_tokens / 1_000_000
        ) * out_price
