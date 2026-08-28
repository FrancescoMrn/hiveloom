"""Assemble and run a harness — the library behind ``hiveloom run``.

Loads the spec, resolves hooks, builds the runtime components (provider, tool
registry, guardrails, verifiers, context manager, trace writer), and drives the
agent loop. Also supports ``--dry-run``: assemble the first model call without
calling the model API. Declared MCP servers are still contacted because tool
discovery is eager.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveloom import trust
from hiveloom.context.manager import ContextManager
from hiveloom.events import build_event_bus
from hiveloom.guardrails.builtin import build_guardrails
from hiveloom.logging.trace import TraceWriter, spec_version_hash
from hiveloom.loop.agent_loop import AgentLoop, RunResult
from hiveloom.loop.control import RunControl
from hiveloom.models.provider import ModelProvider
from hiveloom.playbooks import PlaybookManager, load_playbooks
from hiveloom.skills import load_skills
from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks
from hiveloom.tools.registry import build_registry
from hiveloom.verify.builtin import build_verifiers

if TYPE_CHECKING:
    from hiveloom.generate.llm import StrongModel
    from hiveloom.spec.schema import HarnessSpec

log = logging.getLogger(__name__)


def _resolve_input(base: Path, value: str) -> str:
    """If ``value`` names an existing file, read it; otherwise treat it as text."""
    direct = Path(value)
    if direct.is_file():
        return direct.read_text(encoding="utf-8")
    nested = base / value
    if nested.is_file():
        return nested.read_text(encoding="utf-8")
    return value


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


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


def dry_run(
    harness_dir: str | Path,
    input_value: str | None = None,
    *,
    conversation: list[dict[str, Any]] | None = None,
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
    resolve_hooks(spec, base)
    registry = build_registry(spec, base)
    try:
        history, run_input = _resolve_conversation(
            base, input_value, conversation, literal_input=False
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
        system = context_manager.system()
        messages = [*history, {"role": "user", "content": run_input}]
        return {
            "name": spec.name,
            "model": spec.model.id,
            "system": system,
            "messages": messages,
            "tools": registry.anthropic_payload(),
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
    resolve_hooks(spec, base)

    history, run_input = _resolve_conversation(
        base, input_value, conversation, literal_input=literal_input
    )
    registry = build_registry(spec, base)
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

        version_hash = spec_version_hash(spec, base)
        run_id = run_id or _new_run_id()
        trace = TraceWriter(
            _resolve_trace_dir(base, spec.logging.trace_dir),
            run_id=run_id,
            harness_name=spec.name,
            version_hash=version_hash,
            redact_patterns=spec.logging.redact,
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
        )
        result = loop.run()
    finally:
        registry.close()

    if ingest:
        _ingest_trace(trace.path, hive_path)
        # Auto-propose needs this run ingested before it can count the failure.
        _maybe_auto_propose(spec, base, result, hive_path, strong_model=strong_model)
    return result


def _ingest_trace(trace_path: Path, hive_path: str | Path | None) -> None:
    """Best-effort ingest of a finished run's trace into the Hive."""
    from hiveloom.logging.hive import Hive

    try:
        with Hive(hive_path) as hive:
            hive.ingest_trace_file(trace_path)
    except Exception:  # noqa: BLE001 - ingestion must never fail a completed run
        pass


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
            since = last_auto_proposal_at(hive, spec.name)
            if hive.failure_count(spec.name, since=since, version=version) < auto.min_failures:
                return
            if since is not None:
                elapsed_hours = (
                    datetime.now(UTC) - datetime.fromisoformat(since)
                ).total_seconds() / 3600
                if elapsed_hours < auto.cooldown_hours:
                    return

            model = strong_model or build_strong_model(auto.model, base)
            report = analyze(hive, spec.name, version=version)
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
    ``guardrail_halt``, ``max_turns``, ``stopped``, and ``error`` are all completed runs
    reported here, not raised exceptions, so both callers can never diverge
    on what a finished run looks like.
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
    }


def resolve_and_ingest(target: str | Path, hive) -> str:
    """Resolve a harness name-or-dir to a harness name, ingesting its traces.

    If ``target`` is a harness directory (or ``harness.yaml`` path), its
    in-folder traces are ingested into ``hive`` and the spec's name returned.
    Otherwise ``target`` is treated as a harness name (nothing to ingest).
    """
    path = Path(target)
    yaml_path = path / "harness.yaml" if path.is_dir() else path
    if yaml_path.name == "harness.yaml" and yaml_path.exists():
        # load_spec imports declared extensions, so the trust decision must
        # happen before parsing a foreign harness rather than only before run.
        trust.ensure_trusted(yaml_path.parent)
        spec = load_spec(yaml_path)
        hive.ingest_dir(_resolve_trace_dir(yaml_path.parent, spec.logging.trace_dir))
        return spec.name
    return str(target)


def _default_provider(base: Path, provider_name: str) -> ModelProvider:
    """Construct the spec's model provider from the provider registry."""
    from hiveloom import ext

    return ext.build_provider(provider_name, base)
