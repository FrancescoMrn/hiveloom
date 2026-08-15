"""The lifecycle event bus: one hook surface for the whole run.

The loop, context manager, and verify step emit named events; handlers come
from two places:

* the spec's ``hooks:`` section (``code:`` hooks or ``builtin:`` handlers
  registered by extensions) — per-harness, visible in the YAML, evolution-gated
  like any other spec path;
* ``ExtensionAPI.on(event)`` — ambient handlers an extension installs for every
  run in this process (e.g. an org-wide audit pack).

Events and their mutation semantics (a handler returns ``None`` to observe):

========================  ====================================================
``run_started``           observe: ``{input, policy, model}``
``context_assemble``      return ``{"messages": [...]}`` to replace the
                          message list sent to the model
``before_model_call``     observe: ``{turn, phase}``
``before_provider_request``  return ``{"system": ...}`` / ``{"messages": [...]}``
                          / ``{"tools": [...]}`` to patch the outgoing request
                          (this request only, after guardrails have run)
``after_provider_response``  observe: ``{phase, model, stop_reason, usage,
                          cost_usd}`` — the wire-level accounting view
``after_model_response``  observe: ``{turn, text, stop_reason, tool_calls}``
``before_tool_call``      return ``{"block": True, "reason": ...}`` to block,
                          or ``{"input": {...}}`` to replace the tool's args
``after_tool_call``       return ``{"content": ...}`` / ``{"is_error": ...}``
                          to patch the result (middleware, applied in order)
``playbook_enter``        observe: ``{playbook, from, reason}`` — every mode
                          entry, including the run's initial one. To *gate* an
                          entry, use the playbook's own ``on_enter`` hook;
                          this event is for cross-cutting observers.
``playbook_exit``         observe: ``{playbook, to, reason}``
``before_verification``   return ``{"output": ...}`` to replace the final output
                          before output guardrails and validators run
``before_compaction``     return ``{"cancel": True}`` to skip this round, or
                          ``{"summary": "..."}`` to supply the summary yourself
``verification``          observe: ``{verdicts: [...]}``
``run_finished``          observe: ``{status, reason, turns, cost_usd}``
========================  ====================================================

Handler contract (pi's discipline): a handler must not raise. One that does is
logged as a ``hook_error`` trace event and skipped — never crashes the run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hiveloom import ext
from hiveloom import hooks as _builtin_hooks  # noqa: F401 - factory registration
from hiveloom.logging.trace import TraceWriter
from hiveloom.spec.schema import BuiltinHookRef, CodeHookRef, HarnessSpec

EVENTS: tuple[str, ...] = (
    "run_started",
    "context_assemble",
    "before_model_call",
    "before_provider_request",
    "after_provider_response",
    "after_model_response",
    "before_tool_call",
    "after_tool_call",
    "playbook_enter",
    "playbook_exit",
    "before_verification",
    "before_compaction",
    "verification",
    "run_finished",
)

Handler = Callable[[dict[str, Any]], Any]


@dataclass
class _Subscription:
    name: str
    func: Handler


@dataclass
class EventBus:
    """Dispatches events to subscribed handlers; failures never propagate."""

    trace: TraceWriter | None = None
    _subs: dict[str, list[_Subscription]] = field(default_factory=dict)

    def subscribe(self, event: str, name: str, func: Handler) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown event '{event}' (valid: {', '.join(EVENTS)})")
        self._subs.setdefault(event, []).append(_Subscription(name, func))

    def has_handlers(self, event: str) -> bool:
        return bool(self._subs.get(event))

    def emit(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Run handlers in subscription order; return their non-None dict results.

        Each result dict is tagged with the handler's name under ``_handler``
        so call sites can attribute blocks/patches in the trace.
        """
        results: list[dict[str, Any]] = []
        for sub in self._subs.get(event, []):
            try:
                outcome = sub.func(payload)
            except Exception as exc:  # noqa: BLE001 - handlers must never crash the run
                if self.trace is not None:
                    self.trace.emit(
                        "hook_error",
                        event=event,
                        hook=sub.name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                continue
            if isinstance(outcome, dict):
                results.append({**outcome, "_handler": sub.name})
        return results


def build_event_bus(
    spec: HarnessSpec, base_dir: str | Path, trace: TraceWriter | None = None
) -> EventBus:
    """Assemble the bus: ambient extension handlers first, then spec hooks."""
    from hiveloom.spec.loader import _import_hook

    base = Path(base_dir)
    if base.is_file():
        base = base.parent

    bus = EventBus(trace=trace)
    for event, name, func in ext.ambient_hooks():
        bus.subscribe(event, name, func)
    for ref in spec.hooks:
        if isinstance(ref, BuiltinHookRef):
            handler = ext.build("hooks", ref.builtin, ref.params(), ext.BuildContext(base=base))
            bus.subscribe(ref.event, ref.builtin, handler)
        elif isinstance(ref, CodeHookRef):
            func = _import_hook(ref.code, base)
            bus.subscribe(ref.event, ref.code, func)
    return bus
