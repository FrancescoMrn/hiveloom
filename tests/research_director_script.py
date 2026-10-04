"""A scripted director for tests: reads brief() and aims at the top risk signal."""

from __future__ import annotations

import json
from typing import Any

from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider, ModelResponse

UPPERCASE_RULE = "Invoice ids are uppercase in the ledger: uppercase the invoice id first."


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    found = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = block.get("content", "")
                    if isinstance(body, list):
                        body = "".join(str(b.get("text", "")) for b in body)
                    found.append(str(body))
    return found


def _task(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


class ScriptedDirector(ModelProvider):
    """Hypothesize: brief → register → design. Interpret: brief → interpret."""

    def __init__(self, change: str = UPPERCASE_RULE, decision: str = "continue"):
        self.change = change
        self.decision = decision
        self.refusals: list[str] = []

    def complete(self, *, system, messages, tools, config) -> ModelResponse:
        results = _tool_results(messages)
        for body in results:
            if body.startswith('{"refused"'):
                self.refusals.append(body)
        step = len(results)
        if "hypothesize phase" in _task(messages):
            if step == 0:
                return tool_response("brief", {}, call_id="d1")
            brief = json.loads(results[0])
            if step == 1:
                risks = [s["feature"] for s in brief["signal"]["signals"]
                         if s["direction"] == "risk"]
                target = risks[0] if risks else "success_rate"
                return tool_response("register_hypothesis", {
                    "claim": "lookups fail because ids are not normalized",
                    "levers": ["system_prompt"], "target": target,
                    "expect": "increase" if target == "success_rate" else "decrease",
                    "falsifier": "the failing lookups keep failing", "prior": 0.7,
                }, call_id="d2")
            if step == 2:
                registered = json.loads(results[1]).get("registered")
                prompt = brief["incumbent"]["spec"]["system_prompt"]
                return tool_response("design_experiment", {
                    "hypothesis_id": registered,
                    "changes": [{"path": "system_prompt",
                                 "value": prompt + "\n\n" + self.change}],
                }, call_id="d3")
            return text_response("designed one experiment")
        if step == 0:
            return tool_response("brief", {}, call_id="i1")
        if step == 1:
            return tool_response("interpret", {
                "findings": ["normalizing ids is the lever"], "next_focus": [],
                "decision": self.decision,
            }, call_id="i2")
        return text_response("interpreted")
