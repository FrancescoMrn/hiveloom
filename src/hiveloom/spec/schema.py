"""Pydantic models for the hiveloom harness spec.

This module is the single source of truth for the harness contract: the JSON
schema, the annotated YAML template, ``hiveloom explain``, and the generator's
meta-prompt are all derived from these models (never hand-written), so the
contract and the code can never drift.

The spec is a declarative YAML document with code escape hatches
(``path.py:function``). YAML is what the evolver mutates cheaply; code hooks
carry company-specific logic.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

from hiveloom import catalog


# --------------------------------------------------------------------------- #
# Tool / guardrail / validator references (builtin | code union members)
# --------------------------------------------------------------------------- #
class _CodeRef(BaseModel):
    """A ``path.py:function`` reference to a user code hook."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        description="Code hook as 'relative/path.py:function_name'.",
    )

    @field_validator("code")
    @classmethod
    def _check_code_format(cls, value: str) -> str:
        if value.count(":") != 1 or value.endswith(":") or value.startswith(":"):
            raise ValueError(
                "code hook must be 'relative/path.py:function_name' "
                f"(got {value!r})"
            )
        return value


class _BuiltinRef(BaseModel):
    """A reference to a builtin by name, with inline parameters.

    Concrete subclasses set ``_catalog`` so parameters can be validated against
    the single-source-of-truth catalog. Extra keys are the builtin's inline
    parameters (e.g. ``value: 0.50``).
    """

    model_config = ConfigDict(extra="allow")

    builtin: str = Field(description="Name of the builtin (see `hiveloom catalog`).")

    _catalog: ClassVar[dict[str, catalog.CatalogEntry]] = {}
    _kind: ClassVar[str] = "builtin"

    @model_validator(mode="after")
    def _check_builtin(self) -> _BuiltinRef:
        registry = type(self)._catalog
        if self.builtin not in registry:
            valid = ", ".join(sorted(registry))
            raise ValueError(
                f"unknown {type(self)._kind} builtin '{self.builtin}' "
                f"(valid: {valid}). If it comes from an extension pack, "
                "install the pack first (see hiveloom.lock and `hiveloom extensions`)."
            )
        problems = catalog.validate_builtin_params(
            registry[self.builtin], dict(self.model_extra or {})
        )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def params(self) -> dict[str, Any]:
        """Return the inline parameters supplied for this builtin."""
        return dict(self.model_extra or {})


class BuiltinToolRef(_BuiltinRef):
    """A builtin tool reference (e.g. ``builtin: file_read``)."""

    _catalog = catalog.BUILTIN_TOOLS
    _kind = "tool"

    deferred: bool | None = Field(
        default=None,
        description=(
            "If true, the tool is registered but inactive: it stays out of the "
            "model's tool payload until the auto-added search_tools tool "
            "activates it. Saves context for harnesses with many tools."
        ),
    )


class CodeToolRef(_CodeRef):
    """A code-hook tool reference; a description is required for tools."""

    description: str = Field(description="What the tool does (shown to the model).")
    deferred: bool | None = Field(
        default=None,
        description="If true, inactive until activated via search_tools (see BuiltinToolRef).",
    )


class BuiltinGuardrailRef(_BuiltinRef):
    """A builtin guardrail reference (e.g. ``builtin: max_cost_usd``)."""

    _catalog = catalog.BUILTIN_GUARDRAILS
    _kind = "guardrail"


class CodeGuardrailRef(_CodeRef):
    """A code-hook guardrail reference."""

    description: str | None = Field(default=None, description="Optional human note.")


class BuiltinValidatorRef(_BuiltinRef):
    """A builtin verifier reference (e.g. ``builtin: output_schema``)."""

    _catalog = catalog.BUILTIN_VALIDATORS
    _kind = "validator"


class CodeValidatorRef(_CodeRef):
    """A code-hook verifier reference (the primary correctness extension point)."""

    description: str | None = Field(default=None, description="Optional human note.")


def _check_event_name(value: str) -> str:
    from hiveloom.events import EVENTS

    if value not in EVENTS:
        raise ValueError(f"unknown event '{value}' (valid: {', '.join(EVENTS)})")
    return value


