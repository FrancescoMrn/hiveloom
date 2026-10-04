"""Research-safe execution: what a research run may do with the harness's tools.

A research program runs the executor many times on inputs nobody typed for
real. A harness with real tools would do real things that many times — write,
call, spend. So every research run goes through a policy that can only
*tighten* it, never widen it:

* ``allow``   — the tool runs as the harness declares it;
* ``sandbox`` — the tool's effects stay inside the candidate's own copy of the
  harness folder (file and notes writes; ``shell`` only when the harness's
  confinement already has no network);
* ``replay``  — the tool serves results recorded from the base harness's own
  real runs, keyed by its input; a miss is a tool error, never a live call;
* ``deny``    — the tool is removed from the run.

Builtin tools carry effect tags, so read-only ones are allowed and file writes
sandboxed by default. Network, shell, code and MCP tools carry no trustworthy
defaults: the charter must classify them, and a program with an unclassified
one does not start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hiveloom.spec.schema import BuiltinToolRef, CodeToolRef, HarnessSpec
from hiveloom.tools.registry import Tool, ToolError, ToolRegistry, ToolResult

from .charter import Execution

#: Builtins whose effects stay within the run's own folder or the review queue
#: (and eval runs never queue): sandboxed by default.
_SANDBOXABLE = {"file_write", "notes", "propose_memory"}
#: Effect tags that make a tool unsafe to allow without the charter saying so.
_EFFECT_TAGS = {"write", "network", "exec", "dangerous"}


@dataclass
class ToolClass:
    name: str
    kind: str  # builtin | code | mcp
    tags: list[str]
    default: str | None  # None: the charter must decide


def classify(spec: HarnessSpec) -> list[ToolClass]:
    from hiveloom import catalog

    tags_of = {name: list(entry.tags) for name, entry in catalog.CATALOGS["tools"].items()}
    classes: list[ToolClass] = []
    for ref in spec.tools:
        if isinstance(ref, BuiltinToolRef):
            tags = tags_of.get(ref.builtin, [])
            if ref.builtin in _SANDBOXABLE:
                default = "sandbox"
            elif not (set(tags) & _EFFECT_TAGS):
                default = "allow"
            else:
                default = None
            classes.append(ToolClass(ref.builtin, "builtin", tags, default))
        elif isinstance(ref, CodeToolRef):
            classes.append(ToolClass(ref.code.rsplit(":", 1)[-1], "code", [], None))
    for server in spec.mcp_servers:
        classes.append(ToolClass(server.name, "mcp", [], None))
    return classes


@dataclass
class ResolvedPolicy:
    tools: dict[str, str] = field(default_factory=dict)
    mcp: dict[str, dict[str, str]] = field(default_factory=dict)  # server -> tool|* -> mode
    errors: list[str] = field(default_factory=list)

    def mode_for(self, tool_name: str) -> str:
        if tool_name.startswith("mcp__"):
            _, server, tool = (tool_name.split("__", 2) + ["", ""])[:3]
            modes = self.mcp.get(server, {})
            return modes.get(tool, modes.get("*", "allow"))
        return self.tools.get(tool_name, "allow")

    def replayed(self) -> set[str]:
        return {name for name, mode in self.tools.items() if mode == "replay"}


def resolve(spec: HarnessSpec, execution: Execution) -> ResolvedPolicy:
    """The policy for this harness under this charter, with every problem listed."""
    policy = ResolvedPolicy()
    classes = classify(spec)
    known_tools = {c.name for c in classes if c.kind != "mcp"}
    known_servers = {c.name for c in classes if c.kind == "mcp"}
    for name in execution.tools:
        if name not in known_tools:
            policy.errors.append(f"execution.tools.{name}: the harness declares no such tool")
    for server in execution.mcp:
        if server not in known_servers:
            policy.errors.append(f"execution.mcp.{server}: the harness declares no such server")
    for entry in classes:
        if entry.kind == "mcp":
            declared = execution.mcp.get(entry.name)
            if declared is None:
                policy.errors.append(
                    f"MCP server '{entry.name}' is unclassified: set execution.mcp."
                    f"{entry.name} to allow, replay or deny (or per tool)"
                )
                continue
            modes = declared if isinstance(declared, dict) else {"*": declared}
            if "sandbox" in modes.values():
                policy.errors.append(
                    f"execution.mcp.{entry.name}: an MCP server cannot be sandboxed; "
                    "use replay or deny"
                )
            policy.mcp[entry.name] = dict(modes)
            continue
        mode = execution.tools.get(entry.name, entry.default)
        if mode is None:
            policy.errors.append(
                f"tool '{entry.name}' ({entry.kind}"
                + (f", tags {','.join(entry.tags)}" if entry.tags else "")
                + f") has effects the engine cannot bound: set execution.tools.{entry.name} "
                "to allow, replay or deny"
            )
            continue
        if mode == "sandbox" and entry.name not in _SANDBOXABLE:
            if entry.name == "shell" and not spec.confinement.network:
                pass  # confined shell without network: effects stay in the copy
            else:
                policy.errors.append(
                    f"execution.tools.{entry.name}: sandbox applies to file writes, notes, "
                    "and shell under a no-network confinement only"
                )
                continue
        policy.tools[entry.name] = mode
    return policy


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def _key(name: str, arguments: dict[str, Any]) -> str:
    return name + "\x00" + json.dumps(arguments, sort_keys=True, separators=(",", ":"),
                                      default=str)


class ReplayStore:
    """Recorded tool results, one JSON line each."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._index: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._index is None:
            self._index = {}
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        record = json.loads(line)
                        self._index[record["key"]] = record
        return self._index

    def lookup(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self._load().get(_key(name, arguments))

    def build(self, trace_paths: list[str], tools: set[str]) -> int:
        """Record every result of ``tools`` found in these journals; returns the count."""
        records: dict[str, dict[str, Any]] = {}
        for trace_path in trace_paths:
            calls: dict[str, dict[str, Any]] = {}
            try:
                lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "tool_call" and payload.get("name") in tools:
                    calls[str(payload.get("id"))] = payload
                elif event.get("type") == "tool_result" and str(payload.get("id")) in calls:
                    call = calls.pop(str(payload.get("id")))
                    key = _key(call["name"], call.get("input") or {})
                    records[key] = {
                        "key": key,
                        "name": call["name"],
                        "content": payload.get("content", ""),
                        "is_error": bool(payload.get("is_error")),
                    }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for record in records.values():
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._index = None
        return len(records)


class ReplayTool(Tool):
    """Stands in for a tool, serving recorded results and nothing else."""

    def __init__(self, original: Tool, store: ReplayStore):
        self._original = original
        self._store = store
        self.name = original.name
        self.description = original.description
        self.tags = [*original.tags, "replay"]
        self.input_schema = original.input_schema
        self.guidelines = original.guidelines

    def run(self, **kwargs: Any) -> ToolResult:
        record = self._store.lookup(self.name, kwargs)
        if record is None:
            raise ToolError(
                f"research replay has no recorded result for this {self.name} call; "
                "only inputs the base harness actually used can be replayed"
            )
        return ToolResult(content=str(record["content"]), is_error=record["is_error"])


class ResearchToolPolicy:
    """Applied to a run's registry right after it is built (see run_harness)."""

    def __init__(self, policy: ResolvedPolicy, store: ReplayStore | None = None):
        self._policy = policy
        self._store = store

    def apply(self, registry: ToolRegistry) -> None:
        active = set(registry.active_names())
        for name in list(registry.names()):
            mode = self._policy.mode_for(name)
            if mode == "deny":
                registry.unregister(name)
            elif mode == "replay":
                original = registry.get(name)
                if original is None or self._store is None:
                    registry.unregister(name)
                    continue
                registry.register(ReplayTool(original, self._store), active=name in active)
