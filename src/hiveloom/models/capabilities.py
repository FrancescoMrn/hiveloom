"""Provider-neutral model capability probes, identity policy, and cache."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from hiveloom import __version__, ext
from hiveloom.errors import SpecError
from hiveloom.models.provider import (
    ModelCapabilities,
    ModelConfig,
    ModelProvider,
    ProviderCapabilityObservation,
    Usage,
)
from hiveloom.paths import hiveloom_home
from hiveloom.spec.loader import atomic_write_text

IdentityPolicy = Literal["warn", "exact", "alias"]


class CapabilityEvidence(BaseModel):
    """One capability value and whether it was declared or observed."""

    value: bool | None = None
    source: Literal["declared", "observed", "unknown"] = "unknown"


class IdentityEvidence(BaseModel):
    """Requested/effective model comparison under an explicit policy."""

    policy: IdentityPolicy
    status: Literal["exact", "alias", "mismatch", "unknown"]
    accepted: bool
    accepted_models: list[str] = Field(default_factory=list)
    warning: str = ""


class ModelProbeResult(BaseModel):
    """Machine-readable model identity and capability evidence."""

    requested_provider: str
    requested_model: str
    effective_provider: str | None = None
    effective_models: list[str] = Field(default_factory=list)
    identity: IdentityEvidence
    capabilities: dict[str, CapabilityEvidence]
    aliases: list[str] = Field(default_factory=list)
    provider_request_ids: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    cost_source: str = "none"
    live: bool = False
    cached: bool = False
    calls: int = 0
    adapter_digest: str
    probed_at: str
    expires_at: str | None = None


class ModelProbePlan(BaseModel):
    """The side effects a caller must acknowledge before a live probe."""

    contacts_provider: bool
    maximum_model_calls: int
    provider_cost_possible: bool
    note: str


def probe_plan(*, live: bool) -> ModelProbePlan:
    if not live:
        return ModelProbePlan(
            contacts_provider=False,
            maximum_model_calls=0,
            provider_cost_possible=False,
            note="Declared-only mode performs no provider I/O.",
        )
    return ModelProbePlan(
        contacts_provider=True,
        maximum_model_calls=2,
        provider_cost_possible=True,
        note=(
            "Live mode sends a small tool request and may send one reasoning-replay "
            "request. The provider may bill both calls."
        ),
    )


def _adapter_digest(provider_name: str, provider: ModelProvider | None) -> str:
    info = ext.provider_info(provider_name)
    material = {
        "hiveloom_version": __version__,
        "provider": provider_name,
        "registration": info.model_dump(mode="json") if info is not None else {},
        "adapter": "",
        "implementation": "",
    }
    if provider is not None:
        provider_type = type(provider)
        material["adapter"] = f"{provider_type.__module__}:{provider_type.__qualname__}"
        try:
            source = inspect.getsourcefile(provider_type)
        except TypeError:
            source = None
        if source is not None and Path(source).is_file():
            material["implementation"] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity(
    requested: str,
    effective: list[str],
    *,
    policy: IdentityPolicy,
    aliases: list[str],
) -> IdentityEvidence:
    accepted_models = list(dict.fromkeys([requested, *aliases]))
    if not effective:
        accepted = policy == "warn"
        return IdentityEvidence(
            policy=policy,
            status="unknown",
            accepted=accepted,
            accepted_models=accepted_models,
            warning="provider did not report an effective model identity",
        )
    exact = all(model == requested for model in effective)
    alias_match = all(model in accepted_models for model in effective)
    if exact:
        return IdentityEvidence(
            policy=policy,
            status="exact",
            accepted=True,
            accepted_models=accepted_models,
        )
    if policy == "alias" and alias_match:
        return IdentityEvidence(
            policy=policy,
            status="alias",
            accepted=True,
            accepted_models=accepted_models,
        )
    warning = (
        f"provider served {', '.join(effective)} for requested model {requested}"
    )
    return IdentityEvidence(
        policy=policy,
        status="mismatch",
        accepted=policy == "warn",
        accepted_models=accepted_models,
        warning=warning,
    )


def _capability_evidence(
    declared: ModelCapabilities,
    observed: ModelCapabilities | None,
) -> dict[str, CapabilityEvidence]:
    result: dict[str, CapabilityEvidence] = {}
    for name in ("tool_calling", "structured_output", "reasoning_replay"):
        observed_value = getattr(observed, name) if observed is not None else None
        declared_value = getattr(declared, name)
        if observed_value is not None:
            result[name] = CapabilityEvidence(value=observed_value, source="observed")
        elif declared_value is not None:
            result[name] = CapabilityEvidence(value=declared_value, source="declared")
        else:
            result[name] = CapabilityEvidence()
    return result


def _cache_path() -> Path:
    return hiveloom_home() / "model-probes.json"


def _load_cache() -> dict[str, dict]:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _cache_key(
    provider: str,
    model: str,
    adapter_digest: str,
    policy: IdentityPolicy,
    aliases: list[str],
) -> str:
    value = json.dumps(
        {
            "provider": provider,
            "model": model,
            "adapter_digest": adapter_digest,
            "policy": policy,
            "aliases": sorted(set(aliases)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cached_result(key: str, now: datetime) -> ModelProbeResult | None:
    raw = _load_cache().get(key)
    if not isinstance(raw, dict):
        return None
    try:
        result = ModelProbeResult.model_validate(raw)
        expires = datetime.fromisoformat(result.expires_at or "")
    except (ValueError, TypeError):
        return None
    if expires <= now:
        return None
    return result.model_copy(update={"cached": True})


def _store_cache(key: str, result: ModelProbeResult) -> None:
    cache = _load_cache()
    cache[key] = result.model_dump(mode="json")
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(cache, indent=2, sort_keys=True) + "\n")


def probe_model(
    provider_name: str,
    model_id: str,
    *,
    provider: ModelProvider | None = None,
    live: bool = False,
    policy: IdentityPolicy = "warn",
    aliases: list[str] | None = None,
    refresh: bool = False,
    ttl_seconds: int = 86_400,
    now: datetime | None = None,
) -> ModelProbeResult:
    """Return declared or live-observed capabilities with identity enforcement."""
    alias_values = list(dict.fromkeys(aliases or []))
    timestamp = now or datetime.now(UTC)
    adapter_digest = _adapter_digest(provider_name, provider)
    key = _cache_key(provider_name, model_id, adapter_digest, policy, alias_values)
    if live and not refresh:
        cached = _cached_result(key, timestamp)
        if cached is not None:
            return cached

    if provider is not None:
        declared = provider.declared_capabilities(model_id)
    else:
        info = ext.model_info(model_id)
        declared = ModelCapabilities(
            tool_calling=info.supports_tool_calling if info is not None else None,
            structured_output=(
                info.supports_structured_output if info is not None else None
            ),
            reasoning_replay=(
                info.supports_reasoning_replay if info is not None else None
            ),
        )
    observation = ProviderCapabilityObservation()
    if live:
        if provider is None:
            raise ValueError("a provider instance is required for a live probe")
        observation = provider.probe_capabilities(
            ModelConfig(id=model_id, provider=provider_name, max_tokens=128)
        )
    expires_at = (
        (timestamp + timedelta(seconds=ttl_seconds)).isoformat() if live else None
    )
    result = ModelProbeResult(
        requested_provider=provider_name,
        requested_model=model_id,
        effective_provider=provider_name if observation.calls else None,
        effective_models=observation.effective_models,
        identity=_identity(
            model_id,
            observation.effective_models,
            policy=policy,
            aliases=alias_values,
        ),
        capabilities=_capability_evidence(
            declared,
            observation.capabilities if live else None,
        ),
        aliases=alias_values,
        provider_request_ids=observation.provider_request_ids,
        usage=observation.usage,
        cost_usd=observation.cost_usd,
        cost_source=observation.cost_source,
        live=live,
        calls=observation.calls,
        adapter_digest=adapter_digest,
        probed_at=timestamp.isoformat(),
        expires_at=expires_at,
    )
    if live:
        _store_cache(key, result)
    return result


def require_compatible_probe(result: ModelProbeResult) -> None:
    """Abort a strict batch before cases run when identity is not accepted."""
    if not result.identity.accepted:
        raise SpecError(result.identity.warning or "model identity probe was not accepted")