class BuiltinHookRef(_BuiltinRef):
    """An extension-registered event handler (e.g. ``builtin: audit_log``).

    The field is named ``event`` (not ``on``) because unquoted ``on`` is a
    boolean in YAML 1.1 — a hand-edited spec would silently break.
    """

    _catalog = catalog.BUILTIN_HOOKS
    _kind = "hook"

    event: str = Field(description="Lifecycle event to handle (see hiveloom.events.EVENTS).")

    _check_event = field_validator("event")(_check_event_name)


class CodeHookRef(_CodeRef):
    """A code-hook event handler attached to a lifecycle event."""

    event: str = Field(description="Lifecycle event to handle (see hiveloom.events.EVENTS).")
    description: str | None = Field(default=None, description="Optional human note.")

    _check_event = field_validator("event")(_check_event_name)


def _ref_kind(value: Any) -> str | None:
    """Discriminate a ref as ``'builtin'`` or ``'code'`` by its shape."""
    if isinstance(value, BaseModel):
        if hasattr(value, "builtin"):
            return "builtin"
        if hasattr(value, "code"):
            return "code"
        return None
    if isinstance(value, dict):
        if "builtin" in value:
            return "builtin"
        if "code" in value:
            return "code"
    return None


ToolRef = Annotated[
    Annotated[BuiltinToolRef, Tag("builtin")] | Annotated[CodeToolRef, Tag("code")],
    Discriminator(_ref_kind),
]

GuardrailRef = Annotated[
    Annotated[BuiltinGuardrailRef, Tag("builtin")] | Annotated[CodeGuardrailRef, Tag("code")],
    Discriminator(_ref_kind),
]

ValidatorRef = Annotated[
    Annotated[BuiltinValidatorRef, Tag("builtin")] | Annotated[CodeValidatorRef, Tag("code")],
    Discriminator(_ref_kind),
]

HookRef = Annotated[
    Annotated[BuiltinHookRef, Tag("builtin")] | Annotated[CodeHookRef, Tag("code")],
    Discriminator(_ref_kind),
]


# --------------------------------------------------------------------------- #
# MCP server references (stdio | http union, discriminated on `transport`)
# --------------------------------------------------------------------------- #
_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _check_mcp_server_name(value: str) -> str:
    if not _MCP_NAME_RE.match(value):
        raise ValueError(
            f"mcp server name {value!r} must match [a-zA-Z0-9_-]+ (it becomes "
            "the mcp__<name>__<tool> prefix on every tool it exposes)"
        )
    return value


