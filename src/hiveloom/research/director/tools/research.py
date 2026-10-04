"""The director's typed tools: thin calls into the engine's DirectorSession.

The session is caller-owned context injected by the research engine for one
phase of one round; it is never journalled. Every tool validates against the
charter and the program's state and returns either its result or
``{"refused": reason}`` — a refusal the director reads and adapts to, never an
exception that ends its run.
"""


from __future__ import annotations

from typing import Any, NotRequired

from typing_extensions import TypedDict

from hiveloom.tools import ToolResult, tool
from hiveloom.tools.registry import ToolError


def _call(run_context: dict[str, Any] | None, name: str, **arguments: Any) -> ToolResult:
    context = (run_context or {}).get("context")
    session = context.get("research") if isinstance(context, dict) else None
    if session is None:
        raise ToolError("this run was not launched by a hiveloom research program")
    return ToolResult(content=session.call(name, arguments))


@tool(
    description=(
        "The program at a glance: goal, levers, guard, budget pools, the power plan, the "
        "incumbent's spec, its signal map (signals, failure features, counted mechanisms, "
        "loss attribution, valid targets), evidence (failing runs behind each top signal: "
        "task, output, the first error), every hypothesis and verdict so far, calibration "
        "and findings. Call this first in every phase."
    ),
    tags=["read"],
)
def brief(run_context: dict[str, Any] | None = None) -> ToolResult:
    return _call(run_context, "brief")


@tool(
    description=(
        "Working-split runs of the incumbent with their task, output and features. Filter "
        "by status (e.g. verify_failed) or by a feature name from the signal map."
    ),
    tags=["read"],
)
def runs(
    status: str = "", feature: str = "", limit: int = 10,
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    return _call(run_context, "runs", status=status, feature=feature, limit=limit)


@tool(
    description="The tool calls, results, verifier feedback and model text of one working run.",
    tags=["read"],
)
def excerpt(
    run_id: str, max_events: int = 40, run_context: dict[str, Any] | None = None
) -> ToolResult:
    return _call(run_context, "excerpt", run_id=run_id, max_events=max_events)


@tool(description="The changes that made a candidate from its parent.", tags=["read"])
def diff(candidate: str, run_context: dict[str, Any] | None = None) -> ToolResult:
    return _call(run_context, "diff", candidate=candidate)


@tool(
    description=(
        "Register a falsifiable hypothesis before testing it. levers: the spec paths it "
        "would change (within the charter). target: a target from brief().signal.targets, "
        "or success_rate. expect: increase|decrease. by: the predicted change (optional). "
        "prior: your confidence 0..1. falsifier: what result would prove it wrong."
    ),
    tags=["write"],
)
def register_hypothesis(
    claim: str,
    levers: list[str],
    target: str,
    expect: str,
    falsifier: str,
    prior: float = 0.5,
    by: float | None = None,
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    return _call(
        run_context, "register_hypothesis", claim=claim, levers=levers, target=target,
        expect=expect, falsifier=falsifier, prior=prior, by=by,
    )


class Change(TypedDict):
    """One spec change: the complete new value for one dotted path."""

    path: str
    value: Any


@tool(
    description=(
        "Design one experiment for a registered hypothesis: a list of changes "
        "[{path, value}] applied to a copy of the incumbent. Dotted paths, e.g. "
        "system_prompt or loop.max_turns; a list item by index, e.g. tools.0.description. "
        "The engine runs and judges it; you do not."
    ),
    tags=["write"],
)
def design_experiment(
    hypothesis_id: str,
    changes: list[Change],
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    return _call(run_context, "design_experiment", hypothesis_id=hypothesis_id,
                 changes=[dict(change) for change in changes])


@tool(
    description=(
        "Move budget between the exploration and experiments pools (at most 25% of the "
        "total per pool; the confirmation pool is reserved)."
    ),
    tags=["write"],
)
def move_budget(
    source: str, target: str, usd: float, run_context: dict[str, Any] | None = None
) -> ToolResult:
    return _call(run_context, "move_budget", source=source, target=target, usd=usd)


@tool(
    description=(
        "Close the round: findings (what the verdicts taught, ≤8), next_focus (≤3), and a "
        "decision: continue, pivot, or stop (with stop_reason). Call exactly once."
    ),
    tags=["write"],
)
def interpret(
    findings: list[str],
    next_focus: list[str],
    decision: str,
    stop_reason: str = "",
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    return _call(
        run_context, "interpret", findings=findings, next_focus=next_focus,
        decision=decision, stop_reason=stop_reason or None,
    )


@tool(description="Record a short note in the program's ledger.", tags=["write"])
def note(text: str, run_context: dict[str, Any] | None = None) -> ToolResult:
    return _call(run_context, "note", text=text)


class CheckSpec(TypedDict):
    """How a criterion is checked."""

    kind: str
    field: NotRequired[str]
    pattern: NotRequired[str]
    rubric: NotRequired[str]


class CriterionSpec(TypedDict):
    """One thing the user cares about, and how it is checked."""

    id: str
    says: str
    check: CheckSpec


class CaseSpec(TypedDict):
    """One working case: a realistic request, and what its checks expect."""

    input: str
    expected: NotRequired[dict[str, Any]]
    criterion: NotRequired[str]


@tool(
    description=(
        "Frame phase: draft the evaluation contract. criteria: [{id, says, check}] where "
        "check.kind is contains | not_contains | exact | json_field (with field) | "
        "json_present (with field) | regex (with pattern) | judge (with rubric). "
        "goal_thresholds: {criterion id: pass rate that counts as done}. Proposing again "
        "replaces the draft. The user approves it before anything is spent."
    ),
    tags=["write"],
)
def propose_contract(
    criteria: list[CriterionSpec],
    goal_thresholds: dict[str, float] | None = None,
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    return _call(run_context, "propose_contract", criteria=[dict(c) for c in criteria],
                 goal_thresholds=goal_thresholds or {})


@tool(
    description=(
        "Frame phase: add working cases for the drafted contract. Each case is a realistic "
        "request in the format the harness receives, the criterion it was written for, and "
        "expected: {criterion id: value} for every contains / not_contains / exact / "
        "json_field criterion it should be checked against."
    ),
    tags=["write"],
)
def add_cases(cases: list[CaseSpec], run_context: dict[str, Any] | None = None) -> ToolResult:
    return _call(run_context, "add_cases", cases=[dict(c) for c in cases])


@tool(
    description=(
        "Ask the user one short question about what a concept means (kind disambiguate) or "
        "whether a boundary holds (kind confirm), optionally with options to pick from. "
        "Answers arrive later, in brief(); keep working meanwhile."
    ),
    tags=["write"],
)
def ask(
    text: str,
    kind: str = "disambiguate",
    options: list[str] | None = None,
    criterion: str | None = None,
    run_context: dict[str, Any] | None = None,
) -> ToolResult:
    return _call(run_context, "ask", text=text, kind=kind, options=options or [],
                 criterion=criterion)
