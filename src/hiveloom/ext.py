"""The extension registry: how the catalog is assembled and extended.

The catalog dicts in :mod:`hiveloom.catalog` describe what exists; this module
owns how entries come to exist and how they are *built* at runtime. Builtins
register their factories here at import time, and extensions register new
entries through the same :class:`ExtensionAPI` — so an installed pack's tools,
guardrails, validators, and providers appear in ``hiveloom catalog``, validate
in specs, and surface in the generator meta-prompt exactly like builtins.

An extension is any module exposing::

    def hiveloom_extension(hive: ExtensionAPI) -> None: ...

Extensions are discovered from three places (in order):

1. installed packages exposing a ``hiveloom.extensions`` entry point (a pack),
2. user-level files in ``~/.hiveloom/extensions/*.py``,
3. a harness's ``extensions:`` spec list (paths or module names), loaded just
   before that spec is validated.

Environment sources (1 and 2) follow pi's error discipline: a broken extension
is recorded (see ``hiveloom extensions``) and skipped, never crashes the CLI.
A harness-referenced extension that fails is a :class:`SpecError` — the harness
cannot run without it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from hiveloom import catalog, paths
from hiveloom.errors import CatalogError, HiveloomError, SpecError


class ExtensionError(HiveloomError):
    """Raised when an extension registration is invalid."""


@dataclass
class BuildContext:
    """What a factory may need to construct a runtime object."""

    base: Path | None = None
    tool_registry: Any = None
    # The harness's configured trace directory, relative to `base` (or None
    # if unconfigured/external) — only `build_registry` currently populates
    # this (it has the spec loaded); every other factory ignores it via this
    # default, so adding it here does not ripple into unrelated builders.
    trace_dir: Path | None = None


class ModelInfo(BaseModel):
    """Metadata for one model id: pricing (per 1M tokens, USD) and limits."""

    id: str
    provider: str = ""
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    context_window: int | None = None


class ProviderInfo(BaseModel):
    """Describes a registered provider for introspection (``hiveloom models``).

    ``open_catalog`` is the important one. A *closed* provider only accepts the
    model ids registered alongside it, so a typo fails spec validation. An
    *open* provider accepts any id, because enumerating its catalog is either
    impossible (aggregators like OpenRouter route to thousands of models) or
    meaningless (a local Ollama/vLLM server serves whatever the user pulled).
    Unknown ids on an open provider fall back to conservative pricing — see
    ``models/provider.py:_FALLBACK_PRICE`` — so a budget guardrail never
    silently costs *less* than it thinks.
    """

    name: str
    api: str = "openai_compat"
    base_url: str = ""
    api_key_env: str = ""
    open_catalog: bool = False
    source: str = ""
    label: str = ""


# A factory builds a runtime object from a ref's inline params and a context.
Factory = Callable[[dict[str, Any], BuildContext], Any]
ProviderFactory = Callable[[BuildContext], Any]


@dataclass
class _Registry:
    """All mutable registration state, kept in one place so reset() is total."""

    factories: dict[str, dict[str, Factory]] = field(
        default_factory=lambda: {kind: {} for kind in catalog.CATALOGS}
    )
    providers: dict[str, ProviderFactory] = field(default_factory=dict)
    provider_meta: dict[str, ProviderInfo] = field(default_factory=dict)
    # Per-provider (input, output) price for ids the registry does not know.
    # Only open-catalog providers can have unknown ids, and only local ones
    # currently set this — see `_OPENAI_COMPAT_PROVIDERS`.
    provider_default_price: dict[str, tuple[float, float]] = field(default_factory=dict)
    models: dict[str, ModelInfo] = field(default_factory=dict)
    ambient_hooks: list[tuple[str, str, Callable]] = field(default_factory=list)
    blueprints: dict[str, str] = field(default_factory=dict)
    pack_dists: dict[str, dict[str, str]] = field(default_factory=dict)
    registrations: list[dict[str, str]] = field(default_factory=list)  # {source, kind, name}
    errors: list[dict[str, str]] = field(default_factory=list)  # {source, error}
    loaded_sources: set[str] = field(default_factory=set)
    env_loaded: bool = False


_registry = _Registry()

# Names present in the catalog before any extension ran — the reset() baseline.
_BUILTIN_NAMES: dict[str, frozenset[str]] = {
    kind: frozenset(entries) for kind, entries in catalog.CATALOGS.items()
}


# --------------------------------------------------------------------------- #
# The registration API handed to extensions
# --------------------------------------------------------------------------- #
class ExtensionAPI:
    """What an extension receives: registration methods bound to its source."""

    def __init__(self, source: str):
        self.source = source

    # -- catalog kinds ---------------------------------------------------- #
    def register_tool(
        self,
        name: str,
        factory: Factory,
        *,
        description: str,
        tags: Sequence[str] = (),
        params: Sequence[Any] = (),
    ) -> None:
        """Register a tool. ``factory(params, ctx)`` must return a ``Tool``."""
        self._register("tools", name, factory, description, tags, params)

    def register_function_tool(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        tags: Sequence[str] = (),
    ) -> None:
        """Sugar: expose a plain function as a tool (schema from type hints)."""
        from hiveloom.tools.registry import FunctionTool

        meta = getattr(func, "__hiveloom_tool__", {})
        tool_name = name or func.__name__
        desc = description or meta.get("description") or (func.__doc__ or "").strip()
        tool_tags = list(tags or meta.get("tags", []))
        if not desc:
            raise ExtensionError(f"function tool '{tool_name}' needs a description or docstring")

        def factory(_params: dict[str, Any], _ctx: BuildContext) -> Any:
            return FunctionTool(func, name=tool_name, description=desc, tags=tool_tags)

        self._register("tools", tool_name, factory, desc, tool_tags, ())

    def register_guardrail(
        self,
        name: str,
        factory: Factory,
        *,
        description: str,
        tags: Sequence[str] = (),
        params: Sequence[Any] = (),
        singleton: bool = False,
    ) -> None:
        """Register a guardrail. ``factory(params, ctx)`` must return a ``Guardrail``.

        Set ``singleton`` when only one entry of this guardrail is meaningful in a
        spec, so ``hiveloom add guardrail`` replaces rather than appends.
        """
        self._register("guardrails", name, factory, description, tags, params, singleton=singleton)

    def register_validator(
        self,
        name: str,
        factory: Factory,
        *,
        description: str,
        tags: Sequence[str] = (),
        params: Sequence[Any] = (),
    ) -> None:
        """Register a verifier. ``factory(params, ctx)`` must return a ``Verifier``."""
        self._register("validators", name, factory, description, tags, params)

    def register_policy(
        self,
        name: str,
        factory: Factory,
        *,
        description: str,
        tags: Sequence[str] = (),
    ) -> None:
        """Register a loop policy. ``factory(params, ctx)`` must return a ``LoopPolicy``."""
        self._register("policies", name, factory, description, tags, ())

    def register_compaction(
        self,
        name: str,
        factory: Factory,
        *,
        description: str,
        tags: Sequence[str] = (),
    ) -> None:
        """Register a compaction method. ``factory(params, ctx)`` must return
        a ``CompactionMethod``."""
        self._register("compaction", name, factory, description, tags, ())

    def register_hook(
        self,
        name: str,
        factory: Factory,
        *,
        description: str,
        tags: Sequence[str] = (),
        params: Sequence[Any] = (),
    ) -> None:
        """Register a named event handler specs can attach via their ``hooks:``
        section. ``factory(params, ctx)`` must return a ``handler(event) -> result``."""
        self._register("hooks", name, factory, description, tags, params)

    def on(self, event: str) -> Callable[[Callable], Callable]:
        """Decorator: subscribe an ambient handler to a lifecycle event.

        Ambient handlers run for *every* harness executed in this process
        (e.g. an org-wide audit pack). Per-harness handlers belong in the
        spec's ``hooks:`` section instead.
        """
        from hiveloom.events import EVENTS

        if event not in EVENTS:
            raise ExtensionError(f"unknown event '{event}' (valid: {', '.join(EVENTS)})")

        def decorate(func: Callable) -> Callable:
            name = f"{self.source}:{getattr(func, '__name__', 'hook')}"
            _registry.ambient_hooks.append((event, name, func))
            _registry.registrations.append(
                {"source": self.source, "kind": "ambient_hooks", "name": f"{event}:{name}"}
            )
            return func

        return decorate

    # -- providers & models ------------------------------------------------ #
    def register_provider(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        models: Iterable[ModelInfo | dict[str, Any]] = (),
        api: str = "",
        base_url: str = "",
        api_key_env: str = "",
        open_catalog: bool = False,
        label: str = "",
        replace: bool = False,
    ) -> None:
        """Register a model provider. ``factory(ctx)`` must return a ``ModelProvider``.

        ``replace=True`` overrides an existing registration instead of failing.
        Only user configuration uses it: ``~/.hiveloom/models.yaml`` must be
        able to point a builtin lab name at a different ``base_url`` (a
        corporate gateway, a proxy, a pinned API version). Packs still collide
        loudly, so two extensions cannot silently fight over one name.
        """
        if name in _registry.providers and not replace:
            raise ExtensionError(f"provider '{name}' is already registered")
        _registry.providers[name] = factory
        _registry.provider_meta[name] = ProviderInfo(
            name=name,
            api=api or "openai_compat",
            base_url=base_url,
            api_key_env=api_key_env,
            open_catalog=open_catalog,
            source=self.source,
            label=label or name,
        )
        _registry.registrations.append({"source": self.source, "kind": "providers", "name": name})
        for model in models:
            self.register_model(model, provider=name)

    def register_blueprint(self, name: str, text: str) -> None:
        """Register a generator blueprint (see ``hiveloom generate --blueprint``)."""
        if name in _registry.blueprints:
            raise ExtensionError(f"blueprint '{name}' is already registered")
        _registry.blueprints[name] = text
        _registry.registrations.append(
            {"source": self.source, "kind": "blueprints", "name": name}
        )

    def register_model(self, model: ModelInfo | dict[str, Any], *, provider: str = "") -> None:
        """Register model metadata (pricing/limits) for cost estimation."""
        info = model if isinstance(model, ModelInfo) else ModelInfo.model_validate(model)
        if provider and not info.provider:
            info.provider = provider
        _registry.models[info.id] = info
        _registry.registrations.append(
            {"source": self.source, "kind": "models", "name": info.id}
        )

    # -- shared ------------------------------------------------------------ #
    def _register(
        self,
        kind: str,
        name: str,
        factory: Factory,
        description: str,
        tags: Sequence[str],
        params: Sequence[Any],
        singleton: bool = False,
    ) -> None:
        entries = catalog.CATALOGS[kind]
        if name in entries:
            owner = entries[name].source
            raise ExtensionError(
                f"{kind[:-1] if kind.endswith('s') else kind} '{name}' is already "
                f"registered (by {owner})"
            )
        entries[name] = catalog.CatalogEntry(
            name=name,
            description=description,
            tags=list(tags),
            params=[catalog.ParamSpec.model_validate(p) for p in params],
            source=self.source,
            singleton=singleton,
        )
        _registry.factories[kind][name] = factory
        _registry.registrations.append({"source": self.source, "kind": kind, "name": name})


# --------------------------------------------------------------------------- #
# Builtin factory registration (called by builtin modules at import time)
# --------------------------------------------------------------------------- #
def register_builtin_factory(kind: str, name: str, factory: Factory) -> None:
    """Attach a runtime factory to an existing builtin catalog entry."""
    if name not in catalog.CATALOGS[kind]:
        raise ExtensionError(f"no builtin {kind} entry named '{name}' to attach a factory to")
    _registry.factories[kind][name] = factory


# --------------------------------------------------------------------------- #
# Building runtime objects from refs
# --------------------------------------------------------------------------- #
def build(kind: str, name: str, params: dict[str, Any], ctx: BuildContext) -> Any:
    """Construct the runtime object for a catalog entry, or raise :class:`CatalogError`."""
    factory = _registry.factories.get(kind, {}).get(name)
    if factory is None:
        raise CatalogError(
            f"no {kind[:-1] if kind.endswith('s') else kind} named '{name}' is registered — "
            "is the extension pack that provides it installed? (see `hiveloom extensions`)"
        )
    return factory(params, ctx)


def provider_names() -> list[str]:
    """Names of all registered model providers (triggers environment loading)."""
    ensure_environment_loaded()
    return sorted(_registry.providers)


def build_provider(name: str, base: Path | None = None) -> Any:
    """Construct the model provider registered under ``name``."""
    ensure_environment_loaded()
    factory = _registry.providers.get(name)
    if factory is None:
        raise SpecError(
            f"unknown model provider '{name}' (available: {', '.join(provider_names())}). "
            "Register providers via an extension or ~/.hiveloom/models.yaml."
        )
    return factory(BuildContext(base=base))


def model_pricing(model_id: str, *, provider: str = "") -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens for ``model_id``, if known.

    An exact model registration wins. Failing that, ``provider``'s declared
    default price applies — that is how an unlisted model on a local Ollama or
    vLLM server is priced at zero instead of inheriting the conservative
    hosted fallback. ``None`` means "nothing knows", and the caller applies
    ``models/provider.py:_FALLBACK_PRICE``.
    """
    ensure_environment_loaded()
    info = _registry.models.get(model_id)
    if info is not None:
        return (info.input_cost_per_mtok, info.output_cost_per_mtok)
    if provider:
        return _registry.provider_default_price.get(provider)
    return None


