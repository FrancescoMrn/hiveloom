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
    # `__` is the delimiter in `mcp__<name>__<tool>`. If a server name may
    # contain it, the prefix is no longer injective — server `a__b` tool `c`
    # and server `a` tool `b__c` both flatten to `mcp__a__b__c`, and one
    # silently shadows the other in the registry. Forbid it so the mapping
    # stays one-to-one.
    if "__" in value:
        raise ValueError(
            f"mcp server name {value!r} must not contain '__' (it is the "
            "delimiter in the mcp__<name>__<tool> tool prefix)"
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
            "Model provider name. Builtins cover the major labs (claude, openai, "
            "gemini, mistral, deepseek, xai, groq, openrouter, together, fireworks, "
            "ollama, vllm); more come from extensions or ~/.hiveloom/models.yaml "
            "(see `hiveloom models`)."
        ),
    )
    id: str = Field(default="claude-haiku-4-5", description="Model id to execute with.")
    max_tokens: int = Field(
        default=4096, gt=0, le=32768, description="Max output tokens per call."
    )
    temperature: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Sampling temperature. None omits it — required for models that deprecate it.",
    )

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
            # An open-catalog provider (every OpenAI-compatible lab, the
            # aggregators, and local servers) routes ids hiveloom cannot
            # enumerate, so an unregistered id is normal there and must not
            # block a spec — a model released after this hiveloom version
            # still has to be usable. Cost estimation falls back to the
            # provider's default price; see `ext.model_pricing`.
            provider = ext.provider_info(self.provider)
            if provider is not None and provider.open_catalog:
                return self
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


