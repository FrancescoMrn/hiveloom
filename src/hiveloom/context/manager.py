"""Context assembly, budgeting, and compaction.

Owns the message history for every model call. Enforces ``max_input_tokens``:
when the trigger fires, the configured compaction method runs (a catalog entry
— ``truncate_oldest``/``summarize`` builtin, more via
``ExtensionAPI.register_compaction``). Large tool results are truncated
in-context with a marker (the full result is persisted to the trace by the
loop). Every compaction is a trace event, and the ``before_compaction`` event
lets hooks cancel a round or supply the summary themselves.

The system prompt is assembled from the spec's ``system_prompt`` plus, when
present: the pinned plan (plan_then_act), the skills index, and active tools'
usage guidelines.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hiveloom import ext
from hiveloom.logging.trace import TraceWriter
from hiveloom.models.provider import ModelConfig, ModelProvider
from hiveloom.spec.schema import HarnessSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.events import EventBus
    from hiveloom.skills import Skill
    from hiveloom.tools.registry import ToolRegistry

class CompactionMethod:
    """A pluggable way to reclaim context space (a ``compaction`` catalog entry)."""

    name: str = "compaction"

    def compact(self, manager: ContextManager, budget: int) -> None:
        raise NotImplementedError


class TruncateOldestCompaction(CompactionMethod):
    name = "truncate_oldest"

    def compact(self, manager: ContextManager, budget: int) -> None:
        # Keep configured pinned history plus the newest message.
        while (
            len(manager.messages) > manager.pinned_message_count + 1
            and manager.estimated_input_tokens() > budget
        ):
            del manager.messages[manager.pinned_message_count]


class SummarizeCompaction(CompactionMethod):
    name = "summarize"

    def compact(self, manager: ContextManager, budget: int) -> None:
        if len(manager.messages) <= manager.pinned_message_count + 1:
            return
        older = manager.messages[manager.pinned_message_count : -1]
        transcript = _render_for_summary(older)
        summary_prompt = [
            {
                "role": "user",
                "content": (
                    "Summarize the following agent transcript into a compact set of "
                    "facts and decisions to preserve. Be terse.\n\n" + transcript
                ),
            }
        ]
        response = manager.complete_compaction(
            system="You compress agent transcripts into durable notes.",
            messages=summary_prompt,
        )
        manager.apply_summary(response.text)


class ContextManager:
    """Assembles and budgets the input to every model call."""

    def __init__(
        self,
        spec: HarnessSpec,
        provider: ModelProvider,
        trace: TraceWriter | None = None,
        *,
        tool_result_max_chars: int | None = None,
        events: EventBus | None = None,
        registry: ToolRegistry | None = None,
        skills: list[Skill] | None = None,
    ):
        self._config = spec.context
        self.model_config = ModelConfig(
            id=spec.model.id,
            max_tokens=spec.model.max_tokens,
            temperature=spec.model.temperature,
            provider=spec.model.provider,
        )
        self._system_prompt = spec.system_prompt
        self.provider = provider
        self._trace = trace
        self._events = events
        self._registry = registry
        self._skills = list(skills or [])
        # Reserve roughly a quarter of the input budget for one result (using
        # a four-characters-per-token estimate), instead of a fixed 8 KB cap.
        self._tool_result_max_chars = (
            max(1, self._config.max_input_tokens)
            if tool_result_max_chars is None
            else tool_result_max_chars
        )
        self._compaction_model_call: Callable[[str, list[dict[str, Any]]], Any] | None = None
        self._plan: str | None = None
        self.messages: list[dict[str, Any]] = []

    @property
    def pinned_message_count(self) -> int:
        """Number of history messages declared persistent through compaction."""
        return int("task_statement" in self._config.pinned and bool(self.messages))

    def set_compaction_model_call(
        self, callback: Callable[[str, list[dict[str, Any]]], Any]
    ) -> None:
        """Route compaction through the run loop's accounting and guardrails."""
        self._compaction_model_call = callback

    def complete_compaction(self, *, system: str, messages: list[dict[str, Any]]):
        if self._compaction_model_call is not None:
            return self._compaction_model_call(system, messages)
        return self.provider.complete(
            system=system, messages=messages, tools=[], config=self.model_config
        )

    # ------------------------------------------------------------------ #
    # Building the conversation
    # ------------------------------------------------------------------ #
    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content_blocks: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "assistant", "content": content_blocks})

    def add_tool_results(self, results: list[dict[str, Any]]) -> None:
        """Append a user message of tool_result blocks (truncating large ones)."""
        blocks: list[dict[str, Any]] = []
        for result in results:
            content = result["content"]
            if len(content) > self._tool_result_max_chars:
                content = (
                    content[: self._tool_result_max_chars]
                    + f"\n[... truncated {len(content) - self._tool_result_max_chars} chars;"
                    " full result in trace ...]"
                )
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": result["tool_use_id"],
                    "content": content,
                    "is_error": result.get("is_error", False),
                }
            )
        self.messages.append({"role": "user", "content": blocks})

    def set_plan(self, plan: str) -> None:
        """Pin a plan (plan_then_act) into the system prompt so it is never dropped."""
        self._plan = plan

    # ------------------------------------------------------------------ #
    # Assembly & budgeting
    # ------------------------------------------------------------------ #
    def system(self) -> str:
        parts = [self._system_prompt]
        if self._plan:
            parts.append(f"# Plan\n{self._plan}")
        if self._skills:
            from hiveloom.skills import skill_index

            parts.append(skill_index(self._skills))
        if self._registry is not None:
            guidelines = self._registry.guidelines()
            if guidelines:
                parts.append("# Tool guidelines\n" + "\n".join(f"- {g}" for g in guidelines))
        return "\n\n".join(parts)

    def assemble(self) -> tuple[str, list[dict[str, Any]]]:
        """Return (system, messages) for the next call, compacting if needed."""
        self.maybe_compact()
        # Assembly hooks affect one provider request only. Persisting their
        # replacement can leave dangling tool-use/result blocks in history.
        messages = list(self.messages)
        if self._events is not None and self._events.has_handlers("context_assemble"):
            for outcome in self._events.emit("context_assemble", {"messages": messages}):
                replacement = outcome.get("messages")
                if isinstance(replacement, list):
                    messages = replacement
        return self.system(), messages

    def estimated_input_tokens(self) -> int:
        return self.provider.count_tokens(system=self.system(), messages=self.messages)

    def apply_summary(self, summary: str) -> None:
        """Replace compactible history with a summary while retaining pinned messages."""
        pinned = self.messages[: self.pinned_message_count]
        recent = self.messages[-1:] if len(self.messages) > 1 else []
        summary_block = {
            "role": "user",
            "content": f"[summary of earlier turns]\n{summary}",
        }
        self.messages = [*pinned, summary_block, *recent]

    def maybe_compact(self) -> bool:
        """Compact the history if it exceeds the configured trigger. Returns True if it did."""
        if self._config.strategy == "full":
            return False
        budget = self._config.max_input_tokens
        trigger = budget * self._config.compaction.trigger_at_pct / 100
        tokens = self.estimated_input_tokens()
        if tokens <= trigger:
            return False

        method_name = (
            "summarize" if self._config.strategy == "summary" else self._config.compaction.method
        )
        before = len(self.messages)

        custom_summary: str | None = None
        if self._events is not None:
            for outcome in self._events.emit(
                "before_compaction", {"messages": self.messages, "method": method_name}
            ):
                if outcome.get("cancel"):
                    return False
                if isinstance(outcome.get("summary"), str):
                    custom_summary = outcome["summary"]

        if custom_summary is not None:
            self.apply_summary(custom_summary)
        else:
            method = ext.build("compaction", method_name, {}, ext.BuildContext())
            method.compact(self, budget)

        if self._trace is not None:
            self._trace.emit(
                "context_compaction",
                method="hook_summary" if custom_summary is not None else method_name,
                tokens_before=tokens,
                tokens_after=self.estimated_input_tokens(),
                messages_before=before,
                messages_after=len(self.messages),
            )
        return True


def _render_for_summary(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content", "")
        if isinstance(content, str):
            lines.append(f"{role}: {content}")
        else:
            for block in content:
                text = block.get("text") or block.get("content") or str(block)
                lines.append(f"{role} [{block.get('type', '?')}]: {text}")
    return "\n".join(lines)


def _register_factories() -> None:
    ext.register_builtin_factory(
        "compaction", "truncate_oldest", lambda _p, _c: TruncateOldestCompaction()
    )
    ext.register_builtin_factory(
        "compaction", "summarize", lambda _p, _c: SummarizeCompaction()
    )


_register_factories()