def provider_info(name: str) -> ProviderInfo | None:
    """Metadata for one registered provider (endpoint, key variable, catalog policy)."""
    ensure_environment_loaded()
    return _registry.provider_meta.get(name)


def providers() -> list[ProviderInfo]:
    """Metadata for every registered provider, name-sorted."""
    ensure_environment_loaded()
    return [_registry.provider_meta[name] for name in sorted(_registry.provider_meta)]


def models_for_provider(name: str) -> list[ModelInfo]:
    """Registered models belonging to ``name``, id-sorted.

    An open-catalog provider accepts ids beyond these; the list is a starting
    point with known pricing, not a limit.
    """
    ensure_environment_loaded()
    return sorted(
        (m for m in _registry.models.values() if m.provider == name), key=lambda m: m.id
    )


def model_info(model_id: str) -> ModelInfo | None:
    ensure_environment_loaded()
    return _registry.models.get(model_id)


def ambient_hooks() -> list[tuple[str, str, Callable]]:
    """(event, name, handler) triples registered via ``ExtensionAPI.on``."""
    ensure_environment_loaded()
    return list(_registry.ambient_hooks)


def get_blueprint(name: str) -> str | None:
    """A blueprint's markdown: ``~/.hiveloom/blueprints/<name>.md`` wins over packs."""
    ensure_environment_loaded()
    file_path = paths.hiveloom_home() / "blueprints" / f"{name}.md"
    if file_path.exists():
        return file_path.read_text(encoding="utf-8")
    return _registry.blueprints.get(name)


