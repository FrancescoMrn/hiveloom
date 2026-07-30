"""Declarative catalog of builtin tools, guardrails, validators, and policies.

This module is the single source of truth for what builtins exist and what
spec-time parameters they accept. It is consumed by:

* ``hiveloom catalog`` (lists these entries),
* the spec schema (validates ``builtin:`` references against these entries),
* the generator's ``{builtin_catalog}`` prompt placeholder (M4).

Runtime *implementations* (added in M2 for guardrails/verify/tools) register
against the names defined here, so the metadata never drifts from behaviour.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ParamSpec(BaseModel):
    """A single spec-time parameter accepted by a builtin."""

    name: str
    type: str = Field(description="One of: float, int, str, bool, list, dict.")
    required: bool = False
    default: Any = None
    description: str = ""


class CatalogEntry(BaseModel):
    """Metadata describing one catalog tool/guardrail/validator/policy.

    ``source`` records where the entry came from: ``builtin`` for entries
    defined in this module, or the registering extension's source label
    (e.g. ``pkg:hiveloom-web``) for entries added via :mod:`hiveloom.ext`.
    """

    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    params: list[ParamSpec] = Field(default_factory=list)
    source: str = "builtin"
    singleton: bool = Field(
        default=False,
        description=(
            "Only one entry of this name is meaningful in a spec, so ``add`` replaces "
            "an existing one instead of appending (e.g. a second, weaker max_cost_usd "
            "is redundant). Entries that compose as a list — like regex_output_filter, "
            "one per pattern — leave this False."
        ),
    )


# Python types accepted for each declared param ``type``.
_PY_TYPES: dict[str, tuple[type, ...]] = {
    "float": (int, float),
    "int": (int,),
    "str": (str,),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
}


def _entries(*items: CatalogEntry) -> dict[str, CatalogEntry]:
    return {e.name: e for e in items}


BUILTIN_TOOLS: dict[str, CatalogEntry] = _entries(
    CatalogEntry(
        name="file_read",
        description="Read a UTF-8 text file from within the harness working directory.",
        tags=["read", "file"],
    ),
    CatalogEntry(
        name="file_write",
        description="Write a UTF-8 text file within the harness working directory.",
        tags=["write", "file"],
    ),
    CatalogEntry(
        name="shell",
        description=(
            "Run an allowlisted shell command. Disabled unless an allowlist is "
            "provided; never enabled by default (safety invariant)."
        ),
        tags=["exec", "dangerous"],
        params=[
            ParamSpec(
                name="commands",
                type="list",
                required=False,
                default=[],
                description=(
                    "Strict commands as strings, or mappings such as "
                    "{argv: [echo], allow_extra_args: true}. Strings match exact argv; "
                    "arbitrary extra args are limited to a small safe command set."
                ),
            )
        ],
    ),
    CatalogEntry(
        name="http_get",
        description="Perform an HTTP GET request and return the response body.",
        tags=["network", "read"],
    ),
)


BUILTIN_GUARDRAILS: dict[str, CatalogEntry] = _entries(
    CatalogEntry(
        name="max_cost_usd",
        description="Halt the run once accumulated model cost exceeds this many USD.",
        tags=["budget"],
        singleton=True,
        params=[
            ParamSpec(
                name="value",
                type="float",
                required=True,
                default=1.00,
                description="Cost ceiling in USD.",
            )
        ],
    ),
    CatalogEntry(
        name="max_wall_clock_seconds",
        description="Halt the run once it has run longer than this many seconds.",
        tags=["budget", "time"],
        singleton=True,
        params=[
            ParamSpec(name="value", type="int", required=True, default=300,
                      description="Wall-clock ceiling in seconds."),
        ],
    ),
    CatalogEntry(
        name="max_turns_hard_cap",
        description="Hard cap on loop turns, independent of loop.max_turns.",
        tags=["budget"],
        singleton=True,
        params=[
            ParamSpec(name="value", type="int", required=True, default=50,
                      description="Absolute maximum number of loop turns."),
        ],
    ),
    CatalogEntry(
        name="tool_allowlist",
        description="Block any tool call whose name is not a registered tool.",
        tags=["safety"],
        singleton=True,
    ),
    CatalogEntry(
        name="no_network_write",
        description="Block tools tagged both 'network' and 'write'.",
        tags=["safety", "network"],
        singleton=True,
    ),
    CatalogEntry(
        name="regex_output_filter",
        description="Block final output that matches the given regex (e.g. secrets).",
        tags=["safety", "output"],
        params=[
            ParamSpec(name="pattern", type="str", required=True,
                      description="Regex; a match blocks the output."),
        ],
    ),
)


BUILTIN_VALIDATORS: dict[str, CatalogEntry] = _entries(
    CatalogEntry(
        name="output_schema",
        description="Validate the run output against a JSON schema file.",
        tags=["schema"],
        params=[
            ParamSpec(name="schema_file", type="str", required=True,
                      description="Path (relative to the harness dir) of a JSON schema."),
        ],
    ),
    CatalogEntry(
        name="regex_match",
        description="Pass only if the run output matches the given regex.",
        tags=["regex"],
        params=[
            ParamSpec(name="pattern", type="str", required=True,
                      description="Regex the output must match."),
        ],
    ),
    CatalogEntry(
        name="file_exists",
        description="Pass only if the given file exists after the run.",
        tags=["file"],
        params=[
            ParamSpec(name="path", type="str", required=True,
                      description="Path (relative to the harness dir) that must exist."),
        ],
    ),
    CatalogEntry(
        name="command_succeeds",
        description="Run a shell command; pass if it exits 0 (e.g. a test suite).",
        tags=["exec"],
        params=[
            ParamSpec(name="command", type="str", required=True,
                      description="Command to execute; exit code 0 means pass."),
        ],
    ),
)


POLICIES: dict[str, CatalogEntry] = _entries(
    CatalogEntry(
        name="react",
        description="Think -> tool call(s) -> observe -> repeat.",
        tags=["loop"],
    ),
    CatalogEntry(
        name="plan_then_act",
        description="One planning turn produces a pinned step list, then react over it.",
        tags=["loop"],
    ),
    CatalogEntry(
        name="sequential_steps",
        description="Walk a fixed, ordered list of objectives (loop.steps), refusing "
                    "completion until each is done in order.",
        tags=["loop"],
    ),
)


BUILTIN_COMPACTION: dict[str, CatalogEntry] = _entries(
    CatalogEntry(
        name="summarize",
        description="Compress older turns into a model-written summary message.",
        tags=["context"],
    ),
    CatalogEntry(
        name="truncate_oldest",
        description="Drop the oldest messages (the first user message stays pinned).",
        tags=["context"],
    ),
)


# Event handlers a spec can attach via its ``hooks:`` section. Most hooks come
# from extensions; these small, deterministic normalizers cover common model
# transport quirks without requiring each harness to ship executable code.
BUILTIN_HOOKS: dict[str, CatalogEntry] = _entries(
    CatalogEntry(
        name="strip_json_fence",
        description=(
            "Before verification, unwrap a final JSON object enclosed only in a "
            "Markdown ```json (or ```) fence."
        ),
        tags=["output", "normalization", "json"],
    ),
)


CATALOGS: dict[str, dict[str, CatalogEntry]] = {
    "tools": BUILTIN_TOOLS,
    "guardrails": BUILTIN_GUARDRAILS,
    "validators": BUILTIN_VALIDATORS,
    "policies": POLICIES,
    "compaction": BUILTIN_COMPACTION,
    "hooks": BUILTIN_HOOKS,
}


def validate_builtin_params(entry: CatalogEntry, provided: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems with ``provided`` params.

    An empty list means the parameters are valid for ``entry``.
    """

    problems: list[str] = []
    allowed = {p.name: p for p in entry.params}

    for key in provided:
        if key not in allowed:
            allowed_names = ", ".join(sorted(allowed)) or "none"
            problems.append(
                f"'{entry.name}' has no parameter '{key}' (allowed: {allowed_names})"
            )

    for param in entry.params:
        if param.name not in provided:
            if param.required:
                problems.append(f"'{entry.name}' requires parameter '{param.name}'")
            continue
        value = provided[param.name]
        expected = _PY_TYPES.get(param.type)
        # bool is a subclass of int; guard against silently accepting True as int.
        if expected and (
            not isinstance(value, expected)
            or (param.type != "bool" and isinstance(value, bool))
        ):
            problems.append(
                f"'{entry.name}' parameter '{param.name}' must be {param.type}, "
                f"got {type(value).__name__}"
            )

    return problems
