"""Assemble and run a harness — the library behind ``hiveloom run``.

Loads the spec, resolves hooks, builds the runtime components (provider, tool
registry, guardrails, verifiers, context manager, trace writer), and drives the
agent loop. Also supports ``--dry-run``: assemble the first model call without
any API use.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from hiveloom import trust
from hiveloom.context.manager import ContextManager
from hiveloom.events import build_event_bus
from hiveloom.guardrails.builtin import build_guardrails
from hiveloom.logging.trace import TraceWriter, spec_version_hash
from hiveloom.loop.agent_loop import AgentLoop, RunResult
from hiveloom.models.provider import ModelProvider
from hiveloom.skills import load_skills
from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks
from hiveloom.tools.registry import build_registry
from hiveloom.verify.builtin import build_verifiers

if TYPE_CHECKING:
    from hiveloom.generate.llm import StrongModel
    from hiveloom.spec.schema import HarnessSpec

# Keep diagnostics off machine-readable command stdout when structlog has not
# been configured by an embedding application.
log = structlog.wrap_logger(logging.getLogger(__name__))


def _resolve_input(base: Path, value: str) -> str:
    """If ``value`` names an existing file, read it; otherwise treat it as text."""
    direct = Path(value)
    if direct.is_file():
        return direct.read_text(encoding="utf-8")
    nested = base / value
    if nested.is_file():
        return nested.read_text(encoding="utf-8")
    return value


def _resolve_trace_dir(base: Path, trace_dir: str) -> Path:
    path = Path(trace_dir)
    if path.is_absolute():
        return path
    return (base / trace_dir).resolve()


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


def dry_run(
    harness_dir: str | Path, input_value: str, *, approve_trust=None
) -> dict[str, Any]:
    """Assemble the would-be first model call without any API use."""
    yaml_path = harness_path(harness_dir)
    base = yaml_path.parent
    trust.ensure_trusted(base, approve_trust)
    spec = load_spec(yaml_path)
    resolve_hooks(spec, base)
    registry = build_registry(spec, base)
    run_input = _resolve_input(base, input_value)

    from hiveloom.models.provider import _estimate_messages_tokens

    system = spec.system_prompt
    messages = [{"role": "user", "content": run_input}]
    return {
        "name": spec.name,
        "model": spec.model.id,
        "system": system,
        "messages": messages,
        "tools": registry.anthropic_payload(),
        "estimated_input_tokens": _estimate_messages_tokens(system, messages),
    }


def run_harness(
    harness_dir: str | Path,
    input_value: str,
    *,
    provider: ModelProvider | None = None,
    ingest: bool = True,
    hive_path: str | Path | None = None,
    on_event=None,
    approve_trust=None,
    strong_model: StrongModel | None = None,
) -> RunResult:
    """Run a harness end to end and return the :class:`RunResult`.

    Unless ``ingest`` is false, the completed run's trace is ingested into the
    Hive so ``hiveloom trace``/``stats`` see it immediately, and (if the spec
    opts in via ``evolution.auto_propose``) a failing run may auto-draft a
    gated evolution proposal — see :func:`_maybe_auto_propose`. ``on_event``
    receives every :class:`TraceEvent` as it is emitted (the in-process
    equivalent of ``hiveloom run --stream``). ``approve_trust`` is asked once
    when the harness folder is not yet trusted on this machine. ``strong_model``
    is a test seam: when given, auto-propose uses it instead of resolving one
    (so tests never need ``ANTHROPIC_API_KEY``).
    """
    yaml_path = harness_path(harness_dir)
    base = yaml_path.parent
    trust.ensure_trusted(base, approve_trust)
    spec = load_spec(yaml_path)
    resolve_hooks(spec, base)

    run_input = _resolve_input(base, input_value)
    registry = build_registry(spec, base)
    guardrails = build_guardrails(spec, registry, base)
    verifiers = build_verifiers(spec, base)
    skills = load_skills(spec, base)

    if provider is None:
        provider = _default_provider(base, spec.model.provider)

    version_hash = spec_version_hash(spec, base)
    run_id = _new_run_id()
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
    context = ContextManager(
        spec, provider, trace, events=events, registry=registry, skills=skills
    )
    loop = AgentLoop(
        spec=spec,
        base_dir=base,
        provider=provider,
        registry=registry,
        guardrails=guardrails,
        verifiers=verifiers,
        context=context,
        trace=trace,
        run_input=run_input,
        run_id=run_id,
        events=events,
    )
    result = loop.run()

    if ingest:
        _ingest_trace(trace.path, hive_path)
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
            since = last_auto_proposal_at(hive, spec.name)
            if hive.failure_count(spec.name, since=since) < auto.min_failures:
                return
            if since is not None:
                elapsed_hours = (
                    datetime.now(UTC) - datetime.fromisoformat(since)
                ).total_seconds() / 3600
                if elapsed_hours < auto.cooldown_hours:
                    return

            model = strong_model or build_strong_model(auto.model, base)
            report = analyze(hive, spec.name)
            create_proposal(hive, spec, base, report, model, trigger="auto")
    except Exception as exc:  # noqa: BLE001 - see docstring: never fail a completed run
        log.warning(
            "auto_propose_failed",
            harness_name=spec.name,
            error=str(exc),
            error_type=type(exc).__name__,
        )


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
