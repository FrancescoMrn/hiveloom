"""Tool registry: wraps builtins and code hooks into a uniform dispatch surface.

Each tool carries a name, description, tags, and a JSON-schema input (derived
from type hints for code hooks). The registry produces the Anthropic ``tools``
payload and dispatches normalized :class:`ToolCall`s to implementations.

Tools may be registered *inactive* (a spec entry with ``deferred: true``):
they stay out of the model's tool payload until activated. When any tool is
deferred, :func:`build_registry` auto-adds a ``search_tools`` tool the model
can call to find and activate them — keeping the always-paid payload small
for harnesses with many tools.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import BaseModel, Field, create_model, field_validator

from hiveloom.errors import HiveloomError
from hiveloom.models.provider import ToolCall
from hiveloom.package import trace_dir_relative_to
from hiveloom.spec.loader import import_hook
from hiveloom.spec.schema import BuiltinToolRef, CodeToolRef, HarnessSpec


class ToolError(HiveloomError):
    """Raised by a tool when it cannot complete a call."""


# A code tool that declares this parameter is handed the run context instead of
# having the model supply it. Same name the validator contract already uses
# (``validate(run_output, run_context)``), so one word means one thing.
RUN_CONTEXT_PARAM = "run_context"


class Artifact(BaseModel):
    """A structured side-product of a tool call, for the embedding application.

    ``content`` is what the *model* reads back; an artifact is what the
    *caller* renders or stores. A charting tool returns "chart registered" to
    the model and the chart spec as an artifact; a proposal tool returns
    "awaiting confirmation" and the proposal rows as an artifact. Keeping the
    two apart means a harness can drive a real UI without smuggling JSON
    through the model's text channel.

    ``kind`` groups artifacts for the caller (``"chart"``, ``"proposal"``);
    ``data`` is arbitrary JSON-serializable payload.
    """

    kind: str
    data: Any

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact kind must not be empty")
        return value


class ToolResult(BaseModel):
    """The outcome of dispatching a tool call.

    ``terminate`` is a hint that this result is the final output and the loop
    may skip the follow-up model call — honored only when every result in the
    turn's batch terminates (mirrors pi's semantics).

    ``artifacts`` carries structured output to the embedding caller; see
    :class:`Artifact`. They are recorded on the trace and collected onto
    ``RunResult.artifacts`` in dispatch order.
    """

    content: str
    is_error: bool = False
    terminate: bool = False
    artifacts: list[Artifact] = Field(default_factory=list)
    # Set False on an error that will deterministically recur, so
    # `loop.on_tool_error: retry_once` does not repeat it. A refused playbook
    # switch is a policy decision, not a flaky call: retrying re-runs the gate
    # hook's side effects and double-counts the refusal in run evidence.
    retryable: bool = True


class Tool(ABC):
    """A callable tool with a JSON-schema input and tags.

    Optional surface (all have safe defaults):

    * ``guidelines`` — usage guidance appended to the system prompt while the
      tool is active;
    * :meth:`prepare` — normalize/repair arguments before execution (cheap
      executors mangle args; pi calls this ``prepareArguments``);
    * ``supports_updates`` + :meth:`run_with_updates` — stream progress from
      long-running tools as ``tool_update`` trace events.
    """

    name: str
    description: str
    tags: list[str]
    input_schema: dict[str, Any]
    guidelines: str = ""
    supports_updates: bool = False
    # Set by tools that declare a ``run_context`` parameter; the registry then
    # injects the run context at dispatch (see :data:`RUN_CONTEXT_PARAM`).
    wants_run_context: bool = False

    @abstractmethod
    def run(self, **kwargs: Any) -> str | ToolResult:
        """Execute the tool; return a string or a full :class:`ToolResult`."""

    def prepare(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Normalize raw call arguments before execution (default: unchanged)."""
        return kwargs

    def run_with_updates(
        self, kwargs: dict[str, Any], on_update: Callable[[str], None]
    ) -> str | ToolResult:
        """Streaming variant; override when ``supports_updates`` is true."""
        return self.run(**kwargs)


class FunctionTool(Tool):
    """Wraps a ``@tool``-decorated code hook."""

    def __init__(self, func, name: str, description: str, tags: list[str],
                 guidelines: str = ""):
        self._func = func
        self.name = name
        self.description = description
        self.tags = tags
        self.guidelines = guidelines
        self.input_schema = schema_from_function(func)
        self.wants_run_context = RUN_CONTEXT_PARAM in inspect.signature(func).parameters

    def run(self, **kwargs: Any) -> str | ToolResult:
        result = self._func(**kwargs)
        if isinstance(result, ToolResult):
            return result
        return result if isinstance(result, str) else str(result)