def blueprint_names() -> list[str]:
    ensure_environment_loaded()
    names = set(_registry.blueprints)
    blueprint_dir = paths.hiveloom_home() / "blueprints"
    if blueprint_dir.is_dir():
        names.update(p.stem for p in blueprint_dir.glob("*.md"))
    return sorted(names)


def packs_for_spec(spec: Any) -> list[dict[str, str]]:
    """The extension packs a spec's catalog references require, for the lock file.

    Scans every ``builtin:`` ref (plus the loop policy and compaction method)
    for entries whose ``source`` is a pack, and reports the pack's distribution
    name/version when known.
    """
    ensure_environment_loaded()
    sources: set[str] = set()

    def _note(kind: str, name: str) -> None:
        entry = catalog.CATALOGS[kind].get(name)
        if entry is not None and entry.source not in ("builtin",):
            sources.add(entry.source)

    for ref in spec.tools:
        if hasattr(ref, "builtin"):
            _note("tools", ref.builtin)
    for ref in spec.guardrails:
        if hasattr(ref, "builtin"):
            _note("guardrails", ref.builtin)
    for ref in spec.verify.validators:
        if hasattr(ref, "builtin"):
            _note("validators", ref.builtin)
    for ref in spec.hooks:
        if hasattr(ref, "builtin"):
            _note("hooks", ref.builtin)
    _note("policies", spec.loop.policy)
    _note("compaction", spec.context.compaction.method)

    packs: list[dict[str, str]] = []
    for source in sorted(sources):
        dist = _registry.pack_dists.get(source)
        packs.append(
            {"source": source, **(dist or {})}
        )
    return packs


