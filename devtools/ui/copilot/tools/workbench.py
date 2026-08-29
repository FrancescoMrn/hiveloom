"""Constrained workbench operations for the Hiveloom copilot.

The service object is caller-owned context injected by the local workbench. It
is deliberately not global and is never written to the journal. This harness
therefore remains an ordinary Hiveloom harness while each run is scoped to the
catalog, selection, and creation root of the workbench request that launched
it.
"""

from __future__ import annotations

from typing import Any

from hiveloom.tools import Artifact, ToolResult, tool
from hiveloom.tools.registry import ToolError


def _service(run_context: dict[str, Any]):
    context = run_context.get("context")
    service = context.get("workbench") if isinstance(context, dict) else None
    if service is None:
        raise ToolError("this run was not launched by a Hiveloom workbench")
    return service


def _result(kind: str, data: dict[str, Any], summary: str) -> ToolResult:
    return ToolResult(content=summary, artifacts=[Artifact(kind=kind, data=data)])


@tool(
    description="Read the harness and run currently attached to this conversation.",
    tags=["read", "context"],
)
def workspace_context(run_context: dict[str, Any]) -> ToolResult:
    data = _service(run_context).selection()
    return _result("workspace_context", data, "Read the current workbench selection.")


@tool(
    description=(
        "Recall explicit durable memories from earlier conversations. Returns global "
        "memories plus memories for the selected harness; query may narrow by text."
    ),
    tags=["read", "memory"],
)
def recall_memories(
    query: str = "", run_context: dict[str, Any] | None = None
) -> ToolResult:
    data = _service(run_context or {}).recall_memories(query)
    return _result("memories", data, f"Recalled {data['count']} memories.")


@tool(
    description=(
        "Remember one concise durable fact for future conversations. Use only when the "
        "user asks to remember it or clearly states a stable preference/convention. "
        "scope is global or harness; harness requires a selected harness."
    ),
    tags=["write", "memory"],
)
def remember_memory(
    content: str,
    scope: str = "global",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).remember_memory(content, scope)
    return _result("memory_saved", data, "Saved one durable memory.")


@tool(
    description="Forget one durable memory by its id when the user asks to remove it.",
    tags=["write", "memory"],
)
def forget_memory(
    memory_id: str, run_context: dict[str, Any] | None = None
) -> ToolResult:
    data = _service(run_context or {}).forget_memory(memory_id)
    return _result("memory_forgotten", data, f"Forgot memory {memory_id}.")


@tool(
    description="List every harness available in the workbench with status and aggregate fitness.",
    tags=["read", "discovery"],
)
def list_harnesses(run_context: dict[str, Any]) -> ToolResult:
    data = {"harnesses": _service(run_context).list_harnesses()}
    return _result("harness_catalog", data, f"Found {len(data['harnesses'])} harnesses.")


@tool(
    description=(
        "Inspect one harness's task contract, version, tools, verification, "
        "and safety posture."
    ),
    tags=["read", "harness"],
)
def inspect_harness(harness_id: str = "", run_context: dict[str, Any] | None = None) -> ToolResult:
    data = _service(run_context or {}).inspect_harness(harness_id)
    return _result("harness_contract", data, f"Inspected harness {data['name']!r}.")


@tool(
    description=(
        "Read a UTF-8 text file attached to a target harness. Use the relative path "
        "named in the user's message; protected state and paths outside the harness "
        "remain inaccessible."
    ),
    tags=["read", "attachment"],
)
def read_harness_file(
    path: str,
    harness_id: str = "",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).read_harness_file(path, harness_id)
    return _result(
        "harness_file",
        data,
        f"Read {data['path']!r} from harness {data['harness_name']!r}.",
    )


@tool(
    description=(
        "Create a complete minimal harness through validated construction operations. "
        "Use builtin tool names from the live catalog. output_schema_json may be an empty "
        "string or a JSON Schema object encoded as JSON."
    ),
    tags=["write", "harness"],
)
def create_harness(
    name: str,
    task: str,
    system_prompt: str,
    builtin_tools: list[str] | None = None,
    output_schema_json: str = "",
    max_turns: int = 8,
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).create_harness(
        name=name,
        task=task,
        system_prompt=system_prompt,
        builtin_tools=builtin_tools or [],
        output_schema_json=output_schema_json,
        max_turns=max_turns,
    )
    return _result(
        "harness_created",
        data,
        f"Created and validated harness {data['name']!r} at version {data['version_hash'][:8]}.",
    )