def schema_from_function(func) -> dict[str, Any]:
    """Derive an Anthropic-style JSON input schema from a function's signature.

    ``run_context`` is skipped: it is injected by the runtime, so exposing it
    to the model would both waste tokens and invite the model to forge
    per-run dependencies.
    """
    signature = inspect.signature(func)
    # Code hooks commonly opt into postponed annotations via
    # ``from __future__ import annotations``.  ``inspect.signature`` then
    # exposes strings such as ``list[ChartSeries]``; handing those strings to
    # pydantic leaves the generated model unresolved and prevents structured
    # item schemas (TypedDict/BaseModel) from reaching the executor model.
    # Resolve in the hook's own globals first, while retaining the historical
    # fallback for unusual callables whose hints cannot be evaluated.
    try:
        resolved_hints = get_type_hints(func, include_extras=True)
    except (NameError, TypeError):
        resolved_hints = {}
    fields: dict[str, Any] = {}
    for pname, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if pname == RUN_CONTEXT_PARAM:
            continue
        annotation = resolved_hints.get(
            pname,
            param.annotation if param.annotation is not inspect.Parameter.empty else str,
        )
        if param.default is inspect.Parameter.empty:
            fields[pname] = (annotation, ...)
        else:
            fields[pname] = (annotation, param.default)
    if not fields:
        return {"type": "object", "properties": {}}
    model: type[BaseModel] = create_model("ToolInput", **fields)  # type: ignore[call-overload]
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


class ToolRegistry:
    """Holds tools (active and deferred) and dispatches calls."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._active: set[str] = set()
        self._closers: list[Callable[[], None]] = []

    def add_closer(self, fn: Callable[[], None]) -> None:
        """Register generic resource teardown (not MCP-specific), run by :meth:`close`."""
        self._closers.append(fn)

    def close(self) -> None:
        """Run registered closers in reverse order, swallowing exceptions.

        Never masks a run's real outcome.
        """
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass

    def register(self, tool: Tool, *, active: bool = True) -> None:
        self._tools[tool.name] = tool
        if active:
            self._active.add(tool.name)
        else:
            self._active.discard(tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        """All registered tool names, including deferred ones."""
        return list(self._tools)

    def active_names(self) -> list[str]:
        return [name for name in self._tools if name in self._active]

    def inactive_names(self) -> list[str]:
        return [name for name in self._tools if name not in self._active]

    def activate(self, names: list[str]) -> list[str]:
        """Activate registered tools by name; returns the names that matched."""
        activated = [n for n in names if n in self._tools and n not in self._active]
        self._active.update(activated)
        return activated

    def set_active(self, names: list[str]) -> None:
        """Replace the active set outright (used by playbook tool subsets).

        Unlike :meth:`activate` this also *deactivates*: entering a mode that
        may only read must actually remove the write tools, or the narrowing
        is decorative.
        """
        self._active = {n for n in names if n in self._tools}

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def anthropic_payload(self) -> list[dict[str, Any]]:
        """Build the ``tools`` payload for the model API (active tools only)."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
            if tool.name in self._active
        ]

    def guidelines(self) -> list[str]:
        """Usage guidelines of active tools, for the system prompt."""
        return [
            tool.guidelines
            for tool in self._tools.values()
            if tool.name in self._active and tool.guidelines
        ]

    def dispatch(
        self,
        call: ToolCall,
        on_update: Callable[[str], None] | None = None,
        run_context: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Run a tool call, converting failures into error results (never crash)."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(content=f"unknown tool '{call.name}'", is_error=True)
        if call.name not in self._active:
            return ToolResult(content=f"tool '{call.name}' is inactive", is_error=True)
        try:
            kwargs = tool.prepare(dict(call.input))
            if tool.wants_run_context:
                # Injected after prepare() so a model-supplied key of the same
                # name can never reach the tool in its place.
                kwargs[RUN_CONTEXT_PARAM] = dict(run_context or {})
            if on_update is not None and tool.supports_updates:
                result = tool.run_with_updates(kwargs, on_update)
            else:
                result = tool.run(**kwargs)
            if isinstance(result, ToolResult):
                return result
            return ToolResult(content=result)
        except ToolError as exc:
            return ToolResult(content=f"tool error: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - tools must never crash the loop
            return ToolResult(content=f"tool raised {type(exc).__name__}: {exc}", is_error=True)


class SwitchPlaybookTool(Tool):
    """Switches the run's active playbook (auto-added when a spec declares any).

    Registered by :func:`build_registry` so it appears in ``run --dry-run``
    like any other tool, then bound by the agent loop, which owns what a
    switch actually does (gates, tracing, prompt swap).
    """

    def __init__(self, playbooks: list[tuple[str, str]]):
        self._names = [name for name, _ in playbooks]
        self._handler: Callable[[str, str], ToolResult] | None = None
        self.name = "switch_playbook"
        listing = "; ".join(f"{name}: {desc}" for name, desc in playbooks)
        self.description = (
            "Switch to another playbook when the work moves into its area. "
            "The active tools and guidance change with it. Available — "
            f"{listing}"
        )
        self.tags = ["meta", "playbook"]
        self.input_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Playbook to switch to.",
                    "enum": self._names,
                },
                "reason": {
                    "type": "string",
                    "description": "One line on why this mode fits what comes next.",
                },
            },
            "required": ["name"],
        }

    def bind(self, handler: Callable[[str, str], ToolResult]) -> None:
        self._handler = handler

    def run(self, name: str = "", reason: str = "", **_: Any) -> ToolResult:
        if self._handler is None:
            raise ToolError("switch_playbook is not available in this context")
        return self._handler(name, reason)