# --------------------------------------------------------------------------- #
# Discovery & loading
# --------------------------------------------------------------------------- #
def ensure_environment_loaded() -> None:
    """Load builtin providers, ``models.yaml``, packs, and user extensions once."""
    if _registry.env_loaded:
        return
    # Set the flag first: extension code may construct specs, which re-enter here.
    _registry.env_loaded = True
    _register_builtin_providers()
    _load_models_yaml()
    for ep in _iter_entry_points():
        source = f"pkg:{ep.name}"
        dist = getattr(ep, "dist", None)
        if dist is not None:
            _registry.pack_dists[source] = {
                "name": getattr(dist, "name", ep.name),
                "version": getattr(dist, "version", ""),
            }
        try:
            factory = ep.load()
            _run_extension(factory, source)
        except Exception as exc:  # noqa: BLE001 - a broken pack must not crash the CLI
            _registry.errors.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})
    ext_dir = paths.user_extensions_dir()
    if ext_dir.is_dir():
        from hiveloom import trust

        for py_file in sorted(ext_dir.glob("*.py")):
            source = f"user:{py_file.name}"
            try:
                trust.ensure_trusted(ext_dir)
                _load_extension_file(py_file, source)
            except Exception as exc:  # noqa: BLE001 - same discipline as packs
                _registry.errors.append(
                    {"source": source, "error": f"{type(exc).__name__}: {exc}"}
                )