class McpStdioServerRef(BaseModel):
    """An MCP server launched as a local subprocess and spoken to over stdio.

    ARBITRARY LOCAL EXEC: ``command`` runs with the harness's own permissions
    the moment its tool registry is built (``build_registry`` connects
    eagerly, including for ``run --dry-run``). Same risk class as
    ``extensions`` — see ``ALWAYS_FROZEN``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "Unique identifier for this server; becomes the mcp__<name>__<tool> "
            "prefix on every tool it exposes. Must match [a-zA-Z0-9_-]+."
        )
    )
    transport: Literal["stdio"] = Field(
        default="stdio",
        description=(
            "Discriminates this variant from `McpHttpServerRef`. Must be written "
            "explicitly as `transport: stdio` in YAML — the union dispatches on "
            "this literal tag before field defaults apply."
        ),
    )
    command: str = Field(
        description=(
            "Executable to launch. ARBITRARY LOCAL EXEC — runs with the "
            "harness's permissions. Use `hiveloom add mcp-server` to add one."
        )
    )
    args: list[str] = Field(
        default_factory=list, description="Command-line arguments passed to `command`."
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Literal environment variables merged into a minimal safe env for "
            "the subprocess — NOT a full host environment passthrough."
        ),
    )
    env_from_host_env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "target-var -> host env-var name, resolved from this process's "
            "environment at connect time (for secrets that must not live in YAML)."
        ),
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Working directory for the subprocess, resolved relative to the "
            "harness directory (defaults to the harness directory itself when "
            "unset). NOT a security boundary: unlike file_read/file_write, "
            "traversal (e.g. '../..') is not constrained, and an absolute path "
            "here overrides the harness directory entirely — `command` is "
            "already arbitrary local exec, so sandboxing `cwd` alone would not "
            "add a real boundary."
        ),
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Allowlist of remote tool names to expose; omit to expose every "
            "tool the server reports."
        ),
    )
    deferred: bool = Field(
        default=False,
        description=(
            "Register discovered tools inactive; the auto-added search_tools "
            "tool activates them on demand."
        ),
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=600,
        description="Timeout in seconds for connect/initialize and for each tool call.",
    )

    _check_name = field_validator("name")(_check_mcp_server_name)


class McpHttpServerRef(BaseModel):
    """An MCP server reached over the Streamable HTTP transport."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "Unique identifier for this server; becomes the mcp__<name>__<tool> "
            "prefix on every tool it exposes. Must match [a-zA-Z0-9_-]+."
        )
    )
    transport: Literal["http"] = Field(
        default="http",
        description=(
            "Discriminates this variant from `McpStdioServerRef`. Must be written "
            "explicitly as `transport: http` in YAML — the union dispatches on "
            "this literal tag before field defaults apply."
        ),
    )
    url: str = Field(description="Base URL of the server's Streamable HTTP endpoint.")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Literal HTTP headers sent with every request."
    )
    header_env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "header-name -> host env-var name, resolved from this process's "
            "environment at connect time (e.g. Authorization -> ACME_MCP_TOKEN)."
        ),
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Allowlist of remote tool names to expose; omit to expose every "
            "tool the server reports."
        ),
    )
    deferred: bool = Field(
        default=False,
        description=(
            "Register discovered tools inactive; the auto-added search_tools "
            "tool activates them on demand."
        ),
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=600,
        description="Timeout in seconds for connect/initialize and for each tool call.",
    )

    _check_name = field_validator("name")(_check_mcp_server_name)


McpServerRef = Annotated[McpStdioServerRef | McpHttpServerRef, Field(discriminator="transport")]