class SearchToolsTool(Tool):
    """Finds and activates deferred tools (auto-added when a spec defers any)."""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry
        self.name = "search_tools"
        self.description = (
            "Search the harness's deferred tools by keyword and activate the "
            "matches so they become callable. Use when no active tool fits."
        )
        self.tags = ["meta"]
        self.input_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to match against tool names/descriptions/tags.",
                }
            },
            "required": ["query"],
        }

    def run(self, query: str = "", **_: Any) -> str:
        words = [w for w in query.lower().split() if w]
        matches: list[Tool] = []
        for name in self._registry.inactive_names():
            tool = self._registry.get(name)
            haystack = " ".join([tool.name, tool.description, " ".join(tool.tags)]).lower()
            if not words or any(w in haystack for w in words):
                matches.append(tool)
        if not matches:
            available = ", ".join(self._registry.inactive_names()) or "none"
            return f"no deferred tools matched '{query}' (still inactive: {available})"
        self._registry.activate([t.name for t in matches])
        lines = [f"activated {len(matches)} tool(s):"]
        lines += [f"- {t.name}: {t.description}" for t in matches]
        return "\n".join(lines)


def build_registry(spec: HarnessSpec, base_dir: str | Path) -> ToolRegistry:
    """Instantiate catalog tools and import code-hook tools from a spec."""
    from hiveloom.tools import builtin  # local import to avoid cycles

    base = Path(base_dir)
    if base.is_file():
        base = base.parent
    # So file_read/file_write can refuse the configured trace directory too,
    # not just the .hiveloom/.env* coverage they get regardless — the same
    # protection the HTTP control plane's input_file and the evolver's
    # code-change containment get when they have a spec loaded.
    trace_dir = trace_dir_relative_to(base, spec.logging.trace_dir)

    registry = ToolRegistry()
    has_deferred = False
    for tool_ref in spec.tools:
        active = not bool(tool_ref.deferred)
        has_deferred = has_deferred or not active
        if isinstance(tool_ref, BuiltinToolRef):
            tool = builtin.make_builtin_tool(
                tool_ref, base, trace_dir=trace_dir, skills=spec.skills
            )
            registry.register(tool, active=active)
        elif isinstance(tool_ref, CodeToolRef):
            func = import_hook(tool_ref.code, base)
            meta = getattr(func, "__hiveloom_tool__", {})
            registry.register(
                FunctionTool(
                    func,
                    name=func.__name__,
                    description=tool_ref.description or meta.get("description", ""),
                    tags=list(meta.get("tags", [])),
                    guidelines=meta.get("guidelines", ""),
                ),
                active=active,
            )

    if spec.mcp_servers:
        from hiveloom.tools.mcp import McpBridge, connect_mcp_server  # local import to avoid cycles

        bridge = McpBridge()
        registry.add_closer(bridge.close)
        try:
            for server_ref in spec.mcp_servers:
                active = not server_ref.deferred
                has_deferred = has_deferred or not active
                for adapter in connect_mcp_server(server_ref, base, bridge):
                    registry.register(adapter, active=active)
        except Exception:
            # A later server failing to connect must not leak an earlier
            # server's already-open session/subprocess or portal thread.
            registry.close()
            raise

    if has_deferred:
        registry.register(SearchToolsTool(registry))
    if spec.playbooks:
        registry.register(
            SwitchPlaybookTool([(p.name, p.description) for p in spec.playbooks])
        )
    return registry