def load_harness_extensions(sources: Sequence[str], base: Path) -> None:
    """Load a harness's declared extensions. Failures are :class:`SpecError`s."""
    ensure_environment_loaded()
    for ref in sources:
        try:
            if ref.endswith(".py"):
                file_path = (base / ref).resolve()
                if not file_path.exists():
                    raise ExtensionError(f"extension file not found: {ref}")
                _load_extension_file(file_path, source=f"harness:{ref}")
            else:
                module = importlib.import_module(ref)
                factory = getattr(module, "hiveloom_extension", None)
                if factory is None:
                    raise ExtensionError(
                        f"module '{ref}' has no hiveloom_extension(hive) function"
                    )
                _run_extension(factory, source=f"harness:{ref}")
        except SpecError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize to an actionable SpecError
            raise SpecError(f"failed to load harness extension '{ref}': {exc}") from exc


def _load_extension_file(file_path: Path, source: str) -> None:
    key = f"file:{file_path.resolve()}"
    if key in _registry.loaded_sources:
        return
    module_name = f"hiveloom_ext_{abs(hash(str(file_path.resolve())))}"
    module_spec = importlib.util.spec_from_file_location(module_name, file_path)
    if module_spec is None or module_spec.loader is None:
        raise ExtensionError(f"could not load extension module: {file_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    factory = getattr(module, "hiveloom_extension", None)
    if factory is None:
        raise ExtensionError(f"{file_path} has no hiveloom_extension(hive) function")
    _run_extension(factory, source, dedupe_key=key)


def _run_extension(factory: Callable[[ExtensionAPI], None], source: str,
                   *, dedupe_key: str | None = None) -> None:
    key = dedupe_key or f"source:{source}"
    if key in _registry.loaded_sources:
        return
    factory(ExtensionAPI(source))
    _registry.loaded_sources.add(key)


def _iter_entry_points():
    """Installed packs: entry points in the ``hiveloom.extensions`` group."""
    from importlib import metadata

    try:
        return list(metadata.entry_points(group="hiveloom.extensions"))
    except Exception:  # noqa: BLE001 - metadata quirks must not break startup
        return []


# --------------------------------------------------------------------------- #
# Builtin providers & models.yaml
# --------------------------------------------------------------------------- #
# Per-1M-token (input, output) pricing for the builtin Claude models. Sourced
# from the Claude API model catalog; the executor default is claude-haiku-4-5.
_CLAUDE_MODELS: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}