@tool(
    description="Validate a harness specification and all of its code hooks.",
    tags=["read", "verify"],
)
def validate_harness(harness_id: str = "", run_context: dict[str, Any] | None = None) -> ToolResult:
    data = _service(run_context or {}).validate_harness(harness_id)
    return _result("validation", data, f"Harness {data['name']!r} is valid.")


@tool(
    description="Assemble a harness's first model request without calling its model provider.",
    tags=["read", "test"],
)
def dry_run_harness(
    sample_input: str,
    harness_id: str = "",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).dry_run(harness_id, sample_input)
    return _result("dry_run", data, f"Dry-run assembled for {data['name']!r}; no model was called.")


@tool(
    description=(
        "Execute a target harness on a literal input. This creates a separately journalled "
        "target run and returns its output, verifier verdicts, cost, and run id."
    ),
    tags=["execute", "test"],
)
def run_target(
    input: str,
    harness_id: str = "",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).run_target(
        harness_id, input, copilot_run_id=str((run_context or {}).get("run_id") or "")
    )
    return _result(
        "target_run",
        data,
        f"Target run {data['run_id']} finished with status {data['status']}.",
    )


@tool(
    description=(
        "List recent runs for a harness with task, status, version, cost, and verifier results."
    ),
    tags=["read", "discovery"],
)
def list_runs(
    harness_id: str = "",
    limit: int = 10,
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).list_runs(harness_id, limit)
    return _result(
        "recent_runs",
        data,
        f"Found {data['count']} recent runs for {data['harness_name']!r}.",
    )


@tool(
    description=(
        "Inspect one recorded run's input, result, verifier failures, "
        "guardrails, model path, and key events."
    ),
    tags=["read", "diagnose"],
)
def inspect_run(run_id: str = "", run_context: dict[str, Any] | None = None) -> ToolResult:
    data = _service(run_context or {}).inspect_run(run_id)
    return _result("run_evidence", data, f"Inspected run {data['run']['run_id']}.")


@tool(
    description=(
        "Read aggregate success, cost, turn, version, and recurring-failure "
        "statistics for a harness."
    ),
    tags=["read", "measure"],
)
def harness_stats(harness_id: str = "", run_context: dict[str, Any] | None = None) -> ToolResult:
    data = _service(run_context or {}).stats(harness_id)
    return _result(
        "harness_stats",
        data,
        f"Read statistics for {data['harness_name']!r} across {data['total_runs']} runs.",
    )


@tool(
    description=(
        "Compare two recorded versions of one harness, including success, "
        "cost, and failure movement."
    ),
    tags=["read", "measure"],
)
def compare_versions(
    left_version: str,
    right_version: str,
    harness_id: str = "",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).compare(harness_id, left_version, right_version)
    return _result("version_comparison", data, "Compared the two recorded harness versions.")


@tool(
    description=(
        "Draft and safety-gate an improvement proposal from recorded failures. "
        "This never applies the proposal."
    ),
    tags=["write", "proposal"],
)
def propose_improvement(
    harness_id: str = "",
    model: str = "",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).propose(harness_id, model or None)
    return _result(
        "improvement_proposal",
        data,
        data.get("summary", "Drafted an improvement proposal."),
    )


@tool(
    description=(
        "Create or replace a standalone one-page HTML interface for a harness and return a "
        "sandbox-preview artifact. input_kind may be auto, url, text, or file."
    ),
    tags=["write", "interface"],
)
def create_interface(
    harness_id: str = "",
    title: str = "",
    input_label: str = "",
    submit_label: str = "Run",
    input_kind: str = "auto",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    data = _service(run_context or {}).create_interface(
        harness_id,
        title=title,
        input_label=input_label,
        submit_label=submit_label,
        input_kind=input_kind,
    )
    return _result(
        "interface",
        data,
        f"Created a standalone interface for {data['harness_name']!r}.",
    )