class SequentialStep(BaseModel):
    """One enforceable phase in the builtin sequential-steps policy."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Stable step identifier used in traces and Hive records.",
    )
    instruction: str = Field(
        min_length=1,
        max_length=5_000,
        description="Objective pinned into context while this step is active.",
    )
    tools: list[str] | None = Field(
        default=None,
        max_length=1_000,
        description=(
            "Tools exposed during this step. Omit to preserve the current active set; "
            "use an empty list for a tool-free phase."
        ),
    )
    require_tool_calls: list[str] = Field(
        default_factory=list,
        max_length=1_000,
        description="Tool names that must succeed before the step can complete.",
    )
    max_model_calls: int | None = Field(
        default=None, ge=1, le=1_000, description="Optional model-call cap for this step."
    )
    max_tool_calls: int | None = Field(
        default=None, ge=1, le=10_000, description="Optional tool-call cap for this step."
    )

    @field_validator("instruction")
    @classmethod
    def _instruction_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("step instruction must not be blank")
        return value

    @field_validator("tools", "require_tool_calls")
    @classmethod
    def _unique_tool_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not name.strip() for name in value):
            raise ValueError("step tool names must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("step tool names must be unique")
        return value

    @model_validator(mode="after")
    def _required_tools_are_exposed(self) -> SequentialStep:
        if self.tools is not None:
            hidden = sorted(set(self.require_tool_calls) - set(self.tools))
            if hidden:
                raise ValueError(
                    "required tool calls must also appear in step.tools: "
                    + ", ".join(hidden)
                )
        return self


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

    steps: list[str | SequentialStep] = Field(
        default_factory=list,
        description=(
            "Ordered objectives for sequential_steps. Legacy strings keep their current "
            "instruction-only behavior. Objects can constrain tools, required successful "
            "calls, and per-step model/tool call limits. Ignored by other policies."
        ),
    )

    @model_validator(mode="after")
    def _check_sequential_steps(self) -> LoopConfig:
        # Deliberately one-directional: only reject sequential_steps with empty
        # steps, never the reverse. Every `hiveloom set` commits and fully
        # re-validates immediately (see construct._commit), so a two-directional
        # check would make the natural workflow `hiveloom set loop.steps
        # '[...]'` then `hiveloom set loop.policy sequential_steps` fail on the
        # first command. Non-empty steps with any other policy are allowed.
        if self.policy == "sequential_steps" and not self.steps:
            raise ValueError("loop.policy 'sequential_steps' requires a non-empty loop.steps")
        ids = [step.id for step in self.steps if isinstance(step, SequentialStep)]
        if len(ids) != len(set(ids)):
            raise ValueError("structured sequential step ids must be unique")
        return self

    max_turns: int = Field(
        default=20, gt=0, le=1_000, description="Max loop turns before stopping."
    )
    on_tool_error: Literal["retry_once", "surface_to_model", "abort"] = Field(
        default="retry_once", description="What to do when a tool call errors."
    )
    tool_execution: Literal["sequential", "parallel"] = Field(
        default="sequential",
        description="How a turn's tool calls run. 'sequential' runs each call's "
        "full pipeline in order. 'parallel' preflights guardrails/hooks for "
        "every call in source order, executes the surviving calls concurrently, "
        "then finalizes results in source order. Only opt in when the "
        "harness's tools are safe to run concurrently.",
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


_PLAYBOOK_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class PlaybookRef(BaseModel):
    """One named mode the harness can work in.

    A skill is reference material the model *reads*; a playbook is a
    configuration the runtime *applies*. Entering one swaps in a prompt
    fragment, narrows the active tools, and adds mode-specific validators, so a
    single harness can cover what would otherwise need several — a profiling
    mode that may only read, an action mode that may also propose — while
    keeping one conversation and one evolving spec.

    Because each switch is traced, the Hive measures success, cost, and turns
    *per playbook*, and evolution can rewrite one mode's prompt on evidence
    without touching a mode that already works.

    ``on_enter``/``on_exit`` run arbitrary code and are therefore frozen from
    evolution (see :data:`ALWAYS_FROZEN` and the evolver's gate); ``prompt`` is
    the part meant to be evolved.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique mode name; what switch_playbook takes.")
    description: str = Field(
        description=(
            "What this mode is for. Shown to the model in the playbook index, "
            "so it can pick the right mode — write it as selection guidance."
        )
    )
    prompt: str | None = Field(
        default=None,
        description=(
            "Relative path to a markdown file appended to the system prompt "
            "while this playbook is active (e.g. playbooks/targeting.md)."
        ),
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Tool names active in this mode; omit to leave the harness's tool "
            "activation untouched. Narrowing here is what makes a mode a mode."
        ),
    )
    validators: list[ValidatorRef] = Field(
        default_factory=list,
        description="Validators added to verify.validators while this mode is active.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Model id to execute with while this mode is active; omit to keep "
            "the harness's model. Profile on a cheap model, decide on an "
            "expensive one, in one harness and one conversation. Leaving the "
            "mode restores the previous model."
        ),
    )
    model_provider: str | None = Field(
        default=None,
        description=(
            "Provider serving this playbook's `model`; omit to use the "
            "harness's provider. Set it to cross providers mid-run."
        ),
    )
    on_enter: str | None = Field(
        default=None,
        description=(
            "Code hook 'path.py:function' run when this playbook is entered. "
            "Return {'context': str} to inject a note, or {'block': True, "
            "'reason': str} to refuse entry. Frozen from evolution."
        ),
    )
    on_exit: str | None = Field(
        default=None,
        description=(
            "Code hook 'path.py:function' run when leaving this playbook. "
            "Return {'block': True, 'reason': str} to refuse the exit — a "
            "boundary gate, e.g. 'you entered targeting and proposed nothing'. "
            "Frozen from evolution."
        ),
    )
    entry: bool = Field(
        default=False,
        description="Start the run in this playbook (defaults to the first one).",
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _PLAYBOOK_NAME_RE.match(value):
            raise ValueError(f"playbook name {value!r} must match [a-zA-Z0-9_-]+")
        return value

    @field_validator("on_enter", "on_exit")
    @classmethod
    def _check_hook_ref(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.count(":") != 1 or value.endswith(":") or value.startswith(":"):
            raise ValueError(
                "playbook hook must be 'relative/path.py:function_name' "
                f"(got {value!r})"
            )
        return value


class LoggingConfig(BaseModel):
    """Trace persistence policy. ``redact`` is frozen from evolution."""

    model_config = ConfigDict(extra="forbid")

    trace_dir: str = Field(
        default="./.hiveloom/traces",
        description="Where traces are written. In-folder by default so memory travels.",
    )
    level: Literal["journal", "summary"] = Field(
        default="journal",
        description=(
            "What the run journal keeps. 'journal' records the conversation "
            "progressively and is forkable. 'summary' keeps outcomes and tool "
            "activity but no context bodies — smaller, and NOT forkable."
        ),
    )

    @field_validator("level", mode="before")
    @classmethod
    def _accept_pre_1_0_levels(cls, value: Any) -> Any:
        """Accept the 0.x names. They said how much; the new ones say what for.

        A harness folder is a portable artifact that outlives the runtime that
        wrote it, so an old `harness.yaml` must keep loading rather than
        failing validation on a rename.
        """
        return {"full": "journal", "tool_calls_only": "summary"}.get(value, value)
    redact: list[str] = Field(
        default_factory=list,
        description="Regexes scrubbed from persisted traces (frozen from evolution).",
    )
    snapshot_files: bool = Field(
        default=False,
        description=(
            "Inline the harness's code hooks, validators, and skills into the "
            "run_started snapshot, not just their hashes. Makes a journal "
            "portable — forkable without the original folder — at the cost of "
            "size. The manifest of hashes is always recorded either way."
        ),
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


# A bare `gt=0` bound on cooldown_hours would still accept e.g. 1e-9, which is
# functionally "no cooldown" (no two runs complete within nanoseconds of each
# other) — that would make the "cannot be disabled" claim below false. One
# minute is generous enough to constrain no legitimate configuration while
# making the floor real. No ceiling: an unusually large cooldown just means
# less auto-proposing, the safe direction (unlike a cost guardrail's ceiling).
MIN_COOLDOWN_HOURS = 1 / 60


class AutoProposeConfig(BaseModel):
    """Opt-in: draft (never apply) an evolution proposal after a failing run."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Draft a gated proposal after a failing run. Never auto-applies.",
    )
    min_failures: int = Field(
        default=5, ge=1,
        description=(
            "Non-success runs of the current harness version (since the last "
            "auto-proposal) required before drafting."
        ),
    )
    cooldown_hours: float = Field(
        default=24.0, ge=MIN_COOLDOWN_HOURS,
        description=(
            "Minimum gap between auto-drafted proposals for this harness. Cannot be "
            "removed: values below one minute are rejected. Each qualifying failing run "
            "costs a strong-model call unless the dedup pre-check catches it, so this is "
            "partly a spend guard; use `min_failures` for a different shape of restraint."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Strong-model override for auto-drafted proposals; else the CLI/env default.",
    )


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
    auto_propose: AutoProposeConfig = Field(
        default_factory=AutoProposeConfig,
        description="Automatic post-run proposal drafting (opt-in; drafts only, never applies).",
    )


# Paths the evolver must never touch, regardless of a spec's declared `frozen`
# list. Enforced in the evolver and declared here as the safety contract.
# `extensions` load arbitrary code, so evolution can never add or change them.
# Hooks can transform tool inputs/results and final output, placing them
# upstream of guardrails. They therefore share the non-negotiable evolution
# boundary with guardrails themselves. `mcp_servers` is the same risk class as
# `extensions` (arbitrary local exec / arbitrary network endpoint).
# `evolution.auto_propose` is its own paid, post-run trigger — a harness must
# never be able to enable that trigger via evolution itself (docs/spec.md
# documents it as never mutable; this is what makes that claim true).
# `id` is identity, not behaviour: letting evolution (or a remote caller)
# rewrite it would detach a harness from its own accumulated evidence.
ALWAYS_FROZEN: tuple[str, ...] = (
    "id",
    "guardrails",
    "model",
    "logging.redact",
    "extensions",
    "hooks",
    "mcp_servers",
    "evolution.auto_propose",
)

# Playbook fields that execute code, and so share the boundary above. They
# cannot be expressed in ALWAYS_FROZEN because playbooks are a *list*: the
# dotted paths would need an index or a wildcard, and both `_covered` and
# `touches_frozen` match literal dotted prefixes only. The evolver enforces
# these with a dedicated value-inspecting check (`_touches_playbook_code`),
# the same shape as its dangerous-tool check, so that rewriting the whole
# `playbooks` list cannot smuggle a hook in either.
# `model`/`model_provider` join them: top-level `model` is already in
# ALWAYS_FROZEN, and a per-playbook executor is the same decision by another
# route. Evolution must not be able to move a harness onto a pricier model —
# or onto a different lab — on its own initiative.
PLAYBOOK_FROZEN_FIELDS: tuple[str, ...] = (
    "on_enter",
    "on_exit",
    "model",
    "model_provider",
)


# --------------------------------------------------------------------------- #
# Top-level spec
# --------------------------------------------------------------------------- #
class HarnessSpec(BaseModel):
    """A complete, validated harness specification."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default="0.2.0",
        description=(
            "Harness document format version. Legacy `version` loads as this field; "
            "use `hiveloom migrate` to rewrite it canonically."
        ),
    )
    name: str = Field(description="Harness name (used for display and packaging).")
    id: str = Field(
        default="",
        description=(
            "Stable harness identity, generated by `init` (e.g. hl-3f2a...). "
            "The Hive keys run evidence, evolutions, and proposals on it, so "
            "two harnesses that happen to share a *name* can never pollute "
            "each other's stats. Empty on a pre-1.0 harness, which stays "
            "keyed by name; adopt one with `hiveloom set id hl-<hex>` — runs "
            "recorded before adoption remain keyed by the name. Frozen from "
            "evolution and from remote mutation."
        ),
    )
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
    playbooks: list[PlaybookRef] = Field(
        default_factory=list,
        description=(
            "Named modes the harness can switch between mid-run: each carries a "
            "prompt fragment, an active tool subset, extra validators, and "
            "enter/exit code hooks. Declaring any auto-adds the "
            "switch_playbook tool. Measured per playbook in the Hive."
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

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_version_field(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "version" not in value:
            return value
        data = dict(value)
        legacy = data.pop("version")
        if "schema_version" in data and data["schema_version"] != legacy:
            raise ValueError(
                "conflicting version and schema_version values; migrate from a "
                "document with one unambiguous format version"
            )
        data.setdefault("schema_version", legacy)
        return data

    @property
    def version(self) -> str:
        """Compatibility alias for SDK callers; serialize ``schema_version``."""
        return self.schema_version

    @property
    def identity(self) -> str:
        """The Hive key for this harness: its stable ``id``, or the name for a
        pre-1.0 spec that never adopted one."""
        return self.id or self.name

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

    def tool_names(self) -> set[str]:
        """Names the spec's tools will register under.

        A builtin registers under its catalog name; a code tool registers under
        its function name, which is the part after the colon in its ``code``
        ref. Derivable without importing anything, which is what lets playbook
        tool subsets be checked at validation time.
        """
        names: set[str] = set()
        for tool in self.tools:
            if isinstance(tool, BuiltinToolRef):
                names.add(tool.builtin)
            else:
                names.add(tool.code.split(":", 1)[1])
        return names

    @model_validator(mode="after")
    def _check_structured_steps(self) -> HarnessSpec:
        deferred: set[str] = set()
        for ref in self.tools:
            if not ref.deferred:
                continue
            deferred.add(
                ref.builtin
                if isinstance(ref, BuiltinToolRef)
                else ref.code.split(":", 1)[1]
            )
        deferred_mcp_prefixes = {
            f"mcp__{server.name}__" for server in self.mcp_servers if server.deferred
        }
        available = self.tool_names()
        if deferred or deferred_mcp_prefixes:
            available.add("search_tools")
        if self.playbooks:
            available.add("switch_playbook")
        for step in self.loop.steps:
            if not isinstance(step, SequentialStep):
                continue
            declared = set(step.tools or []) | set(step.require_tool_calls)
            known = {name for name in declared if not name.startswith("mcp__")}
            unknown = sorted(known - available)
            if unknown:
                raise ValueError(
                    f"sequential step '{step.id}' lists unknown tool(s): "
                    f"{', '.join(unknown)}. Declared tools: "
                    f"{', '.join(sorted(available)) or '(none)'}"
                )
            deferred_required = sorted(
                name
                for name in step.require_tool_calls
                if name in deferred
                or any(name.startswith(prefix) for prefix in deferred_mcp_prefixes)
            )
            if deferred_required:
                raise ValueError(
                    f"sequential step '{step.id}' requires deferred tool(s): "
                    f"{', '.join(deferred_required)}"
                )
            if self.playbooks and (step.tools is not None or step.require_tool_calls):
                raise ValueError(
                    "structured step tool constraints cannot be combined with playbooks; "
                    f"step '{step.id}' must omit tools and require_tool_calls"
                )
        return self

    @model_validator(mode="after")
    def _check_playbooks(self) -> HarnessSpec:
        seen: set[str] = set()
        entries: list[str] = []
        for playbook in self.playbooks:
            if playbook.name in seen:
                raise ValueError(f"duplicate playbook name '{playbook.name}'")
            seen.add(playbook.name)
            if playbook.entry:
                entries.append(playbook.name)
        if len(entries) > 1:
            raise ValueError(
                "at most one playbook may set entry: true "
                f"(got {', '.join(entries)})"
            )

        # A tool subset naming a tool the harness does not have would silently
        # deactivate everything on entry and strand the model with no way back.
        # Catch the typo at validation time instead.
        #
        # MCP tools are exempt: they are discovered from a live server when the
        # registry is built, so their names cannot be known here. Validating
        # them would mean either refusing valid specs or requiring every
        # declared server to be reachable just to parse the YAML.
        available = self.tool_names() | {"switch_playbook", "search_tools"}
        for playbook in self.playbooks:
            declared = {t for t in (playbook.tools or []) if not t.startswith("mcp__")}
            unknown = sorted(declared - available)
            if unknown:
                raise ValueError(
                    f"playbook '{playbook.name}' lists unknown tool(s): "
                    f"{', '.join(unknown)}. Declared tools: "
                    f"{', '.join(sorted(available)) or '(none)'}"
                )
        return self