def _claude_factory(ctx: BuildContext) -> Any:
    """Build the Claude provider, loading a harness-local .env if present."""
    if ctx.base is not None:
        env_file = ctx.base / ".env"
        if env_file.exists():
            try:
                from dotenv import load_dotenv

                load_dotenv(env_file)
            except ImportError:  # pragma: no cover - dotenv is a declared dependency
                pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SpecError(
            "ANTHROPIC_API_KEY is not set. Add it to the harness .env or the environment."
        )
    from hiveloom.models.claude import ClaudeProvider

    return ClaudeProvider()


# Every other lab hiveloom ships with speaks the OpenAI chat-completions API,
# so one adapter (`OpenAICompatProvider`) covers all of them; a builtin entry
# is just the endpoint, the key variable, and a starter model list.
#
# All of these are `open_catalog`: model catalogs churn far faster than
# hiveloom releases, and the aggregators route to thousands of ids, so
# pinning a closed list would mean a new frontier model is unusable until the
# next release. `claude` stays closed — it has a native provider and a list
# maintained in-repo, so a typo there should still fail validation.
#
# The pricing below is list price per 1M tokens at the time of writing and is
# used only for cost *estimation* and budget guardrails. Verify against your
# provider's current pricing and override any entry in ~/.hiveloom/models.yaml.
# `default_price` prices ids that are not listed; local servers are free, so
# theirs is (0, 0) rather than the conservative hosted fallback.
@dataclass(frozen=True)
class _BuiltinProvider:
    base_url: str
    api_key_env: str
    label: str
    models: dict[str, tuple[float, float]] = field(default_factory=dict)
    default_price: tuple[float, float] | None = None


