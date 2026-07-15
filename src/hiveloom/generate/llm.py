"""The strong-model client used by the generator and evolver.

This is a deliberately thin text-in / text-out surface, separate from the
runtime :class:`~hiveloom.models.provider.ModelProvider` (which runs the small
executor model inside a harness). The generator/evolver need a strong model and
must not send sampling params that newer strong models reject.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# The build spec names ``claude-sonnet-4-6`` as the generator default. That model
# is still active; ``claude-sonnet-5`` is its current drop-in replacement. Kept
# as a single constant so it is trivial to bump.
DEFAULT_STRONG_MODEL = "claude-sonnet-4-6"


class StrongModel(ABC):
    """A strong text model: given a system + user prompt, return text."""

    @abstractmethod
    def generate(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        """Return the model's text response."""


class ClaudeStrongModel(StrongModel):
    """A strong Claude model via the ``anthropic`` SDK (no sampling params)."""

    def __init__(self, model_id: str = DEFAULT_STRONG_MODEL, api_key: str | None = None):
        import anthropic  # imported lazily so tests never need the SDK/key

        self._model_id = model_id
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def generate(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        response = self._client.messages.create(
            model=self._model_id,
            max_tokens=max_tokens,
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

    def generate(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        from hiveloom.models.provider import ModelConfig

        response = self._provider.complete(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[],
            config=ModelConfig(id=self._model_id, max_tokens=max_tokens),
        )
        return response.text


class FakeStrongModel(StrongModel):
    """Returns scripted responses in order, one per ``generate`` call (tests)."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts: list[dict[str, str]] = []

    def generate(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        self.prompts.append({"system": system, "user": user})
        if not self._responses:
            raise RuntimeError("FakeStrongModel ran out of scripted responses")
        return self._responses.pop(0)
