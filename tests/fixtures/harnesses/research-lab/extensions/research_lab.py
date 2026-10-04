"""Deterministic models for the research-lab demo: the desk clerk and a director.

Scoped to this harness through ``extensions`` so the walkthrough is offline and
reproducible; it is not a runtime builtin. Neither script carries the answer:

* ``desk-clerk``, the executor, reads what its context says. It converts a
  weight written in grams to kilograms only when its system prompt tells it
  to, and it quotes the list price the rate table returned.
* ``director`` reads the program the way a model would, through its tools. It
  aims at the strongest addressable risk signal in ``brief()``, reads the
  failing runs behind it, and forms its hypotheses from what those runs'
  requests have in common — alongside a cheaper, more generic idea, so the
  engine has something to refute. It interprets only the verdicts the engine
  measured, and stops when nothing in the charter can reach what is left.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import Message, ModelConfig, ModelProvider, ModelResponse

_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g)\b")
_ZONE = re.compile(r"zone (\d+)")
_KNOWN = re.compile(r"^KNOWN: (.+?) => ([0-9.]+)\s*$", re.MULTILINE)
_GRAMS_RULE = re.compile(r"grams?\b[^\n]*(kilograms|kg)|(kilograms|kg)[^\n]*grams?\b", re.I)

GRAMS_RULE = (
    "Requests may give the weight in grams (\"1200 g\"); rate_quote takes kilograms, so "
    "convert grams to kilograms (divide by 1000) before calling it."
)


def _task(messages: list[Message]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _results(messages: list[Message]) -> list[tuple[str, bool]]:
    """Every tool result so far, in order: (content, is_error)."""
    found: list[tuple[str, bool]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = "".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                found.append((str(body), bool(block.get("is_error"))))
    return found


def _share(signal: dict[str, Any]) -> float:
    runs = sum(signal[k] for k in (
        "failures_with", "successes_with", "failures_without", "successes_without"))
    return (signal["failures_with"] + signal["successes_with"]) / runs if runs else 0.0


class ResearchLabProvider(ModelProvider):
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        config: ModelConfig,
    ) -> ModelResponse:
        if config.id == "desk-clerk":
            return self._clerk(system, messages)
        if config.id == "director":
            return self._director(messages)
        if config.id == "examiner":
            return self._examiner(_task(messages))
        if config.id in ("judge-a", "judge-b"):
            return self._judge(_task(messages))
        return text_response("unsupported research-lab model")

    # ------------------------------------------------------------------ #
    # The executor
    # ------------------------------------------------------------------ #
    def _clerk(self, system: str, messages: list[Message]) -> ModelResponse:
        task = _task(messages)
        # Like a small model, the clerk copies an answer its prompt already holds:
        # a line `KNOWN: <request> => <price>` is answered from memory, no lookup.
        for request, price in _KNOWN.findall(system):
            if request.strip() == task.strip():
                weight, zone = _WEIGHT.search(task), _ZONE.search(task)
                return text_response(json.dumps({
                    "zone": int(zone.group(1)) if zone else None,
                    "weight_kg": float(weight.group(1)) if weight else None,
                    "price": float(price),
                }))
        weight, zone = _WEIGHT.search(task), _ZONE.search(task)
        if not (weight and zone):
            return text_response(json.dumps({"price": None, "error": "unreadable request"}))
        value = float(weight.group(1))
        if weight.group(2) == "g" and _GRAMS_RULE.search(system):
            value = value / 1000
        results = _results(messages)
        if not results:
            return tool_response(
                "rate_quote", {"zone": int(zone.group(1)), "weight_kg": value}, call_id="q1"
            )
        body, is_error = results[-1]
        if is_error:
            return text_response(json.dumps({"price": None, "error": body[:200]}))
        quote = json.loads(body)
        return text_response(json.dumps({
            "zone": quote["zone"], "weight_kg": quote["weight_kg"], "price": quote["list_price"],
        }))

    # ------------------------------------------------------------------ #
    # The director
    # ------------------------------------------------------------------ #
    def _director(self, messages: list[Message]) -> ModelResponse:
        task = _task(messages)
        results = [json.loads(body) for body, _ in _results(messages)]
        if not results:
            return tool_response("brief", {}, call_id="brief")
        brief = results[0]
        if brief.get("phase") == "frame":
            return self._frame(brief, results[1:])
        if "hypothesize phase" in task:
            return self._hypothesize(brief, results[1:])
        return self._interpret(brief, results[1:])

    def _hypothesize(self, brief: dict[str, Any], done: list[dict[str, Any]]) -> ModelResponse:
        risks = [
            s for s in brief["signal"]["signals"]
            if s["direction"] == "risk" and s["addressable"] and s["strength"] != "weak"
        ]
        if not risks:
            return text_response("No addressable risk signal is left; nothing to test.")
        target = risks[0]["feature"]
        step = len(done)
        if step == 0:
            return tool_response("runs", {"feature": target, "limit": 25}, call_id="evidence")
        evidence = done[0]
        tasks = [run["task"] for run in evidence.get("runs", [])]
        in_grams = [t for t in tasks if (m := _WEIGHT.search(t)) and m.group(2) == "g"]
        if step == 1:
            if len(in_grams) * 2 < len(tasks):
                return text_response(f"The runs behind {target} share nothing I can name.")
            return tool_response("register_hypothesis", {
                "claim": (
                    f"{len(in_grams)} of {len(tasks)} runs with {target} write the weight in "
                    "grams, and the clerk passes the figure to rate_quote as kilograms"
                ),
                "levers": ["system_prompt"],
                "target": target,
                "expect": "decrease",
                # Predicts the share of runs that carry the signal, today all of them.
                "by": round(_share(risks[0]), 2),
                "prior": 0.8,
                "falsifier": f"{target} does not fall once the prompt says to convert grams",
            }, call_id="h-grams")
        if step == 2:
            return tool_response("register_hypothesis", {
                "claim": "the clerk gives up after one failed lookup; more turns let it recover",
                "levers": ["loop.max_turns"],
                "target": target,
                "expect": "decrease",
                "prior": 0.2,
                "falsifier": f"{target} is unchanged with twice the turns",
            }, call_id="h-turns")
        if step == 3:
            prompt = brief["incumbent"]["spec"]["system_prompt"].rstrip()
            return tool_response("design_experiment", {
                "hypothesis_id": done[1]["registered"],
                "changes": [{"path": "system_prompt", "value": f"{prompt}\n\n{GRAMS_RULE}\n"}],
            }, call_id="e-grams")
        if step == 4 and "registered" in done[2]:
            turns = brief["incumbent"]["spec"]["loop"]["max_turns"]
            return tool_response("design_experiment", {
                "hypothesis_id": done[2]["registered"],
                "changes": [{"path": "loop.max_turns", "value": turns * 2}],
            }, call_id="e-turns")
        return text_response("Designed the experiments; the engine will measure them.")

    def _interpret(self, brief: dict[str, Any], done: list[dict[str, Any]]) -> ModelResponse:
        if done:
            return text_response("Round interpreted.")
        this_round = {h["id"] for h in brief["hypotheses"] if h["round"] == brief["round"]}
        verdicts = [e for e in brief["experiments"] if e["hypothesis"] in this_round]
        findings = [
            f"{e['hypothesis']} was {e['verdict']}"
            + (" and kept" if e["kept"] else "")
            + (f" (stopped early: {e['stopped_early']})" if e["stopped_early"] else "")
            + f": {e['target_measure']}"
            for e in verdicts
        ]
        loss = brief["signal"]["loss"]
        if not verdicts:
            # Nothing to test; the engine decides whether that is a ceiling.
            return tool_response("interpret", {
                "findings": [f"{loss['content_share']:.0%} of what fails now is content, "
                             "and no signal points at a lever"],
                "next_focus": [],
                "decision": "continue",
            }, call_id="close")
        return tool_response("interpret", {
            "findings": findings,
            "next_focus": ["what the remaining failures share"],
            "decision": "continue",
        }, call_id="close")


    # ------------------------------------------------------------------ #
    # Concepts mode: framing, the examiner, the judges
    # ------------------------------------------------------------------ #
    def _frame(self, brief: dict[str, Any], done: list[dict[str, Any]]) -> ModelResponse:
        """Read the concepts, draft a contract, write cases from the seeds' format."""
        concepts = brief["concepts"].lower()
        if not done:
            criteria = []
            if "price" in concepts and ("never null" in concepts or "gets a" in concepts):
                criteria.append({"id": "quoted", "says": "Every request gets a quoted price",
                                 "check": {"kind": "json_present", "field": "price"}})
            if "json" in concepts:
                criteria.append({"id": "one-json", "says": "The answer is one JSON object "
                                 "with zone, weight_kg and price, and nothing else",
                                 "check": {"kind": "judge", "rubric": (
                                     "Pass only if the whole answer is a single JSON object "
                                     "with the keys zone, weight_kg and price and no other "
                                     "keys or text.")}})
            return tool_response("propose_contract", {
                "criteria": criteria,
                "goal_thresholds": {c["id"]: 0.9 for c in criteria},
            }, call_id="contract")
        if len(done) == 1:
            seeds = [c["input"] for c in brief["cases"]["examples"]]
            return tool_response("add_cases", {"cases": [
                {"input": text, "criterion": criterion}
                for text, criterion in _variations(seeds, 14)
            ]}, call_id="cases")
        return text_response("Drafted the contract and the working cases.")

    def _examiner(self, prompt: str) -> ModelResponse:
        """Sealed cases from the concepts and the contract: the hard ones, both units."""
        cases = []
        for criterion in ("quoted", "one-json"):
            if f'"{criterion}"' not in prompt:
                continue
            for weight, zone in (("2750 g", 3), ("0.9 kg", 1), ("4400 g", 2), ("6 kg", 4)):
                cases.append({"criterion": criterion,
                              "input": f"Quote shipping for a {weight} parcel to zone {zone} "
                                       f"for customer C-10{zone}, please ({criterion})."})
        return text_response(json.dumps(cases))

    def _judge(self, prompt: str) -> ModelResponse:
        answer = prompt.split("<answer>\n", 1)[-1].split("\n</answer>", 1)[0]
        try:
            data = json.loads(answer)
        except json.JSONDecodeError:
            data = None
        ok = isinstance(data, dict) and set(data) == {"zone", "weight_kg", "price"}
        return text_response(json.dumps({"verdict": "pass" if ok else "fail",
                                         "reason": "one JSON object" if ok else "extra or "
                                         "missing keys"}))


def _variations(seeds: list[str], count: int) -> list[tuple[str, str]]:
    """Requests in the seeds' own format: every zone, both units, both criteria."""
    weights = ["1.5 kg", "800 g", "3.2 kg", "2200 g", "0.6 kg", "5100 g", "7 kg"]
    rows = []
    for index in range(count):
        weight = weights[index % len(weights)]
        zone = index % 4 + 1
        customer = ("C-100", "C-101", "C-102")[index % 3]
        criterion = "quoted" if index % 2 == 0 else "one-json"
        rows.append((f"Quote shipping for a {weight} parcel to zone {zone} for customer "
                     f"{customer}.", criterion))
    del seeds  # the format is fixed for this desk; a real director would read it
    return rows


def hiveloom_extension(hive) -> None:
    hive.register_provider(
        "research_lab",
        lambda _ctx: ResearchLabProvider(),
        models=[
            ModelInfo(id=model, provider="research_lab", context_window=32768)
            for model in ("desk-clerk", "director", "examiner", "judge-a", "judge-b")
        ],
        api="local",
        open_catalog=False,
        label="Research Lab (offline demo)",
    )
