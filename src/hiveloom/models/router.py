"""The executing model for a run — and changing it mid-flight.

A harness declares one model, but a run does not have to spend all of itself
in it. Three things can move the executor:

* a **playbook** that declares its own ``model:`` — profile cheaply, decide
  expensively, with the choice written down in the YAML and therefore inside
  the harness version hash;
* an **operator**, through :class:`~hiveloom.loop.control.RunControl`, consumed
  at the loop's next turn boundary like a stop or a steer;
* nothing else. Automatic escalation on runtime signals is deliberately not
  here — see ``docs/roadmap.md``.

The router owns two things the loop should not have to: which
:class:`ModelConfig` is current, and which provider instance serves it.
Provider instances are built lazily and cached, because a swap may cross
providers (Claude to a local Ollama) and constructing every declared provider
up front would make an unused one's absent credentials a startup failure.

It also records the **model path** — every model the run actually used, in
order. A run that changed models is not a clean sample of "this harness at
this version", and the Hive needs to be able to say so rather than blend it
into a fitness bucket with runs that did not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hiveloom.models.provider import ModelConfig, ModelProvider

#: Content-block types that survive a model swap. Everything else is
#: model-internal: `thinking` and `redacted_thinking` carry signatures that
#: only the model that produced them can validate, and replaying one to a
#: different model is at best ignored and at worst a 400 on the next tool-use
#: turn. Anything carrying a `signature` is dropped for the same reason,
#: whatever it calls itself.
PORTABLE_BLOCK_TYPES: frozenset[str] = frozenset(
    {"text", "tool_use", "tool_result", "image", "document"}
)


@dataclass
class ModelSwitch:
    """One entry in a run's model path."""

    turn: int
    model: str
    provider: str
    reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


def portable_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Strip model-internal blocks from a conversation. Returns (messages, dropped).

    Applied at a swap boundary only. Within one model the blocks are wanted —
    ``models/claude.py`` replays them deliberately, because adaptive-thinking
    models reject a tool-use turn whose thinking was dropped.
    """
    dropped = 0
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        kept: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                kept.append(block)
                continue
            if block.get("type") not in PORTABLE_BLOCK_TYPES or "signature" in block:
                dropped += 1
                continue
            kept.append(block)
        if not kept:
            # An assistant turn stripped to nothing cannot be sent; dropping the
            # whole turn keeps the alternation valid.
            dropped += 1
            continue
        out.append({**message, "content": kept})
    return out, dropped


@dataclass
class ModelRouter:
    """Which model a run is executing in, and the provider instance serving it."""

    base: Path
    config: ModelConfig
    _providers: dict[str, ModelProvider] = field(default_factory=dict)
    _path: list[ModelSwitch] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        base: str | Path,
        config: ModelConfig,
        provider: ModelProvider,
        *,
        providers: dict[str, ModelProvider] | None = None,
    ) -> ModelRouter:
        """Build a router seeded with the spec's model and its provider instance.

        ``providers`` pre-registers instances for other provider names. It is
        how an embedding caller (and the test suite) keeps control of what a
        cross-provider swap actually talks to, instead of the router
        constructing a real client from ambient credentials.
        """
        router = cls(base=Path(base), config=config)
        router._providers[config.provider] = provider
        for name, instance in (providers or {}).items():
            router._providers.setdefault(name, instance)
        router._path.append(
            ModelSwitch(turn=0, model=config.id, provider=config.provider, reason="spec")
        )
        return router

    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> ModelProvider:
        return self._provider_for(self.config.provider)

    def _provider_for(self, name: str) -> ModelProvider:
        instance = self._providers.get(name)
        if instance is None:
            from hiveloom import ext

            instance = ext.build_provider(name, self.base)
            self._providers[name] = instance
        return instance

    def register(self, name: str, provider: ModelProvider) -> None:
        """Pre-register a provider instance (embedders and tests)."""
        self._providers[name] = provider

    # ------------------------------------------------------------------ #
    @property
    def path(self) -> list[ModelSwitch]:
        return list(self._path)

    def path_key(self) -> str:
        """The run's model path as a stable key, e.g. ``claude:haiku>claude:opus``.

        Runs that took different paths are not the same experiment, so this is
        what the Hive buckets on alongside the harness version hash.
        """
        keys: list[str] = []
        for entry in self._path:
            if not keys or keys[-1] != entry.key:
                keys.append(entry.key)
        return ">".join(keys)

    def swapped(self) -> bool:
        return len({entry.key for entry in self._path}) > 1

    # ------------------------------------------------------------------ #
    def switch(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        turn: int = 0,
        reason: str = "",
    ) -> ModelSwitch | None:
        """Move the executor. Returns the recorded switch, or None if it is a no-op.

        Resolving the provider eagerly here — rather than at the next model
        call — means an unknown provider or a missing credential surfaces at
        the switch, where the caller can still be told about it, instead of as
        a mid-run failure two turns later.
        """
        target_provider = provider or self.config.provider
        target_model = model or self.config.id
        if (
            target_provider == self.config.provider
            and target_model == self.config.id
            and max_tokens is None
            and temperature is None
        ):
            return None

        # Validates the target and caches the instance before anything changes.
        self._provider_for(target_provider)

        self.config = ModelConfig(
            id=target_model,
            provider=target_provider,
            max_tokens=self.config.max_tokens if max_tokens is None else max_tokens,
            temperature=self.config.temperature if temperature is None else temperature,
        )
        entry = ModelSwitch(
            turn=turn, model=target_model, provider=target_provider, reason=reason
        )
        self._path.append(entry)
        return entry

    def close(self) -> None:
        """Close any provider instance that owns resources."""
        for instance in self._providers.values():
            closer = getattr(instance, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001, S110 - teardown must not fail a run
                    pass
