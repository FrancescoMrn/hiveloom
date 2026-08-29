"""Assemble and run a harness — the library behind ``hiveloom run``.

Loads the spec, resolves hooks, builds the runtime components (provider, tool
registry, guardrails, verifiers, context manager, trace writer), and drives the
agent loop. Also supports ``--dry-run``: assemble the first model call without
calling the model API. Declared MCP servers are still contacted because tool
discovery is eager.
"""

from __future__ import annotations

import errno
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveloom import __version__, trust
from hiveloom.context.manager import ContextManager
from hiveloom.events import build_event_bus
from hiveloom.guardrails.builtin import build_guardrails
from hiveloom.logging.trace import TraceWriter, spec_version_hash
from hiveloom.loop.agent_loop import AgentLoop, RunResult
from hiveloom.loop.control import RunControl
from hiveloom.models.provider import ModelConfig as ProviderModelConfig
from hiveloom.models.provider import ModelProvider
from hiveloom.models.router import ModelRouter
from hiveloom.playbooks import PlaybookManager, load_playbooks
from hiveloom.skills import load_skills
from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks
from hiveloom.tools.registry import build_registry
from hiveloom.verify.builtin import build_verifiers

if TYPE_CHECKING:
    from hiveloom.generate.llm import StrongModel
from hiveloom.spec.schema import HarnessSpec, SequentialStep

log = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _resolve_input(base: Path, value: str) -> str:
    """If ``value`` names an existing file, read it; otherwise treat it as text."""
    direct = Path(value)
    if _is_file(direct):
        return direct.read_text(encoding="utf-8")
    nested = base / value
    if _is_file(nested):
        return nested.read_text(encoding="utf-8")
    return value


def _is_file(path: Path) -> bool:
    """Like :meth:`Path.is_file`, but an overlong literal is not a path."""
    try:
        return path.is_file()
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return False
        raise


