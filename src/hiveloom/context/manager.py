"""Context assembly, budgeting, and compaction.

Owns the message history for every model call. Enforces ``max_input_tokens``:
when the trigger fires, the configured compaction method runs (a catalog entry
— ``truncate_oldest``/``summarize`` builtin, more via
``ExtensionAPI.register_compaction``). Every compaction is a trace event, and
the ``before_compaction`` event lets hooks cancel a round or supply the summary
themselves.

Oversized tool results are handled before they get here, by
:mod:`hiveloom.context.spill`: the loop stores them whole and passes a
retrievable preview. The in-context truncation below is the backstop for what
spilling does not cover (a blocked call's message, a harness that set
``tool_results.max_inline_bytes: 0``) — it keeps the run inside the budget, but
what it cuts is only recoverable from the trace afterwards.

Every mutation of the message list is journalled as it happens — an append as
``context_append``, a compaction as ``context_compaction`` carrying the
rewritten list — so the conversation is recorded once rather than
re-snapshotted on every model call. :mod:`hiveloom.logging.journal` folds
those events back into the state that went to the model.

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
        # Keep configured pinned history plus the newest exchange. Dropping the
        # oldest message one at a time used to stop at "the newest message",
        # which for a tool turn is the results with their tool_use already
        # gone — the orphan repair then removed them too, so the freshest tool
        # output was the first casualty rather than the last.
        pinned = manager.pinned_message_count
        while (
            manager.retained_tail_start() > pinned
            and manager.estimated_input_tokens() > budget
        ):
            del manager.messages[pinned]
        # The newest exchange alone is over budget: fall back to keeping only
        # the newest message, as before, rather than sending an overflow.
        while (
            len(manager.messages) > pinned + 1
            and manager.estimated_input_tokens() > budget
        ):
            del manager.messages[pinned]


# A structured summary keeps the model oriented after history is dropped:
# free-form summaries reliably preserve *facts* but lose *direction* (what was
# being attempted and what comes next), which is what post-compaction turns
# actually stall on. Fixed section headers force both to survive.
_SUMMARY_FORMAT = """Summarize the agent transcript below so the agent can continue \
the task with the transcript gone. Use exactly these sections, each as terse \
bullet points; write "none" for an empty section:

