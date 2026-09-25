"""Built-in delegation: a phone line for every harness.

A harness's **model** is the user's choice and is frozen (``ALWAYS_FROZEN``).
**Delegation** is the other axis, and it is dynamic: while a run is happening,
the harness can look for a peer harness on this machine that is more specific
to the task at hand, or has better *measured* odds on the Hive, hand the task
to it, and verify the answer with its own validators. When nothing qualifies
automatically, the peers it found come back as *referrals* so the user is told
who would have fitted.

Three parts, in the order the runtime uses them:

* **Directory** (:func:`discover_candidates`) — peers from the local registry
  (:mod:`hiveloom.registry`), each with its Hive fitness. Broken entries,
  untrusted folders, the harness itself, and ``delegation.exclude`` names never
  appear. Trust is checked *before* a peer's spec is loaded, because loading a
  spec imports its declared extensions.
* **Selection** (:func:`select_peer`) — one constrained call to the parent's
  own configured provider and model. Delegation never introduces a second
  model: the point is to change *which harness* runs the task, not which model
  the user picked.
* **Delegate** (:func:`delegate`) — an in-process :func:`hiveloom.runner.run_harness`
  for the peer, with a lineage record, a per-run cost cap carved out of the
  parent's remaining budget, and depth/cycle refusals applied first.

The lineage dict written into the child's ``run_started`` is a shared contract
with the MCP request-meta form of the same hand-off; its keys are fixed:
``{"kind": "delegation", "parent_run_id", "parent_harness_id", "depth",
"chain"}`` where ``chain`` lists harness ids root → parent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from hiveloom import registry as registry_mod
from hiveloom import trust
from hiveloom.spec.loader import harness_path, load_spec
from hiveloom.tools.registry import Tool, ToolError, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.models.provider import ModelConfig, ModelProvider
    from hiveloom.spec.schema import DelegationConfig, HarnessSpec

#: Prefix of the per-peer tool registered in ``model_choice`` mode.
TOOL_PREFIX = "delegate__"
#: The always-registered peer listing tool (cheap; enables referrals).
LIST_PEERS_TOOL = "list_peers"

# Why a hand-off did not happen. These strings are the trace vocabulary of
# `delegation_skipped` and the `reason` of a referral, so they are part of the
# public surface: keep them stable.
NO_CANDIDATES = "no_candidates"
BELOW_FITNESS = "below_fitness"
NONE_FIT = "none_fit"
UNPARSABLE = "unparsable"
DEPTH = "depth"
CYCLE = "cycle"
BUDGET = "budget"
LISTED = "listed"


class PeerCandidate(BaseModel):
    """One harness this run could hand its task to, with measured fitness."""

    name: str
    harness_id: str
    description: str = ""
    path: str
    total_runs: int = 0
    success_rate: float = 0.0
    avg_cost_usd: float = 0.0

    @property
    def tool_name(self) -> str:
        return TOOL_PREFIX + sanitize(self.name)

    def fitness_line(self) -> str:
        """A one-line description + fitness, for a prompt or a tool description."""
        if self.total_runs:
            measured = (
                f"{self.total_runs} run(s), {self.success_rate:.0%} success, "
                f"${self.avg_cost_usd:.4f} avg cost"
            )
        else:
            measured = "no measured runs yet"
        description = self.description.strip() or "(no description)"
        return f"{self.name}: {description} [{measured}]"

    def referral(self, reason: str) -> dict[str, Any]:
        """The public referral record: who would fit, and why they were not used."""
        return {
            "harness": self.name,
            "description": self.description,
            "success_rate": round(self.success_rate, 3),
            "total_runs": self.total_runs,
            "reason": reason,
        }


class DelegationRecord(BaseModel):
    """What one completed hand-off produced, as reported on ``RunResult``."""

    harness: str
    run_id: str = ""
    status: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    output: str = ""
    reason: str = ""


class Selection(BaseModel):
    """The outcome of one selection call: a peer, or why there is none."""

    candidate: PeerCandidate | None = None
    reason: str = ""
    considered: list[PeerCandidate] = Field(default_factory=list)
    cost_usd: float = 0.0


def sanitize(name: str) -> str:
    """Map a harness name onto the tool-name charset (``[a-zA-Z0-9_-]``)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return cleaned or "harness"


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #
def discover_candidates(
    spec: HarnessSpec,
    base: str | Path,
    *,
    hive_path: str | Path | None = None,
) -> list[PeerCandidate]:
    """Peers this harness may consider, newest fitness attached.

    Skips, in order and silently: the harness itself (by resolved folder and by
    harness identity), untrusted folders, folders whose spec no longer loads,
    excluded names, and duplicates.
    """
    from hiveloom.logging.hive import Hive

    exclude = {name.strip() for name in spec.delegation.exclude if name.strip()}
    own_dir = Path(base).resolve()
    seen: set[str] = {spec.identity}
    peers: list[tuple[str, Any]] = []
    for entry in registry_mod.registered_paths():
        directory = Path(entry)
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved == own_dir:
            continue
        # Trust first: load_spec imports the harness's declared extensions, so
        # a registered-but-untrusted folder must not be parsed at all.
        if not trust.is_trusted(resolved):
            continue
        try:
            peer = load_spec(harness_path(resolved))
        except Exception:  # noqa: BLE001 - a broken entry must not hide the rest
            continue
        if peer.identity in seen or peer.name in exclude:
            continue
        seen.add(peer.identity)
        peers.append((str(resolved), peer))

    candidates: list[PeerCandidate] = []
    if not peers:
        return candidates
    with Hive(hive_path) as hive:
        for path, peer in peers:
            stats = hive.summary(peer.identity, display_name=peer.name)
            candidates.append(
                PeerCandidate(
                    name=peer.name,
                    harness_id=peer.identity,
                    description=peer.description,
                    path=path,
                    total_runs=stats["total_runs"],
                    success_rate=stats["success_rate"],
                    avg_cost_usd=stats["avg_cost_usd"],
                )
            )
    return candidates


