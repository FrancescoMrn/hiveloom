"""Playbooks: named modes a harness switches between during a run.

A playbook bundles the things that make an agent good at one *kind* of work —
a prompt fragment, the tools that mode may use, the validators that grade it,
and optional enter/exit code hooks — behind a name the model can switch to.
One harness with three playbooks replaces three harnesses that would otherwise
duplicate a system prompt and split their evidence three ways.

The runtime piece lives here; :class:`~hiveloom.spec.schema.PlaybookRef` is the
declarative contract. :class:`PlaybookManager` owns the current mode and
applies switches; the agent loop drives it and records each switch on the
trace, which is what gives the Hive a per-playbook view.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveloom.errors import SpecError
from hiveloom.spec.schema import HarnessSpec, PlaybookRef

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.tools.registry import ToolRegistry


@dataclass
class LoadedPlaybook:
    """A validated playbook with its prompt text resolved."""

    ref: PlaybookRef
    prompt_text: str = ""
    on_enter: Callable[[dict[str, Any]], Any] | None = None
    on_exit: Callable[[dict[str, Any]], Any] | None = None

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def description(self) -> str:
        return self.ref.description


@dataclass
class SwitchOutcome:
    """What a switch attempt did."""

    ok: bool
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    previous: str | None = None


def load_playbooks(spec: HarnessSpec, base_dir: str | Path) -> list[LoadedPlaybook]:
    """Load every declared playbook: prompt file plus resolved code hooks.

    Raises :class:`SpecError` if a prompt file or hook is missing, so a broken
    playbook fails at assembly rather than halfway through a run.
    """
    from hiveloom.spec.loader import _import_hook

    base = Path(base_dir)
    if base.is_file():
        base = base.parent

    loaded: list[LoadedPlaybook] = []
    for ref in spec.playbooks:
        prompt_text = ""
        if ref.prompt:
            prompt_path = base / ref.prompt
            if not prompt_path.exists():
                raise SpecError(
                    f"playbook '{ref.name}' prompt not found: {ref.prompt}"
                )
            prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        loaded.append(
            LoadedPlaybook(
                ref=ref,
                prompt_text=prompt_text,
                on_enter=_import_hook(ref.on_enter, base) if ref.on_enter else None,
                on_exit=_import_hook(ref.on_exit, base) if ref.on_exit else None,
            )
        )
    return loaded


def playbook_index(playbooks: list[LoadedPlaybook], current: str | None) -> str:
    """The system-prompt section listing modes and marking the current one."""
    if not playbooks:
        return ""
    lines = [
        "# Playbooks",
        "You are working in one playbook at a time. Switch with the "
        "switch_playbook tool when the conversation moves into another mode's "
        "area; the tools available to you change with it.",
    ]
    for playbook in playbooks:
        marker = " (current)" if playbook.name == current else ""
        lines.append(f"- {playbook.name}{marker}: {playbook.description}")
    return "\n".join(lines)


class PlaybookManager:
    """Owns the active playbook and applies switches.

    Kept separate from the loop so the switching rules — hook blocks, tool
    activation, prompt swap — are testable without driving a whole run.
    """

    def __init__(
        self,
        playbooks: list[LoadedPlaybook],
        registry: ToolRegistry | None = None,
        *,
        max_blocked_exits: int = 3,
    ):
        self._playbooks = {p.name: p for p in playbooks}
        self._order = [p.name for p in playbooks]
        self._registry = registry
        self._current: str | None = None
        # A badly written exit gate could otherwise trap the run in one mode
        # forever. After this many consecutive refusals from the same playbook
        # the gate is force-released (and the release is traced).
        self._max_blocked_exits = max_blocked_exits
        self._blocked_exits = 0

    # ------------------------------------------------------------------ #
    @property
    def current(self) -> LoadedPlaybook | None:
        return self._playbooks.get(self._current) if self._current else None

    @property
    def current_name(self) -> str | None:
        return self._current

    @property
    def names(self) -> list[str]:
        return list(self._order)

    def all(self) -> list[LoadedPlaybook]:
        return [self._playbooks[name] for name in self._order]

    def get(self, name: str) -> LoadedPlaybook | None:
        return self._playbooks.get(name)

    def entry_name(self) -> str | None:
        """The playbook a run starts in: the declared entry, else the first."""
        for name in self._order:
            if self._playbooks[name].ref.entry:
                return name
        return self._order[0] if self._order else None

    # ------------------------------------------------------------------ #
    def prompt_fragment(self) -> str:
        current = self.current
        return current.prompt_text if current else ""

    def active_validator_refs(self) -> list[Any]:
        current = self.current
        return list(current.ref.validators) if current else []

    def apply_tools(self) -> None:
        """Narrow the registry to the current playbook's tool subset."""
        current = self.current
        if current is None or self._registry is None or current.ref.tools is None:
            return
        # switch_playbook always survives: a mode the model cannot leave is a
        # trap, not a mode.
        keep = set(current.ref.tools) | {"switch_playbook"}
        self._registry.set_active([n for n in self._registry.names() if n in keep])

    # ------------------------------------------------------------------ #
    def switch(
        self,
        name: str,
        *,
        run_context: dict[str, Any] | None = None,
        reason: str = "",
        on_hook_error: Callable[[str, str, Exception], None] | None = None,
    ) -> SwitchOutcome:
        """Move to playbook ``name``, honoring exit and entry gates."""
        target = self._playbooks.get(name)
        if target is None:
            available = ", ".join(self._order) or "(none)"
            return SwitchOutcome(
                ok=False, reason=f"unknown playbook '{name}'. Available: {available}"
            )
        if name == self._current:
            return SwitchOutcome(ok=False, reason=f"already in playbook '{name}'")

        payload = {
            "playbook": name,
            "from": self._current,
            "reason": reason,
            "run_context": dict(run_context or {}),
        }
        notes: list[str] = []
        previous = self.current

        if previous is not None and previous.on_exit is not None:
            outcome = self._call_hook(previous, "on_exit", payload, on_hook_error)
            if isinstance(outcome, dict) and outcome.get("block"):
                self._blocked_exits += 1
                if self._blocked_exits < self._max_blocked_exits:
                    return SwitchOutcome(
                        ok=False,
                        reason=str(
                            outcome.get("reason")
                            or f"playbook '{previous.name}' refused to be left"
                        ),
                        previous=previous.name,
                    )
                # Force-release: the gate has refused too many times running.
                notes.append(
                    f"exit gate of '{previous.name}' force-released after "
                    f"{self._blocked_exits} refusals"
                )
            if isinstance(outcome, dict) and isinstance(outcome.get("context"), str):
                notes.append(outcome["context"])

        if target.on_enter is not None:
            outcome = self._call_hook(target, "on_enter", payload, on_hook_error)
            if isinstance(outcome, dict) and outcome.get("block"):
                return SwitchOutcome(
                    ok=False,
                    reason=str(
                        outcome.get("reason") or f"playbook '{name}' refused entry"
                    ),
                    previous=self._current,
                )
            if isinstance(outcome, dict) and isinstance(outcome.get("context"), str):
                notes.append(outcome["context"])

        self._blocked_exits = 0
        self._current = name
        self.apply_tools()
        return SwitchOutcome(
            ok=True, notes=notes, previous=previous.name if previous else None
        )

    def enter_initial(
        self,
        *,
        run_context: dict[str, Any] | None = None,
        on_hook_error: Callable[[str, str, Exception], None] | None = None,
    ) -> SwitchOutcome:
        """Enter the starting playbook. Entry gates cannot block the start.

        A blocked start would leave the run in no mode at all, with a system
        prompt that promises modes — worse than entering and letting the
        hook's note explain the problem.
        """
        name = self.entry_name()
        if name is None:
            return SwitchOutcome(ok=False, reason="no playbooks declared")
        target = self._playbooks[name]
        notes: list[str] = []
        if target.on_enter is not None:
            outcome = self._call_hook(
                target,
                "on_enter",
                {
                    "playbook": name,
                    "from": None,
                    "reason": "run start",
                    "run_context": dict(run_context or {}),
                },
                on_hook_error,
            )
            if isinstance(outcome, dict):
                if isinstance(outcome.get("context"), str):
                    notes.append(outcome["context"])
                if outcome.get("block"):
                    notes.append(
                        f"entry hook wanted to block the start: "
                        f"{outcome.get('reason') or 'no reason given'}"
                    )
        self._current = name
        self.apply_tools()
        return SwitchOutcome(ok=True, notes=notes)

    # ------------------------------------------------------------------ #
    def _call_hook(
        self,
        playbook: LoadedPlaybook,
        kind: str,
        payload: dict[str, Any],
        on_hook_error: Callable[[str, str, Exception], None] | None,
    ) -> Any:
        """Run a playbook hook. A raising hook is reported and ignored.

        Same discipline as the event bus: a handler must never crash the run.
        """
        func = playbook.on_enter if kind == "on_enter" else playbook.on_exit
        if func is None:
            return None
        try:
            return func(dict(payload))
        except Exception as exc:  # noqa: BLE001 - hooks must never crash the run
            if on_hook_error is not None:
                on_hook_error(playbook.name, kind, exc)
            return None