# --------------------------------------------------------------------------- #
# Section models
# --------------------------------------------------------------------------- #
class ModelConfig(BaseModel):
    """Which model runs *inside* the harness, and its sampling settings."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        default="claude",
        description=(
            "Model provider name. 'claude' is builtin; more come from extensions "
            "or ~/.hiveloom/models.yaml (see `hiveloom extensions`)."
        ),
    )
    id: str = Field(default="claude-haiku-4-5", description="Model id to execute with.")
    max_tokens: int = Field(
        default=4096, gt=0, le=32768, description="Max output tokens per call."
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0, description="Sampling temperature.")

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, value: str) -> str:
        from hiveloom import ext

        names = ext.provider_names()
        if value not in names:
            raise ValueError(
                f"unknown model provider '{value}' (available: {', '.join(names)}). "
                "Register providers via an extension or ~/.hiveloom/models.yaml."
            )
        return value

    @model_validator(mode="after")
    def _check_model_belongs_to_provider(self) -> ModelConfig:
        from hiveloom import ext

        info = ext.model_info(self.id)
        if info is None:
            raise ValueError(
                f"unknown model id '{self.id}' for provider '{self.provider}'. "
                "Register it through an extension or ~/.hiveloom/models.yaml."
            )
        if info.provider and info.provider != self.provider:
            raise ValueError(
                f"model id '{self.id}' belongs to provider '{info.provider}', not '{self.provider}'"
            )
        return self


class CompactionConfig(BaseModel):
    """When and how to compact context once the token budget is pressured."""

    model_config = ConfigDict(extra="forbid")

    trigger_at_pct: int = Field(
        default=80, ge=1, le=100,
        description="Compact when context reaches this percent of the budget.",
    )
    method: str = Field(
        default="summarize",
        description=(
            "How to reclaim context space. See `hiveloom catalog compaction` "
            "(builtins plus extension-registered methods)."
        ),
    )

    @field_validator("method")
    @classmethod
    def _check_method(cls, value: str) -> str:
        from hiveloom import ext

        ext.ensure_environment_loaded()
        if value not in catalog.BUILTIN_COMPACTION:
            valid = ", ".join(sorted(catalog.BUILTIN_COMPACTION))
            raise ValueError(f"unknown compaction method '{value}' (valid: {valid})")
        return value


class ContextConfig(BaseModel):
    """Context assembly, budgeting, and compaction policy."""

    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int = Field(
        default=30000, gt=0, le=1_000_000, description="Input token budget per model call."
    )
    strategy: Literal["rolling", "full", "summary"] = Field(
        default="rolling", description="How message history is assembled."
    )
    compaction: CompactionConfig = Field(
        default_factory=CompactionConfig, description="Compaction trigger and method."
    )
    pinned: list[str] = Field(
        default_factory=lambda: ["system_prompt", "task_statement"],
        description="Context items always kept, never compacted.",
    )


class LoopConfig(BaseModel):
    """The agent loop policy and stop conditions."""

    model_config = ConfigDict(extra="forbid")

    policy: str = Field(
        default="react",
        description=(
            "Loop policy. See `hiveloom catalog policies` (builtins plus "
            "extension-registered policies)."
        ),
    )

    @field_validator("policy")
    @classmethod
    def _check_policy(cls, value: str) -> str:
        from hiveloom import ext

        ext.ensure_environment_loaded()
        if value not in catalog.POLICIES:
            valid = ", ".join(sorted(catalog.POLICIES))
            raise ValueError(f"unknown loop policy '{value}' (valid: {valid})")
        return value
    max_turns: int = Field(
        default=20, gt=0, le=1_000, description="Max loop turns before stopping."
    )
    on_tool_error: Literal["retry_once", "surface_to_model", "abort"] = Field(
        default="retry_once", description="What to do when a tool call errors."
    )
    require_verification: bool = Field(
        default=True, description="If true, the loop cannot succeed without verify passing."
    )


class OnFailConfig(BaseModel):
    """What happens when verification fails."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["retry_with_feedback", "abort"] = Field(
        default="retry_with_feedback",
        description="On verify failure: retry with feedback injected, or abort.",
    )
    max_retries: int = Field(
        default=2, ge=0, le=100, description="Max verify-driven retries before giving up."
    )


class VerifyConfig(BaseModel):
    """Verification: the reward signal for evolution."""

    model_config = ConfigDict(extra="forbid")

    validators: list[ValidatorRef] = Field(
        default_factory=list, description="Validators run against the run output."
    )
    on_fail: OnFailConfig = Field(
        default_factory=OnFailConfig, description="Behaviour when a validator fails."
    )


class LoggingConfig(BaseModel):
    """Trace persistence policy. ``redact`` is frozen from evolution."""

    model_config = ConfigDict(extra="forbid")

    trace_dir: str = Field(
        default="./.hiveloom/traces",
        description="Where traces are written. In-folder by default so memory travels.",
    )
    level: Literal["full", "tool_calls_only"] = Field(
        default="full", description="Trace verbosity."
    )
    redact: list[str] = Field(
        default_factory=list,
        description="Regexes scrubbed from persisted traces (frozen from evolution).",
    )


def _default_mutable() -> list[str]:
    return [
        "system_prompt",
        "loop.max_turns",
        "loop.policy",
        "context.strategy",
        "tools",
    ]


def _default_frozen() -> list[str]:
    return ["guardrails", "model"]