# Goal
The task being performed, in one line.
# Progress
What has been done and what it produced (include exact values, paths, ids).
# Key decisions
Choices made and why, including approaches ruled out.
# Next steps
What remains to be done, in order.
# Critical context
Verbatim fragments that must survive: identifiers, tool outputs still needed, \
constraints, error messages."""


class SummarizeCompaction(CompactionMethod):
    name = "summarize"

    def compact(self, manager: ContextManager, budget: int) -> None:
        if len(manager.messages) <= manager.pinned_message_count + 1:
            return
        keep_from = manager.retained_tail_start(budget)
        if keep_from <= manager.pinned_message_count:
            # Nothing precedes the newest exchange: summarize its call half
            # rather than make no progress (an overflow retry depends on it).
            keep_from = len(manager.messages) - 1
        older = manager.messages[manager.pinned_message_count : keep_from]
        transcript = _render_for_summary(older)
        summary_prompt = [
            {"role": "user", "content": f"{_SUMMARY_FORMAT}\n\n{transcript}"}
        ]
        response = manager.complete_compaction(
            system="You compress agent transcripts into durable, structured notes.",
            messages=summary_prompt,
        )
        manager.apply_summary(response.text, keep_from=keep_from)


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
        # Snapshotted once: the spec cannot change mid-run, and a section that
        # is rebuilt per assembly would be a needless prompt-cache risk.
        self._memory_section = spec.memory.render() if spec.memory.enabled else ""
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
        self._playbooks: Any = None
        self._notes_index: Callable[[], str | None] | None = None
        self.messages: list[dict[str, Any]] = []
        self._history_count = 0

    @property
    def pinned_message_count(self) -> int:
        """Number of leading messages held persistent through compaction.

        Compaction methods treat this as a *prefix* length, so it can only pin
        the task statement while the task statement is the first message — the
        single-shot case. Once prior turns are seeded (:meth:`seed_history`)
        the task statement is instead the newest message, which every
        compaction method already preserves, and the history in front of it is
        exactly what should be reclaimed first. So nothing is pinned then.
        """
        if self._history_count:
            return 0
        return int("task_statement" in self._config.pinned and bool(self.messages))

    def seed_history(self, messages: list[dict[str, Any]]) -> None:
        """Seed prior conversation turns ahead of the current task statement.

        For multi-turn callers that own the conversation themselves: a chat
        service replays the whole thread each turn, so the earlier turns are
        appended verbatim here and the loop then adds the current input as the
        final user message.
        """
        if not messages:
            return
        for message in messages:
            self._append(message)
        self._history_count += len(messages)

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
    def _append(self, message: dict[str, Any]) -> None:
        """Append a message and journal it. The one write path for history."""
        self.messages.append(message)
        if self._trace is not None:
            self._trace.emit(
                "context_append", index=len(self.messages) - 1, message=message
            )

    def add_user(self, content: str) -> None:
        self._append({"role": "user", "content": content})

    def add_assistant(self, content_blocks: list[dict[str, Any]]) -> None:
        self._append({"role": "assistant", "content": content_blocks})

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
        self._append({"role": "user", "content": blocks})

    def set_plan(self, plan: str) -> None:
        """Pin a plan (plan_then_act) into the system prompt so it is never dropped."""
        self._plan = plan

    def set_playbooks(self, manager: Any) -> None:
        """Attach the :class:`~hiveloom.playbooks.PlaybookManager` for this run.

        Held rather than snapshotted so the system prompt reflects the current
        mode on every assembly — a switch has to change what the model is
        told, not just which tools it has.
        """
        self._playbooks = manager

    def set_notes_index(self, index: Callable[[], str | None]) -> None:
        """Attach the run's note index (see :mod:`hiveloom.context.notes`).

        A callable rather than a rendered string: notes are written during the
        run, so what the model is told it has must be re-read on every
        assembly. Returning None means the store is empty and no section is
        rendered at all.
        """
        self._notes_index = index

    # ------------------------------------------------------------------ #
    # Assembly & budgeting
    # ------------------------------------------------------------------ #
    def system(self) -> str:
        parts = [self._system_prompt]
        if self._playbooks is not None and self._playbooks.names:
            from hiveloom.playbooks import playbook_index

            parts.append(
                playbook_index(self._playbooks.all(), self._playbooks.current_name)
            )
            fragment = self._playbooks.prompt_fragment()
            if fragment:
                current = self._playbooks.current_name
                parts.append(f"# Playbook: {current}\n{fragment}")
        if self._plan:
            parts.append(f"# Plan\n{self._plan}")
        if self._skills:
            from hiveloom.skills import skill_index

            # Point the model at whichever loader this harness actually
            # carries; a spec with load_skill need not ship file_read at all.
            has_load_skill = (
                self._registry is not None and "load_skill" in self._registry.active_names()
            )
            parts.append(
                skill_index(
                    self._skills, loader="load_skill" if has_load_skill else "file_read"
                )
            )
        # Durable lessons (spec `memory`), after the skills index and before the
        # tool guidelines: standing constraints on how to work, in declaration
        # order and identical on every turn, so the prefix stays cacheable.
        if self._memory_section:
            parts.append(self._memory_section)
        # What this run has written down, listed by name in a stable order. The
        # notes themselves stay out of context — this is the table of contents
        # that tells the model what it can read back after compaction.
        if self._notes_index is not None:
            index = self._notes_index()
            if index:
                parts.append(index)
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

    def rewind_to(self, count: int) -> int:
        """Drop every message after the first ``count``. Returns how many went.

        Compaction *summarises* history because the run still depends on it.
        This discards it outright, which is what an independent retry needs: a
        second attempt that can see the first is not a second sample, it is a
        continuation, and averaging it with the first would be averaging a
        thing with its own echo. The pinned prefix (system prompt and task
        statement) is what ``count`` is normally set to.
        """
        count = max(0, min(count, len(self.messages)))
        dropped = len(self.messages) - count
        if not dropped:
            return 0
        self.messages = self.messages[:count]
        if self._trace is not None:
            self._trace.emit("context_rewound", kept=count, dropped=dropped)
        return dropped

    def retained_tail_start(self, budget: int | None = None) -> int:
        """Index of the first message compaction keeps verbatim at the end.

        The newest *exchange* survives compaction whole: when the last message
        answers tool calls, the assistant message that made those calls stays
        with it. Keeping only the last message instead orphans its results,
        and the orphan repair then drops them — so the output the model had
        just asked for vanished unread, neither summarized nor kept, and the
        model re-ran the same calls after every compaction.

        If that exchange alone would take more than half of ``budget``, only
        the last message is kept (as before) and the rest is compacted, so a
        turn of large results cannot pin the context above its trigger.
        """
        messages = self.messages
        floor = self.pinned_message_count
        last = len(messages) - 1
        if last < floor:
            return len(messages)
        start = last
        if (
            last - 1 >= floor
            and _has_block(messages[last], "tool_result")
            and messages[last - 1].get("role") == "assistant"
            and _has_block(messages[last - 1], "tool_use")
        ):
            start = last - 1
        if start < last and budget is not None:
            tail_tokens = self.provider.count_tokens(system="", messages=messages[start:])
            if tail_tokens > budget // 2:
                start = last
        return start

    def apply_summary(self, summary: str, *, keep_from: int | None = None) -> None:
        """Replace compactible history with a summary while retaining pinned messages.

        ``keep_from`` is where the verbatim tail starts (see
        :meth:`retained_tail_start`); by default the newest exchange is kept.
        """
        pinned = self.messages[: self.pinned_message_count]
        if keep_from is None:
            keep_from = self.retained_tail_start(self._config.max_input_tokens)
        keep_from = max(self.pinned_message_count, min(keep_from, len(self.messages)))
        recent = self.messages[keep_from:] if len(self.messages) > 1 else []
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
        return self._compact_now(budget, tokens)

    def force_compact(self) -> bool:
        """Compact unconditionally, after the provider rejected a request as too long.

        An overflow proves the offline token estimate under-counted, so the
        target budget is half the *current* estimate (capped at the configured
        budget) rather than the configured budget alone — enough to guarantee
        real headroom even when the estimator is well off.
        """
        if self._config.strategy == "full":
            return False
        if len(self.messages) <= self.pinned_message_count + 1:
            return False
        tokens = self.estimated_input_tokens()
        budget = min(self._config.max_input_tokens, max(1, tokens // 2))
        return self._compact_now(budget, tokens)

    def _compact_now(self, budget: int, tokens: int) -> bool:
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

        # Every compaction reclaims space by removing whole messages, and the
        # boundary it picks can fall between an assistant's ``tool_use`` and the
        # ``tool_result`` answering it. Providers reject a transcript with a
        # ``tool_result`` whose call is gone, so the run dies on the next turn
        # with a 400 rather than continuing shorter. Repair here, once, so this
        # holds for hook summaries and extension-registered methods too.
        self.messages = _drop_orphan_tool_results(self.messages)

        if self._trace is not None:
            self._trace.emit(
                "context_compaction",
                method="hook_summary" if custom_summary is not None else method_name,
                tokens_before=tokens,
                tokens_after=self.estimated_input_tokens(),
                messages_before=before,
                messages_after=len(self.messages),
                # Compaction rewrites history rather than extending it, so the
                # journal carries the result: the fold replaces on this event.
                # Cheap by construction — a compaction only ever shrinks.
                messages=list(self.messages),
            )
        return True


def _drop_orphan_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove ``tool_result`` blocks whose ``tool_use`` is no longer present.

    A message left with no blocks at all is dropped with them: an empty
    ``content`` list is itself an invalid request. Blocks of every other kind
    are passed through untouched, so a message that mixes a surviving result
    with an orphaned one keeps the half that is still answerable.
    """
    seen_tool_use_ids: set[str] = set()
    repaired: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            repaired.append(message)
            continue
        kept: list[Any] = []
        for block in content:
            if not isinstance(block, dict):
                kept.append(block)
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                seen_tool_use_ids.add(block["id"])
            elif block.get("type") == "tool_result":
                if block.get("tool_use_id") not in seen_tool_use_ids:
                    continue
            kept.append(block)
        if not kept:
            continue
        repaired.append(message if len(kept) == len(content) else {**message, "content": kept})
    return repaired


def _has_block(message: dict[str, Any], kind: str) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == kind for block in content
    )


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