_OPENAI_COMPAT_PROVIDERS: dict[str, _BuiltinProvider] = {
    "openai": _BuiltinProvider(
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        label="OpenAI",
        models={
            "gpt-4o": (2.50, 10.00),
            "gpt-4o-mini": (0.15, 0.60),
            "gpt-4.1": (2.00, 8.00),
            "gpt-4.1-mini": (0.40, 1.60),
            "gpt-4.1-nano": (0.10, 0.40),
        },
    ),
    "gemini": _BuiltinProvider(
        # Google exposes an OpenAI-compatible surface at this path. Tool-call
        # support there is narrower than the native API; see docs/models.md.
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        label="Google Gemini",
        models={
            "gemini-2.0-flash": (0.10, 0.40),
            "gemini-2.5-flash": (0.30, 2.50),
            "gemini-2.5-pro": (1.25, 10.00),
        },
    ),
    "mistral": _BuiltinProvider(
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        label="Mistral",
        models={
            "mistral-small-latest": (0.20, 0.60),
            "mistral-large-latest": (2.00, 6.00),
        },
    ),
    "deepseek": _BuiltinProvider(
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        label="DeepSeek",
        models={"deepseek-chat": (0.27, 1.10), "deepseek-reasoner": (0.55, 2.19)},
    ),
    "xai": _BuiltinProvider(
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        label="xAI Grok",
        models={"grok-3": (3.00, 15.00), "grok-3-mini": (0.30, 0.50)},
    ),
    "moonshot": _BuiltinProvider(
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        label="Moonshot AI",
    ),
    "groq": _BuiltinProvider(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        label="Groq",
    ),
    "openrouter": _BuiltinProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        label="OpenRouter",
    ),
    "together": _BuiltinProvider(
        base_url="https://api.together.xyz/v1",
        api_key_env="TOGETHER_API_KEY",
        label="Together AI",
    ),
    "fireworks": _BuiltinProvider(
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        label="Fireworks AI",
    ),
    "ollama": _BuiltinProvider(
        base_url="http://localhost:11434/v1",
        api_key_env="",
        label="Ollama (local)",
        default_price=(0.0, 0.0),
    ),
    "vllm": _BuiltinProvider(
        base_url="http://localhost:8000/v1",
        api_key_env="",
        label="vLLM (local)",
        default_price=(0.0, 0.0),
    ),
}


def _register_builtin_providers() -> None:
    api = ExtensionAPI(source="builtin")
    api.register_provider(
        "claude",
        _claude_factory,
        api="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        label="Anthropic Claude",
        models=[
            ModelInfo(id=mid, provider="claude", input_cost_per_mtok=inp,
                      output_cost_per_mtok=out)
            for mid, (inp, out) in _CLAUDE_MODELS.items()
        ],
    )
    for name, entry in _OPENAI_COMPAT_PROVIDERS.items():
        if entry.default_price is not None:
            _registry.provider_default_price[name] = entry.default_price
        api.register_provider(
            name,
            _openai_compat_factory(entry.base_url, entry.api_key_env or None),
            base_url=entry.base_url,
            api_key_env=entry.api_key_env,
            label=entry.label,
            open_catalog=True,
            models=[
                ModelInfo(id=mid, provider=name, input_cost_per_mtok=inp,
                          output_cost_per_mtok=out)
                for mid, (inp, out) in entry.models.items()
            ],
        )


class _YamlModelEntry(BaseModel):
    id: str
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    context_window: int | None = None


class _YamlProviderEntry(BaseModel):
    api: str = "openai_compat"
    # Optional so an entry can *extend* a builtin provider (add models, fix a
    # price) without restating its endpoint. Supplying it overrides the builtin.
    base_url: str | None = None
    api_key_env: str | None = None
    open_catalog: bool | None = None
    models: list[_YamlModelEntry] = []


def _load_models_yaml() -> None:
    """Register providers/models declared in ``~/.hiveloom/models.yaml``.

    Format::

        providers:
          ollama:
            api: openai_compat          # the only custom api kind in v0
            base_url: http://localhost:11434/v1
            api_key_env: OLLAMA_API_KEY # optional
            models:
              - id: qwen3:8b
                input_cost_per_mtok: 0
                output_cost_per_mtok: 0
    """
    yaml_path = paths.models_yaml_path()
    if not yaml_path.exists():
        return
    import yaml as _yaml

    source = "models.yaml"
    try:
        data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        providers = data.get("providers") or {}
        api = ExtensionAPI(source=source)
        for name, raw_entry in providers.items():
            entry = _YamlProviderEntry.model_validate(raw_entry)
            if entry.api != "openai_compat":
                raise ExtensionError(
                    f"provider '{name}': unsupported api '{entry.api}' "
                    "(v0 supports: openai_compat)"
                )
            models = [_model_info_from_yaml(m, name, source) for m in entry.models]
            builtin = _registry.provider_meta.get(name)
            if entry.base_url is None:
                # Extend-only: the entry adds models or corrects pricing for a
                # provider that already exists. Without a builtin to extend
                # there is no endpoint to call, so that is a real error.
                if builtin is None:
                    raise ExtensionError(
                        f"provider '{name}': base_url is required "
                        "(no builtin provider of that name to extend)"
                    )
                for model in models:
                    api.register_model(model, provider=name)
                continue
            api.register_provider(
                name,
                _openai_compat_factory(entry.base_url, entry.api_key_env),
                base_url=entry.base_url,
                api_key_env=entry.api_key_env or "",
                label=builtin.label if builtin else name,
                # Declared entries are closed by default — the user listed the
                # models they meant, so a typo should fail. Overriding a builtin
                # keeps the builtin's policy unless the entry says otherwise.
                open_catalog=(
                    entry.open_catalog
                    if entry.open_catalog is not None
                    else bool(builtin and builtin.open_catalog)
                ),
                replace=True,
                models=models,
            )
    except Exception as exc:  # noqa: BLE001 - a bad models.yaml must not crash the CLI
        _registry.errors.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})