class EvolutionConfig(BaseModel):
    """What the evolver may and may not change."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Whether evolution is allowed at all.")
    mutable: list[str] = Field(
        default_factory=_default_mutable,
        description="Spec paths the evolver MAY change.",
    )
    frozen: list[str] = Field(
        default_factory=_default_frozen,
        description="Spec paths the evolver must NEVER change.",
    )


# Paths the evolver must never touch, regardless of a spec's declared `frozen`
# list. Enforced in the evolver (M4); declared here as the safety contract.
# `extensions` load arbitrary code, so evolution can never add or change them.
# Hooks can transform tool inputs/results and final output, placing them
# upstream of guardrails. They therefore share the non-negotiable evolution
# boundary with guardrails themselves. `mcp_servers` is the same risk class as
# `extensions` (arbitrary local exec / arbitrary network endpoint).
ALWAYS_FROZEN: tuple[str, ...] = (
    "guardrails",
    "model",
    "logging.redact",
    "extensions",
    "hooks",
    "mcp_servers",
)


# --------------------------------------------------------------------------- #
# Top-level spec
# --------------------------------------------------------------------------- #
class HarnessSpec(BaseModel):
    """A complete, validated harness specification."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="0.2.0", description="Spec format version.")
    name: str = Field(description="Harness name (used for the Hive and packaging).")
    description: str = Field(description="One-line description of the task.")
    extensions: list[str] = Field(
        default_factory=list,
        description=(
            "Extension modules loaded before this spec is validated: relative "
            ".py paths or installed module names, each exposing "
            "hiveloom_extension(hive). Frozen from evolution."
        ),
    )
    model: ModelConfig = Field(default_factory=ModelConfig, description="Model config.")
    system_prompt: str = Field(description="System prompt for the harness model.")
    tools: list[ToolRef] = Field(default_factory=list, description="Tools available to the loop.")
    mcp_servers: list[McpServerRef] = Field(
        default_factory=list,
        description=(
            "MCP servers whose tools become ordinary dispatchable tools inside "
            "the loop (mcp__<name>__<tool>). Discovered eagerly when the tool "
            "registry is built — including for `run --dry-run` — and frozen "
            "from evolution: same risk class as `extensions` (arbitrary "
            "code/process). stdio servers are arbitrary local exec. Use "
            "`hiveloom add mcp-server` to add one."
        ),
    )
    skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skill names: each is a skills/<name>/SKILL.md folder. Only the "
            "name + description enter the system prompt; the model reads the "
            "full skill on demand (progressive disclosure — pair with the "
            "file_read tool)."
        ),
    )
    hooks: list[HookRef] = Field(
        default_factory=list,
        description=(
            "Lifecycle event handlers (code hooks or extension-registered "
            "builtins). See hiveloom.events for events and mutation semantics."
        ),
    )
    context: ContextConfig = Field(
        default_factory=ContextConfig, description="Context management policy."
    )
    guardrails: list[GuardrailRef] = Field(
        default_factory=list, description="Guardrails (frozen from evolution by design)."
    )
    loop: LoopConfig = Field(default_factory=LoopConfig, description="Agent loop policy.")
    verify: VerifyConfig = Field(default_factory=VerifyConfig, description="Verification policy.")
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig, description="Trace/logging policy."
    )
    evolution: EvolutionConfig = Field(
        default_factory=EvolutionConfig, description="Evolution policy."
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @model_validator(mode="after")
    def _ensure_cost_guardrail(self) -> HarnessSpec:
        """Safety invariant: the cost guardrail defaults ON even if omitted.

        If no ``max_cost_usd`` guardrail is present, inject one at 1.00 USD.
        """
        has_cost = any(
            isinstance(g, BuiltinGuardrailRef) and g.builtin == "max_cost_usd"
            for g in self.guardrails
        )
        if not has_cost:
            self.guardrails.append(BuiltinGuardrailRef(builtin="max_cost_usd", value=1.00))
        for guardrail in self.guardrails:
            if not isinstance(guardrail, BuiltinGuardrailRef):
                continue
            value = guardrail.params().get("value")
            limits = {
                "max_cost_usd": 10_000.0,
                "max_wall_clock_seconds": 86_400,
                "max_turns_hard_cap": 10_000,
            }
            if guardrail.builtin in limits and (
                not isinstance(value, (int, float)) or not 0 < value <= limits[guardrail.builtin]
            ):
                raise ValueError(
                    f"{guardrail.builtin}.value must be greater than 0 and at most "
                    f"{limits[guardrail.builtin]}"
                )
        return self

    @model_validator(mode="after")
    def _check_mcp_server_names_unique(self) -> HarnessSpec:
        seen: set[str] = set()
        for server in self.mcp_servers:
            if server.name in seen:
                raise ValueError(f"duplicate mcp server name '{server.name}'")
            seen.add(server.name)
        return self