def eligible(
    candidates: Sequence[PeerCandidate],
    config: DelegationConfig,
) -> list[PeerCandidate]:
    """The candidates a run may choose *automatically*.

    ``min_peer_runs`` is a floor on measurement, not on quality: a peer with
    fewer runs is unmeasured, and an unmeasured peer can never satisfy a
    positive ``min_peer_success_rate``. Everything filtered out here is still
    referable — the user can be told about a promising peer the runtime is not
    willing to spend on unattended.
    """
    keep: list[PeerCandidate] = []
    for candidate in candidates:
        if candidate.total_runs < config.min_peer_runs:
            continue
        if config.min_peer_success_rate > 0.0 and (
            candidate.total_runs == 0
            or candidate.success_rate < config.min_peer_success_rate
        ):
            continue
        keep.append(candidate)
    return keep


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
_SELECT_SYSTEM = (
    "You are routing one task to the harness best suited to it. A harness is a "
    "confined agent that does one job and verifies its own output. Answer with "
    "EXACTLY one line: the name of the single best harness, or the word none. "
    "No punctuation, no explanation, no code fences. Answer none unless a "
    "harness clearly covers this task; a general task is not a reason to pick "
    "a specialist."
)


def selection_prompt(task: str, candidates: Sequence[PeerCandidate]) -> str:
    lines = ["Task:", task.strip()[:4000], "", "Available harnesses:"]
    lines += [f"- {candidate.fitness_line()}" for candidate in candidates]
    lines.append("")
    lines.append("Reply with one harness name from the list above, or none.")
    return "\n".join(lines)


def parse_selection(text: str, candidates: Sequence[PeerCandidate]) -> tuple[
    PeerCandidate | None, str
]:
    """Strictly map a model reply onto one candidate, ``none``, or a failure."""
    answer = (text or "").strip().strip("`").strip()
    # A one-line answer is what was asked for; take the first non-empty line
    # so a chatty model's trailing prose cannot smuggle in a second choice.
    answer = next((line.strip() for line in answer.splitlines() if line.strip()), "")
    answer = answer.strip().strip("\"'").rstrip(".").strip()
    if not answer:
        return None, UNPARSABLE
    lowered = answer.lower()
    if lowered in ("none", "no", "nothing"):
        return None, NONE_FIT
    for candidate in candidates:
        if candidate.name.lower() == lowered:
            return candidate, ""
    return None, UNPARSABLE


def select_peer(
    provider: ModelProvider,
    config: ModelConfig,
    task: str,
    candidates: Sequence[PeerCandidate],
    delegation: DelegationConfig,
    *,
    screen: Callable[..., tuple[str, list[Any], list[Any]]] | None = None,
) -> Selection:
    """Ask the parent's own model to pick one peer, or none.

    Costs at most one model call, and none at all when nothing is eligible —
    the common case for a machine with one harness registered. ``screen`` is
    the run's egress filter: this request carries the task statement, so it
    leaves the machine through the same check every other request does.
    """
    allowed = eligible(candidates, delegation)
    if not allowed:
        return Selection(
            candidate=None,
            reason=BELOW_FITNESS if candidates else NO_CANDIDATES,
            considered=list(candidates),
        )
    system = _SELECT_SYSTEM
    messages: list[Any] = [
        {"role": "user", "content": selection_prompt(task, allowed)}
    ]
    tools: list[Any] = []
    if screen is not None:
        system, messages, tools = screen(system, messages, tools)
    response = provider.complete(
        system=system,
        messages=messages,
        tools=tools,
        config=config,
    )
    estimated = provider.estimated_cost(response.usage, config.id, config.provider)
    cost, _source = response.resolved_cost_usd(estimated)
    candidate, reason = parse_selection(response.text, allowed)
    return Selection(
        candidate=candidate,
        reason=reason,
        considered=list(candidates),
        cost_usd=cost,
    )


