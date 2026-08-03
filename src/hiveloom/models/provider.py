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
    """Token usage for a single model call.

    ``input_tokens`` counts only the *uncached* input, mirroring the Anthropic
    convention; prompt-cache traffic is broken out into ``cache_read_tokens``
    (prefix served from cache) and ``cache_write_tokens`` (prefix written to
    cache). Providers whose usage reports cached tokens inside the prompt
    total must subtract them during normalization.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ContextOverflowError(RuntimeError):
    """The provider rejected a request because the prompt exceeded the model's
    context window. Raised instead of a generic error so the agent loop can
    force a compaction and retry the turn rather than failing the run."""


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
    # Which provider serves `id`. Carried for pricing, not for dispatch: an
    # open-catalog provider accepts model ids that are not individually
    # registered, and the *provider* is then the only thing that says what an
    # unregistered id costs (a local Ollama model is free; an unknown hosted
    # one is not). Defaulted so existing callers keep working.
    provider: str = ""


# Fallback per-1M-token pricing (input, output) in USD when a model id is not
# in the registry: assume Haiku-class pricing rather than zero, so budget
# guardrails stay conservative. Real pricing lives in the model registry
# (builtin Claude models plus anything from extensions or models.yaml).
_FALLBACK_PRICE: tuple[float, float] = (1.00, 5.00)

# Prompt-cache pricing as multiples of the input price. These are Anthropic's
# list multipliers (5-minute ephemeral cache); other labs bill cache reads at
# 0.25-0.5x and writes at 1.0x, close enough that per-model cache pricing is
# not worth carrying in the registry yet.
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


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

    def estimated_cost(self, usage: Usage, model_id: str, provider: str = "") -> float:
        """Return the USD cost of ``usage`` for ``model_id`` (registry-priced).

        ``provider`` only matters for ids the registry does not know: it selects
        that provider's declared default price instead of ``_FALLBACK_PRICE``,
        which is what keeps a budget guardrail from charging Haiku rates for a
        free local model.
        """
        from hiveloom import ext

        in_price, out_price = ext.model_pricing(model_id, provider=provider) or _FALLBACK_PRICE
        weighted_input = (
            usage.input_tokens
            + usage.cache_read_tokens * _CACHE_READ_MULTIPLIER
            + usage.cache_write_tokens * _CACHE_WRITE_MULTIPLIER
        )
        return (weighted_input / 1_000_000) * in_price + (
            usage.output_tokens / 1_000_000
        ) * out_price
