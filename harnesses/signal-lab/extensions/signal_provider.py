"""Deterministic provider for the signal-lab demo.

Scoped to this harness through ``extensions`` so the walkthrough is offline and
reproducible; it is not a runtime builtin. Neither script carries the answer:

* ``qa-clerk``, the executor, reads what its context actually says. It
  uppercases an invoice id only when a lesson in its system prompt tells it
  to, calls the tool again after an error only when the prompt tells it to,
  and reports the amount it parsed out of the tool result it received.
* ``qa-evolver`` reads its prompt the way a model would. As the evolver it
  aims at the strongest ``tool_error`` signal in the signal map, and chooses
  its change from the measured attempt history: an idea already tried and not
  confirmed is not proposed again. As the reflector it drafts a lesson only
  when the run's own evidence (a failed lookup of a lowercase id) supports one.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse

_INVOICE = re.compile(r"\binv-\d{4}\b", re.IGNORECASE)
_UPPERCASE_RULE = re.compile(
    r"invoice ids?\b[^\n]*uppercase|uppercase[^\n]*invoice ids?", re.IGNORECASE
)
_RETRY_RULE = re.compile(r"call (?:it|lookup_invoice) once more", re.IGNORECASE)
_SIGNAL_LINE = re.compile(
    r"^\s+(tool_error:[\w.-]+)(?: \(same runs as:[^)]*\))?: "
    r"(\d+)/(\d+) vs (\d+)/(\d+), risk",
    re.MULTILINE,
)

RETRY_SENTENCE = (
    "If lookup_invoice returns an error, call it once more with the same id before "
    "answering."
)
LESSON = {
    "id": "invoice-ids-are-uppercase",
    "kind": "rule",
    "title": "Invoice ids are uppercase in the ledger",
    "content": (
        "The ledger stores invoice ids in uppercase (INV-1004) and lookup_invoice "
        "matches exactly: uppercase the invoice id before calling it."
    ),
    "source": "evolve:qa-evolver",
}


def _results(messages: list[Message]) -> dict[str, tuple[str, bool]]:
    """Every tool result so far: call id -> (content, is_error)."""
    found: dict[str, tuple[str, bool]] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = "".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                found[str(block.get("tool_use_id"))] = (str(body), bool(block.get("is_error")))
    return found


def _task(messages: list[Message]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    return text.split(start, 1)[1].split(end, 1)[0]


class SignalLabProvider(ModelProvider):
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        if config.id == "qa-clerk":
            return self._clerk(system, messages)
        if config.id == "qa-evolver":
            prompt = _task(messages)
            if system.startswith("You review one finished run"):
                return self._reflect(prompt)
            return self._evolve(prompt)
        return text_response("unsupported signal-lab model")

    # ------------------------------------------------------------------ #
    def _clerk(self, system: str, messages: list[Message]) -> ModelResponse:
        match = _INVOICE.search(_task(messages))
        if match is None:
            return text_response(json.dumps({"invoice_id": None, "status": "not_found"}))
        written = match.group(0)
        invoice_id = written.upper() if _UPPERCASE_RULE.search(system) else written
        seen = _results(messages)

        if "lookup-1" not in seen:
            return tool_response(
                "lookup_invoice", {"invoice_id": invoice_id}, call_id="lookup-1"
            )
        last = seen["lookup-1"]
        if last[1] and _RETRY_RULE.search(system):
            if "lookup-2" not in seen:
                return tool_response(
                    "lookup_invoice", {"invoice_id": invoice_id}, call_id="lookup-2"
                )
            last = seen["lookup-2"]

        body, is_error = last
        if is_error:
            return text_response(json.dumps({"invoice_id": invoice_id, "status": "not_found"}))
        row = json.loads(body)
        return text_response(
            json.dumps(
                {"invoice_id": row["invoice_id"], "status": "found", "amount": row["amount"]}
            )
        )

    # ------------------------------------------------------------------ #
    def _evolve(self, prompt: str) -> ModelResponse:
        signal_map = _between(prompt, "<signal_map>", "</signal_map>")
        history = _between(prompt, "<untrusted_attempt_history>", "</untrusted_attempt_history>")
        spec = yaml.safe_load(
            _between(prompt, "Current harness spec (YAML):\n", "\nMutable paths:")
        )

        match = _SIGNAL_LINE.search(signal_map)
        if match is None:
            return text_response(json.dumps({
                "rationale": "no tool_error signal to aim at",
                "target": {"signal": "success_rate", "expect": "increase"},
                "yaml_changes": [],
            }))
        feature = match.group(1)
        with_rate = int(match.group(2)) / int(match.group(3))
        without_rate = int(match.group(4)) / int(match.group(5))
        target = {
            "signal": feature,
            "expect": "decrease",
            "by": round(with_rate - without_rate, 2),
            "rationale": (
                f"failure rate {with_rate:.0%} with {feature} vs {without_rate:.0%} without"
            ),
        }

        # First idea: the error looks transient, so retry it. Only once the
        # measured history shows that idea did not move the signal does the
        # evolver reach for the lesson the failures actually support.
        retry_tried = RETRY_SENTENCE.split(",")[0] in history
        if not retry_tried:
            prompt_text = (spec or {}).get("system_prompt", "").rstrip()
            return text_response(json.dumps({
                "rationale": f"{feature} separates failures; retry the lookup once on error",
                "target": target,
                "yaml_changes": [{
                    "path": "system_prompt",
                    "value": f"{prompt_text}\n\n{RETRY_SENTENCE}\n",
                    "rationale": "a transient ledger error would clear on a second call",
                }],
            }))
        return text_response(json.dumps({
            "rationale": (
                f"retrying did not move {feature} (see history); the failing ids are "
                "lowercase and the ledger's are uppercase"
            ),
            "target": {**target, "by": round(with_rate - without_rate, 2)},
            "yaml_changes": [{
                "path": "memory.entries.+",
                "value": {**LESSON, "evidence": target["rationale"]},
                "rationale": "a standing rule about the ledger, not a retry",
            }],
        }))

    def _reflect(self, prompt: str) -> ModelResponse:
        run = json.loads(_between(prompt, "<run>\n", "\n</run>") or "{}")
        task = str(run.get("task") or "")
        failed_lookup = any(
            item.get("category") == "tool_error" and item.get("component") == "lookup_invoice"
            for item in run.get("friction") or []
        )
        written = _INVOICE.search(task)
        if not (failed_lookup and written and written.group(0) != written.group(0).upper()):
            return text_response(json.dumps({"lessons": []}))
        return text_response(json.dumps({"lessons": [{
            "kind": "fact",
            "title": "The ledger keys invoices in uppercase",
            "content": (
                "Invoice ids in the ledger are uppercase (INV-1004); a lowercase id "
                "from the task will not be found as written."
            ),
            "evidence": (
                f"lookup_invoice failed for {written.group(0)!r}, written in lowercase"
            ),
        }]}))


def hiveloom_extension(hive) -> None:
    hive.register_provider(
        "signal_lab",
        lambda _ctx: SignalLabProvider(),
        models=[
            ModelInfo(id=model, provider="signal_lab", context_window=32768)
            for model in ("qa-clerk", "qa-evolver")
        ],
        api="local",
        open_catalog=False,
        label="Signal Lab (offline demo)",
    )