def split_conversation(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Split a whole conversation into ``(history, task_statement)``.

    Multi-turn callers own the conversation and replay it every turn, so they
    pass the thread as it stands: alternating user/assistant turns ending with
    the user message to act on. That last message becomes the run's task
    statement (and its trace ``input``); everything before it is seeded as
    history.

    Roles must alternate — the major provider APIs reject consecutive
    same-role messages, and a caller finding that out as an opaque provider
    400 is far worse than finding it out here.
    """
    if not messages:
        raise ValueError("messages must not be empty")

    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role not in ("user", "assistant"):
            raise ValueError(
                f"messages[{index}].role must be 'user' or 'assistant' (got {role!r})"
            )
        if "content" not in message:
            raise ValueError(f"messages[{index}] has no 'content'")
        if normalized and normalized[-1]["role"] == role:
            raise ValueError(
                f"messages[{index}] repeats the '{role}' role; turns must alternate"
            )
        normalized.append({"role": role, "content": message["content"]})

    if normalized[-1]["role"] != "user":
        raise ValueError(
            "the last message must be from the user — it is the task statement"
        )
    task_statement = normalized[-1]["content"]
    if not isinstance(task_statement, str):
        raise ValueError("the last message's content must be a string")
    return normalized[:-1], task_statement


def _resolve_conversation(
    base: Path,
    input_value: str | None,
    messages: list[dict[str, Any]] | None,
    *,
    literal_input: bool,
) -> tuple[list[dict[str, Any]], str]:
    """Normalize the two input forms into ``(history, task_statement)``."""
    if (input_value is None) == (messages is None):
        raise ValueError("pass exactly one of 'input_value' or 'messages'")
    if messages is not None:
        # Conversation content is always literal: it is caller-authored chat
        # text, never a filename to resolve.
        return split_conversation(messages)
    return [], input_value if literal_input else _resolve_input(base, input_value)


def _resolve_trace_dir(base: Path, trace_dir: str) -> Path:
    path = Path(trace_dir)
    if path.is_absolute():
        return path
    return (base / trace_dir).resolve()


def new_run_id() -> str:
    """Allocate a run id ahead of the run itself.

    Public because pre-allocation is how a caller addresses a run *while it is
    still going*: announce the id, hand :func:`run_harness` the same id and a
    :class:`~hiveloom.loop.control.RunControl`, and stop/steer/model-switch
    calls have somewhere to land before the first model call returns.
    """
    return f"run_{uuid.uuid4().hex[:16]}"


def validate_run_id(value: str) -> str:
    """Validate a caller-allocated run id before it becomes a trace filename."""
    if not _RUN_ID_RE.fullmatch(value):
        raise ValueError(
            "run_id must be 1-128 characters: letters, numbers, '.', '_' or '-'; "
            "it must start with a letter or number"
        )
    return value


# Kept for callers written against the private name.
_new_run_id = new_run_id


def dry_run(
    harness_dir: str | Path,
    input_value: str | None = None,
    *,
    conversation: list[dict[str, Any]] | None = None,
    literal_input: bool = False,
    model_override: str | None = None,
    provider_override: str | None = None,
    approve_trust=None,
) -> dict[str, Any]:
    """Assemble the first model call without calling the model provider.

    Declared MCP servers are still contacted because tool discovery is eager.
    Pass either ``input_value`` (single-shot) or ``conversation`` (the whole
    multi-turn thread) — see :func:`run_harness`.
    """
    yaml_path = harness_path(harness_dir)
    base = yaml_path.parent
    trust.ensure_trusted(base, approve_trust)
    spec = load_spec(yaml_path)
    spec = _apply_runtime_model_overrides(spec, model_override, provider_override)
    runtime_config = _runtime_config(
        spec, model_override=model_override, provider_override=provider_override
    )
    resolve_hooks(spec, base)
    registry = build_registry(spec, base)
    try:
        history, run_input = _resolve_conversation(
            base, input_value, conversation, literal_input=literal_input
        )

        from hiveloom.models.provider import _estimate_messages_tokens

        # Assemble the system prompt the way a real run does, rather than
        # echoing spec.system_prompt: the skills index and active tools'
        # guidelines are part of what actually goes on the wire, so leaving
        # them out here would under-report both the prompt and the token
        # estimate this function exists to provide. No provider is needed —
        # ContextManager.system() does not call one.
        context_manager = ContextManager(
            spec, None, registry=registry, skills=load_skills(spec, base)
        )
        if spec.playbooks:
            # Show the run as it would start: in the entry playbook, with its
            # prompt fragment and its narrowed tool set.
            manager = PlaybookManager(load_playbooks(spec, base), registry)
            manager.enter_initial()
            context_manager.set_playbooks(manager)
        initial_tools = registry.active_names()
        effective_tools = list(initial_tools)
        step_plan = []
        for index, step in enumerate(spec.loop.steps):
            if isinstance(step, SequentialStep):
                if step.tools is not None:
                    effective_tools = [
                        name for name in step.tools if name in initial_tools
                    ]
                step_plan.append(
                    {
                        "id": step.id,
                        "index": index,
                        "instruction": step.instruction,
                        "tools": list(effective_tools),
                        "require_tool_calls": step.require_tool_calls,
                        "max_model_calls": step.max_model_calls,
                        "max_tool_calls": step.max_tool_calls,
                        "enforced": True,
                    }
                )
            else:
                step_plan.append(
                    {
                        "id": f"step-{index + 1}",
                        "index": index,
                        "instruction": step,
                        "tools": list(effective_tools),
                        "require_tool_calls": [],
                        "max_model_calls": None,
                        "max_tool_calls": None,
                        "enforced": False,
                    }
                )
        if step_plan:
            registry.set_active(step_plan[0]["tools"])
        system = context_manager.system()
        messages = [*history, {"role": "user", "content": run_input}]
        return {
            "name": spec.name,
            "model": spec.model.id,
            "provider": spec.model.provider,
            "runtime_config": runtime_config,
            "system": system,
            "messages": messages,
            "tools": registry.anthropic_payload(),
            "steps": step_plan,
            "estimated_input_tokens": _estimate_messages_tokens(system, messages),
        }
    finally:
        registry.close()


def run_harness(
    harness_dir: str | Path,
    input_value: str | None = None,
    *,
    conversation: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    provider: ModelProvider | None = None,
    ingest: bool = True,
    hive_path: str | Path | None = None,
    on_event=None,
    approve_trust=None,
    strong_model: StrongModel | None = None,
    literal_input: bool = False,
    control: RunControl | None = None,
    run_id: str | None = None,
    trace_dir: str | Path | None = None,
    model_override: str | None = None,
    provider_override: str | None = None,
    resume_messages: list[dict[str, Any]] | None = None,
    lineage: dict[str, Any] | None = None,
    providers: dict[str, ModelProvider] | None = None,
) -> RunResult:
    """Run a harness end to end and return the :class:`RunResult`.

    Pass exactly one input form. ``input_value`` is the single-shot task
    string. ``conversation`` is the whole multi-turn thread — alternating
    user/assistant turns ending with the user message to act on — for callers
    that own the conversation and replay it each turn (a chat service). The
    trailing user message becomes the task statement; the rest is seeded as
    history and is the first thing compaction reclaims. Conversation content is
    always literal, so ``literal_input`` applies only to ``input_value``.

    ``context`` carries per-run values the *caller* owns rather than the model:
    a database DSN, request-scoped state, a mutable accumulator. Code tools
    that declare a ``run_context`` parameter receive it (under the ``context``
    key, alongside ``input``/``harness_dir``/``run_id``), as do validators. The
    dict is passed by reference and never traced, so it is also the right place
    for values that must not be persisted.

    Unless ``ingest`` is false, the completed run's trace is ingested into the
    Hive so ``hiveloom trace``/``stats`` see it immediately, and (if the spec
    opts in via ``evolution.auto_propose``) a failing run may auto-draft a
    gated evolution proposal — see :func:`_maybe_auto_propose`. ``on_event``
    receives every :class:`TraceEvent` as it is emitted (the in-process
    equivalent of ``hiveloom run --stream``). ``approve_trust`` is asked once
    when the harness folder is not yet trusted on this machine. ``strong_model``
    is a test seam: when given, auto-propose uses it instead of resolving one
    (so tests never need ``ANTHROPIC_API_KEY``).

    ``control`` is an optional :class:`hiveloom.loop.control.RunControl`: a
    thread-safe channel the caller keeps to stop the run gracefully or inject
    steering messages, both consumed at the loop's next turn boundary.
    ``run_id`` lets the caller pre-allocate the id (so it can be announced to
    a client before the run finishes); by default one is generated.
    ``trace_dir`` selects a durable trace root for this run. ``model_override``
    and ``provider_override`` build a validated in-memory model config: they
    never write the harness, but they do participate in its runtime snapshot
    and version hash so evidence from different executors is not combined.
    ``providers`` pre-registers model provider instances by name, for the
    cross-provider case: a playbook or an operator may move the run onto a
    provider the spec never named, and the router would otherwise construct one
    from ambient credentials. Supplying it keeps an embedding caller (and the
    test suite) in control of what a swap actually talks to.

    ``resume_messages`` re-enters a run part-way through: the folded
    conversation from a parent run's journal is seeded verbatim and **no new
    task statement is appended**, because the seeded thread already ends where
    the parent was. This is what ``hiveloom run <fork> --resume`` passes; see
    :mod:`hiveloom.fork`. ``lineage`` is the accompanying provenance record
    (parent run id, journal seq) written into ``run_started``.

    ``literal_input`` skips the input-names-a-file convenience — see
    :func:`_resolve_input`. It is required when the input comes from an
    untrusted caller (``hiveloom serve`` and the HTTP control plane): over
    HTTP, treating any caller-supplied string that happens to name a file on
    the server as "read this file" would be an arbitrary file read, so those
    ``input`` fields are always literal text.
    """
    yaml_path = harness_path(harness_dir)
    base = yaml_path.parent
    trust.ensure_trusted(base, approve_trust)
    spec = load_spec(yaml_path)
    spec = _apply_runtime_model_overrides(spec, model_override, provider_override)
    runtime_config = _runtime_config(
        spec, model_override=model_override, provider_override=provider_override
    )
    run_id = validate_run_id(run_id) if run_id is not None else new_run_id()
    resolve_hooks(spec, base)

    if resume_messages is not None:
        if input_value is not None or conversation is not None:
            raise ValueError(
                "pass 'resume_messages' alone — a resumed run's conversation "
                "comes from the parent journal, not from a new input"
            )
        history = list(resume_messages)
        # The parent's task statement is already inside the folded thread; the
        # trace records what this run re-entered rather than a fresh input.
        run_input = (lineage or {}).get("parent_run_id", "")
    else:
        history, run_input = _resolve_conversation(
            base, input_value, conversation, literal_input=literal_input
        )
    registry = build_registry(spec, base)
    # Bound before the try: several steps below it can raise, and an unbound
    # name in the finally would mask the real error with a NameError.
    router: ModelRouter | None = None
    try:
        guardrails = build_guardrails(spec, registry, base)
        verifiers = build_verifiers(spec, base)
        skills = load_skills(spec, base)
        playbooks = (
            PlaybookManager(load_playbooks(spec, base), registry)
            if spec.playbooks
            else None
        )

        if provider is None:
            provider = _default_provider(base, spec.model.provider)

        router = ModelRouter.create(
            base,
            ProviderModelConfig(
                id=spec.model.id,
                max_tokens=spec.model.max_tokens,
                temperature=spec.model.temperature,
                provider=spec.model.provider,
            ),
            provider,
            providers=providers,
        )

        version_hash = spec_version_hash(spec, base)
        trace = TraceWriter(
            (
                Path(trace_dir).expanduser().resolve()
                if trace_dir is not None
                else _resolve_trace_dir(base, spec.logging.trace_dir)
            ),
            run_id=run_id,
            harness_name=spec.name,
            harness_id=spec.id,
            version_hash=version_hash,
            redact_patterns=spec.logging.redact.patterns,
            redact_keys=spec.logging.redact.keys,
            redact_paths=spec.logging.redact.paths,
            level=spec.logging.level,
            on_event=on_event,
        )
        events = build_event_bus(spec, base, trace)
        # Named context_manager, not context: the `context` parameter is the
        # caller's per-run values and must not be shadowed here.
        context_manager = ContextManager(
            spec, provider, trace, events=events, registry=registry, skills=skills
        )
        loop = AgentLoop(
            spec=spec,
            base_dir=base,
            provider=provider,
            registry=registry,
            guardrails=guardrails,
            verifiers=verifiers,
            context=context_manager,
            trace=trace,
            run_input=run_input,
            run_id=run_id,
            events=events,
            history=history,
            context_values=context,
            playbooks=playbooks,
            control=control,
            resume=resume_messages is not None,
            lineage=lineage,
            router=router,
            harness_version_hash=version_hash,
            runtime_version=__version__,
            runtime_config=runtime_config,
        )
        result = loop.run()
    finally:
        registry.close()
        if router is not None:
            router.close()

    if ingest:
        indexed = _ingest_trace(trace.path, hive_path)
        if indexed:
            # Auto-propose needs this run ingested before it can count the failure.
            _maybe_auto_propose(spec, base, result, hive_path, strong_model=strong_model)
            _apply_trace_retention(spec, trace.path, hive_path)
    return result


def _ingest_trace(trace_path: Path, hive_path: str | Path | None) -> bool:
    """Best-effort ingest of a finished run's trace into the Hive."""
    from hiveloom.logging.hive import Hive

    try:
        with Hive(hive_path) as hive:
            hive.ingest_trace_file(trace_path)
        return True
    except Exception:  # noqa: BLE001 - ingestion must never fail a completed run
        return False


def _apply_trace_retention(
    spec: HarnessSpec, trace_path: Path, hive_path: str | Path | None
) -> None:
    """Apply an explicit policy without invalidating the just-finished result."""
    if spec.logging.retention is None:
        return
    try:
        from hiveloom.logging.hive import Hive
        from hiveloom.logging.retention import prune_trace_root

        with Hive(hive_path) as hive:
            hive.ingest_dir(trace_path.parent)
            prune_trace_root(
                trace_path.parent,
                spec.logging.retention,
                hive=hive,
                preserve=[trace_path],
            )
    except Exception as exc:  # noqa: BLE001 - maintenance cannot erase a completed result
        log.warning(
            "trace retention failed for harness %s: %s: %s",
            spec.name,
            type(exc).__name__,
            exc,
        )


def _maybe_auto_propose(
    spec: HarnessSpec,
    base: Path,
    result: RunResult,
    hive_path: str | Path | None,
    *,
    strong_model: StrongModel | None = None,
) -> None:
    """Best-effort auto-draft (never auto-apply) of an evolution proposal.

    Opt-in via ``evolution.auto_propose.enabled``. Guards are ordered
    cheapest-first so the default (disabled) case costs nothing: no Hive
    query, no network, for virtually every harness and every run.

    No daemon/scheduler — this runs synchronously at the tail of a completed
    run, exactly like the trace-ingest step above, and shares its
    never-fail-a-completed-run discipline: this one doubly so, since it may
    touch the network and needs an API key the run's executor may not have.

    Trust: ``run_harness`` already called ``trust.ensure_trusted`` for this
    harness dir at its top (and ``proposals.create_proposal`` re-checks it
    internally regardless) — no additional trust check belongs here.
    """
    try:
        auto = spec.evolution.auto_propose
        if not auto.enabled:
            return
        if result.status == "success":
            return

        from hiveloom.evolve.analyzer import analyze
        from hiveloom.evolve.proposals import create_proposal, last_auto_proposal_at
        from hiveloom.generate.llm import build_strong_model
        from hiveloom.logging.hive import Hive

        with Hive(hive_path) as hive:
            # One version for both: the gate must count what the report carries.
            version = spec_version_hash(spec, base)
            since = last_auto_proposal_at(hive, spec.identity)
            if hive.failure_count(spec.identity, since=since, version=version) < auto.min_failures:
                return
            if since is not None:
                elapsed_hours = (
                    datetime.now(UTC) - datetime.fromisoformat(since)
                ).total_seconds() / 3600
                if elapsed_hours < auto.cooldown_hours:
                    return

            model = strong_model or build_strong_model(auto.model, base)
            report = analyze(
                hive,
                spec.identity,
                version=version,
                excerpt_config=spec.evolution.trace_excerpts,
                redaction=spec.logging.redact,
                objectives=spec.evolution.objectives,
            )
            # record_empty_as_rejected: even when the draft gates to nothing,
            # persist a terminal auto row so the cooldown timestamp advances —
            # otherwise every failing run past min_failures re-pays a
            # strong-model call with no throttle.
            create_proposal(
                hive, spec, base, report, model, trigger="auto", record_empty_as_rejected=True
            )
    except Exception as exc:  # noqa: BLE001 - see docstring: never fail a completed run
        log.warning(
            "auto-propose failed for harness %s: %s: %s",
            spec.name,
            type(exc).__name__,
            exc,
        )


def run_result_payload(result: RunResult) -> dict[str, Any]:
    """The JSON shape of a completed run, shared by the CLI and the HTTP control plane.

    ``ok`` reflects only ``status == "success"`` — ``verify_failed``,
    ``guardrail_halt``, ``step_failed``, ``max_turns``, ``stopped``, and ``error``
    are all completed runs reported here, not raised exceptions, so both
    callers can never diverge on what a finished run looks like.
    """
    return {
        "ok": result.status == "success",
        "status": result.status,
        "output": result.output,
        "turns": result.turns,
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "run_id": result.run_id,
        "trace_path": result.trace_path,
        "reason": result.reason,
        "artifacts": result.artifacts,
        "provider_calls": getattr(result, "provider_calls", []),
        "steps": [
            step.model_dump(mode="json")
            for step in getattr(result, "steps", [])
        ],
        # Structural fakes and 1.0-era embedding adapters may still return the
        # pre-override result shape. Keep that additive transition readable.
        "runtime_config": getattr(result, "runtime_config", {}),
        "execution": (
            result.execution.model_dump(mode="json")
            if getattr(result, "execution", None) is not None
            else None
        ),
    }


def _apply_runtime_model_overrides(
    spec: HarnessSpec,
    model_override: str | None,
    provider_override: str | None,
) -> HarnessSpec:
    """Return a validated in-memory spec with run-only model selection."""
    if model_override is None and provider_override is None:
        return spec

    from hiveloom.spec.schema import ModelConfig as SpecModelConfig

    model = SpecModelConfig(
        id=model_override or spec.model.id,
        provider=provider_override or spec.model.provider,
        max_tokens=spec.model.max_tokens,
        temperature=spec.model.temperature,
    )
    return spec.model_copy(update={"model": model})


def _runtime_config(
    spec: HarnessSpec,
    *,
    model_override: str | None,
    provider_override: str | None,
) -> dict[str, Any]:
    return {
        "requested": {"model": model_override, "provider": provider_override},
        "resolved": {"model": spec.model.id, "provider": spec.model.provider},
    }


def resolve_and_ingest(target: str | Path, hive) -> str:
    """Resolve a harness name-or-dir to its Hive key, ingesting its traces.

    If ``target`` is a harness directory (or ``harness.yaml`` path), its
    in-folder traces are ingested into ``hive`` and the spec's identity
    returned — its stable ``id``, or its name for a pre-1.0 spec without one.
    The Hive keys evidence on that identity, so two harnesses that merely
    share a name never read each other's stats. Otherwise ``target`` is
    treated as a bare key (nothing to ingest).
    """
    path = Path(target)
    yaml_path = path / "harness.yaml" if path.is_dir() else path
    if yaml_path.name == "harness.yaml" and yaml_path.exists():
        # load_spec imports declared extensions, so the trust decision must
        # happen before parsing a foreign harness rather than only before run.
        trust.ensure_trusted(yaml_path.parent)
        spec = load_spec(yaml_path)
        hive.ingest_dir(_resolve_trace_dir(yaml_path.parent, spec.logging.trace_dir))
        return spec.identity
    return str(target)


def _default_provider(base: Path, provider_name: str) -> ModelProvider:
    """Construct the spec's model provider from the provider registry."""
    from hiveloom import ext

    return ext.build_provider(provider_name, base)