def _model_info_from_yaml(entry: _YamlModelEntry, provider: str, source: str) -> ModelInfo:
    """Require explicit zero pricing for free models; otherwise stay conservative."""
    fallback_input, fallback_output = (1.00, 5.00)
    if entry.input_cost_per_mtok is None or entry.output_cost_per_mtok is None:
        _registry.errors.append(
            {
                "source": source,
                "error": (
                    f"provider '{provider}' model '{entry.id}' omits pricing; "
                    "using conservative fallback pricing (set both costs to 0 for a free model)"
                ),
            }
        )
    return ModelInfo(
        id=entry.id,
        provider=provider,
        input_cost_per_mtok=(
            entry.input_cost_per_mtok
            if entry.input_cost_per_mtok is not None
            else fallback_input
        ),
        output_cost_per_mtok=(
            entry.output_cost_per_mtok
            if entry.output_cost_per_mtok is not None
            else fallback_output
        ),
        context_window=entry.context_window,
    )


def _openai_compat_factory(base_url: str, api_key_env: str | None) -> ProviderFactory:
    def factory(ctx: BuildContext) -> Any:
        if ctx.base is not None and (ctx.base / ".env").exists():
            try:
                from dotenv import load_dotenv

                load_dotenv(ctx.base / ".env")
            except ImportError:  # pragma: no cover
                pass
        api_key = os.environ.get(api_key_env) if api_key_env else None
        if api_key_env and not api_key:
            raise SpecError(
                f"{api_key_env} is not set (required by this provider's models.yaml entry)."
            )
        from hiveloom.models.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(base_url, api_key=api_key)

    return factory


# --------------------------------------------------------------------------- #
# Introspection & reset
# --------------------------------------------------------------------------- #
def status() -> dict[str, Any]:
    """A summary of what is loaded: registrations by source, plus load errors."""
    ensure_environment_loaded()
    by_source: dict[str, list[dict[str, str]]] = {}
    for reg in _registry.registrations:
        if reg["source"] == "builtin":
            continue
        by_source.setdefault(reg["source"], []).append(
            {"kind": reg["kind"], "name": reg["name"]}
        )
    return {
        "sources": [
            {"source": src, "registered": regs} for src, regs in by_source.items()
        ],
        "errors": list(_registry.errors),
        "providers": sorted(_registry.providers),
    }


def reset() -> None:
    """Restore the builtins-only state (used by tests and ``/reload``-style flows).

    The catalog dicts are mutated in place: the spec schema holds references to
    them, so they must never be replaced wholesale.
    """
    global _registry
    for kind, entries in catalog.CATALOGS.items():
        for name in [n for n in entries if n not in _BUILTIN_NAMES[kind]]:
            del entries[name]
    builtin_factories = {
        kind: {
            name: factory
            for name, factory in _registry.factories.get(kind, {}).items()
            if name in _BUILTIN_NAMES[kind]
        }
        for kind in catalog.CATALOGS
    }
    _registry = _Registry(factories=builtin_factories)
