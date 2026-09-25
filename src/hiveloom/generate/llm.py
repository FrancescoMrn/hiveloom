"""The strong-model client used by the generator and evolver.

This is a deliberately thin text-in / text-out surface, separate from the
runtime :class:`~hiveloom.models.provider.ModelProvider` (which runs the small
executor model inside a harness). The generator/evolver need a strong model and
must not send sampling params that newer strong models reject.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from hiveloom.errors import SpecError

# The build spec names ``claude-sonnet-4-6`` as the generator default. That model
# is still active; ``claude-sonnet-5`` is its current drop-in replacement. Kept
# as a single constant so it is trivial to bump.
DEFAULT_STRONG_MODEL = "claude-sonnet-4-6"

# Known model capabilities permit a larger proposal budget. Unknown models
# retain the compatible historical default; a high guessed limit can cause 400s.
DEFAULT_STRONG_MAX_TOKENS = 32768
FALLBACK_STRONG_MAX_TOKENS = 4096


def strong_max_tokens(model_id: str, requested: int | None = None) -> int:
    """Use a declared ceiling when available, with a conservative fallback."""
    from hiveloom import ext

    info = ext.model_info(model_id)
    ceiling = info.max_output_tokens if info else None
    if requested is not None:
        if requested <= 0 or (ceiling is not None and requested > ceiling):
            raise ValueError("strong-model max_tokens must be positive and within its limit")
        return requested
    return min(ceiling, DEFAULT_STRONG_MAX_TOKENS) if ceiling else FALLBACK_STRONG_MAX_TOKENS


class StrongModel(ABC):
    """A strong text model: given a system + user prompt, return text."""

    @abstractmethod
    def generate(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        """Return the model's text response.

        An unset budget uses the registered model ceiling, capped by
        DEFAULT_STRONG_MAX_TOKENS, or the conservative fallback for unknown
        models. See :func:`strong_max_tokens`.
        """


class ClaudeStrongModel(StrongModel):
    """A strong Claude model via the ``anthropic`` SDK (no sampling params)."""

    def __init__(self, model_id: str = DEFAULT_STRONG_MODEL, api_key: str | None = None):
        import anthropic  # imported lazily so tests never need the SDK/key

        self._model_id = model_id
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def generate(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        response = self._client.messages.create(
            model=self._model_id,
            max_tokens=strong_max_tokens(self._model_id, max_tokens),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class ProviderStrongModel(StrongModel):
    """Adapts any registered :class:`ModelProvider` into a strong model.

    Selected with the ``provider/model-id`` syntax (e.g. ``ollama/qwen3:32b``)
    in ``hiveloom generate --model`` / ``evolve --model``, so generation and
    evolution share the runtime's provider registry.
    """

    def __init__(self, provider, model_id: str):
        self._provider = provider
        self._model_id = model_id

    def generate(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        from hiveloom.models.provider import ModelConfig

        response = self._provider.complete(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[],
            config=ModelConfig(
                id=self._model_id,
                max_tokens=strong_max_tokens(self._model_id, max_tokens),
            ),
        )
        return response.text


class FakeStrongModel(StrongModel):
    """Returns scripted responses in order, one per ``generate`` call (tests)."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts: list[dict[str, str]] = []

    def generate(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        del max_tokens
        self.prompts.append({"system": system, "user": user})
        if not self._responses:
            raise RuntimeError("FakeStrongModel ran out of scripted responses")
        return self._responses.pop(0)


def build_strong_model(model_id: str | None, base: Path | None = None) -> StrongModel:
    """Build the strong generation/evolution model.

    ``provider/model-id`` (e.g. ``ollama/qwen3:32b``) routes through the
    provider registry when the prefix names a registered provider; anything
    else is a Claude model id. Shared by the CLI (``generate``/``evolve``) and
    the runner's auto-propose trigger, so both resolve a strong model the same
    way. Imports ``hiveloom.ext``/``anthropic``/``dotenv`` lazily so importing
    this module (or starting the CLI) never pays for them unless a strong
    model is actually requested.
    """
    import os

    from hiveloom import ext

    if model_id and "/" in model_id:
        prefix, rest = model_id.split("/", 1)
        if prefix in ext.provider_names():
            return ProviderStrongModel(ext.build_provider(prefix, base), rest)

    if base is not None and (base / ".env").exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(base / ".env")
        except ImportError:  # pragma: no cover
            pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SpecError("ANTHROPIC_API_KEY is not set (needed for generate/evolve).")
    return ClaudeStrongModel(model_id or DEFAULT_STRONG_MODEL)
