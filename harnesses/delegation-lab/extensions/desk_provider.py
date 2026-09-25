"""Deterministic provider for delegation-lab's front desk.

Offline and reproducible; not a builtin. The desk has no tools and no ledger.
It answers from the facts in its own system prompt, and it has to answer the
runtime's routing question when delegation asks which peer, if any, should
take a task. Both answers come from what it is actually shown:

* **Routing.** Delegation sends a selection prompt listing the eligible peers
  with their descriptions and measured fitness. The desk names the peer whose
  description covers the task, or ``none``.
* **Answering.** A question one of its own facts covers is answered from that
  fact. Anything else gets an honest "I can't do that here". The runtime then
  names the peer that could, as a referral, if one is registered.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse

_WORD = re.compile(r"[a-z]{4,}")
_PEER = re.compile(r"^- ([\w.-]+): (.*?) \[", re.MULTILINE)
_FACT = re.compile(r"^- (.+)$", re.MULTILINE)


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in _WORD.findall(text.lower())}


def _task(messages: list[Message]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


class DeskProvider(ModelProvider):
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        prompt = _task(messages)
        if system.startswith("You are routing one task"):
            return self._route(prompt)
        return self._answer(system, prompt)

    def _route(self, prompt: str) -> ModelResponse:
        task = prompt.split("Available harnesses:", 1)[0]
        asked = _words(task)
        best, overlap = "none", 1  # a peer must share at least two words with the task
        for name, description in _PEER.findall(prompt):
            shared = len(asked & _words(description))
            if shared > overlap:
                best, overlap = name, shared
        return text_response(best)

    def _answer(self, system: str, question: str) -> ModelResponse:
        facts = _FACT.findall(system.split("Facts:", 1)[-1])
        asked = _words(question)
        ranked = sorted(facts, key=lambda fact: -len(asked & _words(fact)))
        if ranked and len(asked & _words(ranked[0])) >= 2:
            return text_response(json.dumps({"answer": ranked[0]}))
        return text_response(json.dumps({
            "answer": "I can't answer that from the front desk; it is outside what I know.",
        }))


def hiveloom_extension(hive) -> None:
    hive.register_provider(
        "front_desk",
        lambda _ctx: DeskProvider(),
        models=[ModelInfo(id="desk", provider="front_desk", context_window=32768)],
        api="local",
        open_catalog=False,
        label="Front desk (offline demo)",
    )
