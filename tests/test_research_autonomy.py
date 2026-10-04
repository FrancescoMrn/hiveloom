"""Unattended for many rounds: a bad director must not be able to hurt the harness.

The properties an autonomous evolve loop owes the person who is not watching:
it keeps nothing that makes things worse, it stops on its own when it stops
making progress, it never spends past its budget, and when it keeps nothing it
promotes nothing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from research_director_script import _task, _tool_results

from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider
from hiveloom.research.engine import Engine
from hiveloom.research.program import init_program

RESEARCH_LAB = Path(__file__).resolve().parent / "fixtures" / "harnesses" / "research-lab"


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    target = tmp_path / "research-lab"
    shutil.copytree(RESEARCH_LAB, target, ignore=shutil.ignore_patterns(".hiveloom"))
    return target


class _Director(ModelProvider):
    """Designs one change per round from ``idea(brief, round)``; interprets 'continue'."""

    def __init__(self, idea):
        self.idea = idea
        self.rounds = 0

    def complete(self, *, system, messages, tools, config):
        results = _tool_results(messages)
        if "hypothesize phase" not in _task(messages):
            if not results:
                return tool_response("brief", {}, call_id="b")
            if len(results) == 1:
                return tool_response("interpret", {"findings": ["keep going"], "next_focus": [],
                                                   "decision": "continue"}, call_id="i")
            return text_response("ok")
        if not results:
            self.rounds += 1
            return tool_response("brief", {}, call_id="b")
        brief = json.loads(results[0])
        if len(results) == 1:
            return tool_response("runs", {"status": "success", "limit": 25}, call_id="r")
        passing = [run["task"] for run in json.loads(results[1]).get("runs", [])]
        if len(results) == 2:
            return tool_response("register_hypothesis", {
                "claim": f"idea number {self.rounds}", "levers": ["system_prompt"],
                "target": "success_rate", "expect": "increase", "falsifier": "none"},
                call_id="h")
        if len(results) == 3 and "registered" in json.loads(results[2]):
            prompt = brief["incumbent"]["spec"]["system_prompt"].rstrip()
            return tool_response("design_experiment", {
                "hypothesis_id": json.loads(results[2])["registered"],
                "changes": [{"path": "system_prompt",
                             "value": prompt + "\n" + self.idea(passing, self.rounds)}]},
                call_id="d")
        return text_response("done")


def _saboteur(passing, round_number):
    # Made-up answers memorized for the requests that pass today: billing
    # rejects every one — a change that looks busy and makes things worse.
    return "\n".join(f"KNOWN: {task} => 1.0{round_number}" for task in passing)


def _wanderer(_passing, round_number):
    return f"Be courteous (variant {round_number})."


def _charter(**overrides) -> dict:
    charter = {
        "goal": "Every quote is what billing will invoice.", "holdout": 0.25,
        "levers": ["system_prompt"], "budget": {"usd": 1.0, "rounds": 10},
        "stop": {"no_progress_rounds": 2}, "execution": {"tools": {"rate_quote": "allow"}},
        "models": {"director": "claude/claude-haiku-4-5"},
    }
    charter.update(overrides)
    return charter


@pytest.mark.parametrize("idea", [_saboteur, _wanderer], ids=["saboteur", "wanderer"])
def test_an_unattended_program_with_a_bad_director_keeps_and_promotes_nothing(lab, idea):
    program = init_program(lab, "bad", _charter())
    director = _Director(idea)
    Engine(program, director_provider=director).run()
    state = program.load_state()

    assert state["status"] == "done"
    assert state["stop_reason"]["condition"] in ("no_progress", "ceiling")
    assert state["round"] <= 3  # stopped on its own, long before its 10 rounds
    assert not any(e["kept"] for e in state["experiments"])
    assert state["incumbent"] == "c0" and state["promotion"] is None
    if idea is _saboteur:
        # Every sabotage measured as harm (on a sample this small the label may
        # stay "inconclusive" — five worsened pairs are p = 0.06 — but it is
        # never kept, which is what an unwatched loop owes).
        assert state["experiments"] and all(e["measured_effect"] < 0 or
                                            e["success_gain"] < 0
                                            for e in state["experiments"])
    for pool in program.budget.pools().values():
        assert pool.spent <= pool.size + 1e-9
    assert program.ledger.verify()["ok"]


def test_a_good_idea_after_bad_ones_is_still_kept_and_nothing_bad_rides_along(lab):
    from research_director_script import UPPERCASE_RULE  # noqa: F401 - documents the pattern

    ideas = iter(["Be courteous.", "Requests may give the weight in grams (\"1200 g\"); "
                  "rate_quote takes kilograms, so convert grams to kilograms (divide by 1000)."])
    program = init_program(lab, "late", _charter(stop={"no_progress_rounds": 3}))
    Engine(program, director_provider=_Director(lambda *_: next(ideas, "Be brief."))).run()
    state = program.load_state()
    kept = [e for e in state["experiments"] if e["kept"]]
    assert len(kept) == 1 and kept[0]["round"] == 2
    from hiveloom.spec.loader import load_spec

    final = load_spec(program.candidate_dir(state["incumbent"])).system_prompt
    assert "grams" in final and "Be courteous." not in final