# --------------------------------------------------------------------------- #
# Delegate
# --------------------------------------------------------------------------- #
def build_lineage(
    *,
    parent_run_id: str,
    parent_harness_id: str,
    depth: int,
    chain: Sequence[str],
) -> dict[str, Any]:
    """The provenance record a delegated child run is started with.

    Shared contract with the MCP hand-off, which sends the same dict as request
    meta — the key names are the interface, so they are built in one place.
    ``depth`` is the child's depth (the root run is 0) and ``chain`` lists
    harness ids root → parent, the child excluded.
    """
    return {
        "kind": "delegation",
        "parent_run_id": parent_run_id,
        "parent_harness_id": parent_harness_id,
        "depth": depth,
        "chain": list(chain),
    }


def refusal(
    candidate: PeerCandidate,
    *,
    child_depth: int,
    chain: Sequence[str],
    max_depth: int,
) -> str | None:
    """Why this hand-off must not happen at all, or ``None`` to proceed.

    Checked before the peer is contacted, never after: a refused delegation
    costs nothing. ``chain`` is the ancestry root → parent, so a peer already
    in it would close a loop that each hop pays for.
    """
    if child_depth > max_depth:
        return DEPTH
    if candidate.harness_id in set(chain):
        return CYCLE
    return None


def child_cost_cap(remaining_budget: float, budget_share: float) -> float:
    """The cap installed on a child run: a share of what the parent has left."""
    return max(remaining_budget, 0.0) * budget_share


def delegate(
    candidate: PeerCandidate,
    task: str,
    *,
    lineage: dict[str, Any],
    cost_cap_usd: float | None = None,
    hive_path: str | Path | None = None,
    on_event=None,
) -> DelegationRecord:
    """Run the peer harness in this process and record what it produced.

    The child builds its own provider from its own spec: the parent's provider
    instance is deliberately not reused, because a peer is a different harness
    with its own (frozen) model. Its cost is charged back to the parent by the
    caller — the parent's ``max_cost_usd`` is the user's total budget.
    """
    from hiveloom import runner

    result = runner.run_harness(
        candidate.path,
        task,
        literal_input=True,
        lineage=lineage,
        cost_cap_usd=cost_cap_usd,
        hive_path=hive_path,
        on_event=on_event,
    )
    return DelegationRecord(
        harness=candidate.name,
        run_id=result.run_id,
        status=result.status,
        cost_usd=result.cost_usd,
        turns=result.turns,
        output=result.output,
        reason=result.reason,
    )


# --------------------------------------------------------------------------- #
# Model-choice tools
# --------------------------------------------------------------------------- #
class DelegateTool(Tool):
    """One deferred tool per eligible peer: hand this task over to it.

    Registered inactive so the always-paid tool payload does not grow with the
    machine's harness count; the auto-added ``search_tools`` finds them. The
    loop binds the handler, because what a hand-off actually does — depth and
    cycle refusals, the child's cost cap, lineage, tracing — belongs to the run,
    not to the tool object.
    """

    def __init__(self, candidate: PeerCandidate):
        self.candidate = candidate
        self.name = candidate.tool_name
        self.description = (
            f"Delegate this task to the '{candidate.name}' harness — "
            f"{candidate.fitness_line()}. It runs with its own tools, budget and "
            "validators, and returns its final output."
        )
        self.tags = ["delegation"]
        self.input_schema = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The complete task statement to hand over.",
                }
            },
            "required": ["task"],
        }
        self._handler: Any = None

    def bind(self, handler) -> None:
        self._handler = handler

    def run(self, task: str = "", **_: Any) -> ToolResult:
        if self._handler is None:
            raise ToolError("delegation is not available in this context")
        return self._handler(self.candidate, task)


class ListPeersTool(Tool):
    """The referral tool: who else could take this, and how well they do it."""

    def __init__(self, candidates: Sequence[PeerCandidate]):
        self._candidates = list(candidates)
        self.name = LIST_PEERS_TOOL
        self.description = (
            "List the peer harnesses available on this machine with their "
            "measured fitness (runs, success rate, average cost). Use it to "
            "tell the user which harness fits their task better than you do."
        )
        self.tags = ["delegation", "meta"]
        self.input_schema = {"type": "object", "properties": {}}
        self._handler: Any = None

    def bind(self, handler) -> None:
        self._handler = handler

    def run(self, **_: Any) -> ToolResult:
        if self._handler is not None:
            return self._handler(self._candidates)
        return ToolResult(content=render_peers(self._candidates))


def render_peers(candidates: Sequence[PeerCandidate]) -> str:
    """The text form of the peer list handed back to the model."""
    if not candidates:
        return "no peer harnesses are registered on this machine"
    lines = [f"{len(candidates)} peer harness(es):"]
    lines += [f"- {candidate.fitness_line()}" for candidate in candidates]
    return "\n".join(lines)
