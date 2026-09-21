"""Deterministic provider for the memory-lab demo.

Scoped to this harness through ``extensions`` so the walkthrough is offline and
reproducible; it is not a runtime builtin. The executor script does not carry
the answers: every value it reports is parsed out of a tool result it actually
received, which is what makes the trace worth reading.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse

_HANDLE = re.compile(r"tr_[0-9a-f]{16}")
_DIGEST = re.compile(r"digest=([A-Z0-9-]+)")
_CODE = re.compile(r"code=([A-Z_]+)")
_MATCHING = re.compile(r"(\d+) lines matching")


def _results(messages: list[Message]) -> dict[str, str]:
    """Every tool result so far, keyed by the call id the script assigned."""
    found: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = "".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                found[str(block.get("tool_use_id"))] = str(body)
    return found


def _first_handle(text: str, *, other_than: str | None = None) -> str | None:
    """The first handle in ``text`` — or the first one that is not the source.

    A derived object's preview quotes the source handle in its header line
    before the marker that names the new object, so "first" is not enough
    when the script is looking for the object a transform produced.
    """
    for match in _HANDLE.finditer(text):
        if match.group(0) != other_than:
            return match.group(0)
    return None


class MemoryLabProvider(ModelProvider):
    """A scripted analyst that walks the L2 tools, then a scripted evolver."""

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        if config.id == "qa-evolver":
            return self._evolver()
        if config.id == "qa-analyst":
            return self._analyst(messages, {t.get("name") for t in tools})
        return text_response("unsupported memory-lab model")

    # ------------------------------------------------------------------ #
    def _analyst(self, messages: list[Message], tool_names: set[str]) -> ModelResponse:
        seen = _results(messages)

        # 1. Read the log. It is far above the inline budget, so the result
        #    comes back as a preview plus a handle.
        if "read-1" not in seen:
            return tool_response("file_read", {"path": "data/service.log"}, call_id="read-1")
        log = _first_handle(seen["read-1"])
        if log is None:
            return text_response("The log came back inline; this demo expects it to spill.")

        # 2. Count ERROR lines in place: nothing is paged through context.
        if "count-1" not in seen:
            return tool_response(
                "transform_result",
                {"handle": log, "op": "count", "pattern": "ERROR"},
                call_id="count-1",
            )
        # 3. Pull only the ERROR lines that carry a code= value.
        if "codes-1" not in seen:
            return tool_response(
                "transform_result",
                {"handle": log, "op": "grep", "pattern": "ERROR.*code=", "max_matches": 200},
                call_id="codes-1",
            )
        # 4. The digest lives on the final SUMMARY line: read the tail.
        if "tail-1" not in seen:
            return tool_response(
                "transform_result",
                {"handle": log, "op": "tail", "bytes": 400},
                call_id="tail-1",
            )

        count_match = _MATCHING.search(seen["count-1"])
        error_count = int(count_match.group(1)) if count_match else 0
        codes = Counter(_CODE.findall(seen["codes-1"]))
        dominant = codes.most_common(1)[0][0] if codes else "UNKNOWN"
        digest_match = _DIGEST.search(seen["tail-1"])
        digest = digest_match.group(1) if digest_match else "UNKNOWN"

        # 5. Record the findings outside the conversation, so compaction can
        #    drop the turns above without losing them.
        if "note-1" not in seen:
            return tool_response(
                "notes",
                {
                    "action": "write",
                    "name": "findings",
                    "content": (
                        f"error_count={error_count}\n"
                        f"dominant_failure_code={dominant}\n"
                        f"build_digest={digest}\n"
                    ),
                },
                call_id="note-1",
            )
        # 6. A wider grep with context is still too large to inline: it comes
        #    back as a *derived* object with its own handle.
        if "context-1" not in seen:
            return tool_response(
                "transform_result",
                {
                    "handle": log,
                    "op": "grep",
                    "pattern": "ERROR",
                    "max_matches": 200,
                    "context_lines": 2,
                },
                call_id="context-1",
            )
        # 7. Hand that derived object to file_write by handle. The bytes never
        #    enter the conversation; the runtime expands the handle at dispatch.
        if "write-1" not in seen:
            derived = _first_handle(seen["context-1"], other_than=log) or seen["context-1"]
            return tool_response(
                "file_write",
                {"path": "out/error-context.txt", "content": derived},
                call_id="write-1",
            )
        # 8. Read the note back (after compaction this is the only copy).
        if "recall-1" not in seen:
            return tool_response(
                "notes", {"action": "read", "name": "findings"}, call_id="recall-1"
            )
        # 9. Propose a durable lesson. Queued for review, never applied here.
        if "propose_memory" in tool_names and "propose-1" not in seen:
            return tool_response(
                "propose_memory",
                {
                    "kind": "rule",
                    "title": "Count before naming a code",
                    "content": (
                        "Establish error_count with a count transform before choosing "
                        "dominant_failure_code; the grep alone under-reports lines "
                        "without a code= value."
                    ),
                    "evidence": (
                        f"this run: {error_count} ERROR lines, "
                        f"{sum(codes.values())} with a code"
                    ),
                },
                call_id="propose-1",
            )

        # 10. The answer, from the note rather than from the turns above.
        note = dict(
            line.split("=", 1) for line in seen["recall-1"].splitlines() if "=" in line
        )
        return text_response(
            json.dumps(
                {
                    "dominant_failure_code": note.get("dominant_failure_code", dominant),
                    "error_count": int(note.get("error_count", error_count)),
                    "build_digest": note.get("build_digest", digest),
                }
            )
        )

    # ------------------------------------------------------------------ #
    def _evolver(self) -> ModelResponse:
        """Append one memory entry, at whatever position the store has by then.

        `memory.entries.+` is resolved when the proposal is applied, so this
        script never has to read the entry count out of the evolve prompt —
        and a proposal queued before another one landed still appends.
        """
        proposal = {
            "rationale": "Operator finding: the digest is only ever on the last line.",
            "yaml_changes": [
                {
                    "path": "memory.entries.+",
                    "value": {
                        "id": "digest-is-in-the-tail",
                        "kind": "fact",
                        "title": "The build digest is on the final SUMMARY line",
                        "content": (
                            "Read the tail of the spilled log for digest=; it is never "
                            "in the head preview."
                        ),
                        "source": "evolve:qa-evolver",
                    },
                    "rationale": "Turns a repeated tail read into a standing fact.",
                }
            ],
            "code_changes": [],
        }
        return text_response(json.dumps(proposal))


def hiveloom_extension(hive) -> None:
    models = [
        ModelInfo(id=model, provider="memory_lab", context_window=32768)
        for model in ("qa-analyst", "qa-evolver")
    ]
    hive.register_provider(
        "memory_lab",
        lambda _ctx: MemoryLabProvider(),
        models=models,
        api="local",
        open_catalog=False,
        label="Memory Lab (offline demo)",
    )
