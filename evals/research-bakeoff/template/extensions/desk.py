"""The planted-defect desk: a scripted executor with fixable bad habits.

``clerk`` reads its request and its system prompt (lessons in memory render
into it) the way a small model would, and has four habits, each undone only
by a rule that says so in plain words, however it is phrased:

* **unit** — a weight in grams is passed on as kilograms, unless a rule
  mentions grams and kilograms (or dividing by 1000);
* **case** — a zone code is passed as written (``z3``), unless a rule says
  zone codes are upper case;
* **retry** — one refused call ends the attempt, unless a rule says to retry
  or call again;
* **currency** — a price asked "in EUR" is reported in USD, unless a rule says
  to convert with the rate the tool returns.

A fifth failure is planted as a decoy: contract customers are invoiced at a
discount only billing knows. No prompt reaches it.

``director-smoke`` and ``evolver-smoke`` are stand-ins for checking the
bake-off's plumbing offline; they are not an arm and prove nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse

_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g)\b")
_ZONE = re.compile(r"zone (z\d)", re.I)
_CUSTOMER = re.compile(r"customer (C-\d+)")

RULES = {
    "unit": lambda s: bool(re.search(r"\bgrams?\b|\(g\)|\" ?g\b", s, re.I))
    and bool(re.search(r"kilogram|\bkg\b|1000|1,000", s, re.I)),
    "case": lambda s: bool(re.search(r"upper\s*-?case|capital", s, re.I))
    and bool(re.search(r"zone", s, re.I)),
    "retry": lambda s: bool(re.search(
        r"\bretry|try again|call (?:it|rate_quote|the tool) again|once more|second (?:call|attempt)"
        r"|call again", s, re.I)),
    "currency": lambda s: bool(re.search(r"\bEUR\b|euro", s, re.I))
    and bool(re.search(r"convert|eur_per_usd|exchange|multiply", s, re.I)),
}


def _task(messages: list[Message]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _results(messages: list[Message]) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = block.get("content", "")
                    if isinstance(body, list):
                        body = "".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                    found.append((str(body), bool(block.get("is_error"))))
    return found


class DeskProvider(ModelProvider):
    def complete(self, *, system: str, messages: list[Message], tools: list[dict[str, Any]],
                 config: ModelConfig) -> ModelResponse:
        if config.id == "clerk":
            return self._clerk(system, messages)
        if config.id == "director-smoke":
            return self._director(messages)
        if config.id == "evolver-smoke":
            return text_response(json.dumps({
                "rationale": "smoke: add a retry rule",
                "target": {"signal": "success_rate", "expect": "increase"},
                "yaml_changes": [{"path": "system_prompt", "value": (
                    _between(_task(messages), "system_prompt: ", "\n") or "") +
                    " If rate_quote refuses a call, retry it once."}],
            }))
        return text_response("unsupported desk model")

    # ------------------------------------------------------------------ #
    def _clerk(self, system: str, messages: list[Message]) -> ModelResponse:
        task = _task(messages)
        weight, zone, customer = _WEIGHT.search(task), _ZONE.search(task), _CUSTOMER.search(task)
        if not (weight and zone and customer):
            return text_response(json.dumps({"price": None, "error": "unreadable request"}))
        kg = float(weight.group(1))
        if weight.group(2) == "g" and RULES["unit"](system):
            kg = kg / 1000
        code = zone.group(1).upper() if RULES["case"](system) else zone.group(1)
        call = {"zone": code, "weight_kg": kg, "customer": customer.group(1)}
        results = _results(messages)
        if not results:
            return tool_response("rate_quote", call, call_id="q1")
        body, is_error = results[-1]
        if is_error:
            if len(results) == 1 and RULES["retry"](system):
                return tool_response("rate_quote", call, call_id="q2")
            return text_response(json.dumps({"price": None, "error": body[:200]}))
        quote = json.loads(body)
        price = quote["list_price_usd"]
        currency = "USD"
        if "in EUR" in task and RULES["currency"](system):
            price, currency = round(price * quote["eur_per_usd"], 2), "EUR"
        return text_response(json.dumps({"zone": code, "price": price, "currency": currency}))

    # ------------------------------------------------------------------ #
    def _director(self, messages: list[Message]) -> ModelResponse:
        task = _task(messages)
        results = [json.loads(body) for body, _ in _results(messages)]
        if not results:
            return tool_response("brief", {}, call_id="b")
        brief = results[0]
        if "interpret phase" in task:
            if len(results) == 1:
                return tool_response("interpret", {
                    "findings": ["smoke"], "next_focus": [], "decision": "continue"}, call_id="i")
            return text_response("done")
        risks = [s for s in brief["signal"]["signals"]
                 if s["direction"] == "risk" and s["addressable"]]
        if not risks:
            return text_response("nothing addressable")
        if len(results) == 1:
            return tool_response("register_hypothesis", {
                "claim": "smoke: the failing lookups need a retry", "levers": ["system_prompt"],
                "target": risks[0]["feature"], "expect": "decrease", "falsifier": "no change",
            }, call_id="h")
        if len(results) == 2 and "registered" in results[1]:
            prompt = brief["incumbent"]["spec"]["system_prompt"].rstrip()
            return tool_response("design_experiment", {
                "hypothesis_id": results[1]["registered"],
                "changes": [{"path": "system_prompt",
                             "value": prompt + "\nIf rate_quote refuses a call, retry it once."}],
            }, call_id="e")
        return text_response("designed")


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    return text.split(start, 1)[1].split(end, 1)[0]


def hiveloom_extension(hive) -> None:
    hive.register_provider(
        "desk",
        lambda _ctx: DeskProvider(),
        models=[ModelInfo(id=m, provider="desk", context_window=32768)
                for m in ("clerk", "director-smoke", "evolver-smoke")],
        api="local",
        open_catalog=False,
        label="Planted-defect desk (research bake-off)",
    )
