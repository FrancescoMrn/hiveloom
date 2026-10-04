"""The research engine end to end over signal-lab, with a scripted director."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from research_director_script import ScriptedDirector

from hiveloom.errors import SpecError
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider
from hiveloom.research.engine import DirectorSession, Engine
from hiveloom.research.program import Program, init_program
from hiveloom.spec.loader import load_spec

SIGNAL_LAB = Path(__file__).resolve().parent / "fixtures" / "harnesses" / "signal-lab"


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    target = tmp_path / "signal-lab"
    shutil.copytree(SIGNAL_LAB, target, ignore=shutil.ignore_patterns(".hiveloom"))
    return target


def _charter(**overrides) -> dict:
    charter = {
        "goal": "every lookup finds its invoice",
        "holdout": 0.2,
        "levers": ["system_prompt"],
        "budget": {"usd": 1.0, "rounds": 3},
        "stop": {"goal": {"success_rate": 1.0}},
        "execution": {"tools": {"lookup_invoice": "allow"}},
        "models": {"director": "claude/claude-haiku-4-5"},
    }
    charter.update(overrides)
    return charter


def test_a_program_finds_the_fix_keeps_it_confirms_it_and_queues_it(lab: Path):
    live_before = (lab / "harness.yaml").read_text()
    program = init_program(lab, "ids", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    units = [step["unit"] for step in engine.run()]
    assert units == ["baseline", "survey", "hypothesize", "experiment", "interpret",
                     "survey", "confirm", "report"]

    state = program.load_state()
    [experiment] = state["experiments"]
    assert experiment["verdict"] == "confirmed" and experiment["kept"]
    assert state["incumbent"] == experiment["candidate"]
    assert state["stop_reason"]["condition"] == "goal"
    assert state["confirmation"]["ran"] and state["confirmation"]["strength"] in (
        "confirmed", "supported")

    # The live harness is untouched; the program's runs live under their own key.
    assert (lab / "harness.yaml").read_text() == live_before
    assert program.key() != load_spec(lab).identity
    with Hive() as hive:
        proposal = hive.get_proposal(state["promotion"]["proposal_id"])
    assert proposal["trigger"] == "research" and proposal["status"] == "pending"
    assert proposal["harness_name"] == load_spec(lab).identity
    changes = json.loads(proposal["proposal_json"])["yaml_changes"]
    assert [c["path"] for c in changes] == ["system_prompt"]

    assert program.ledger.verify()["ok"]
    kinds = [event["kind"] for event in program.ledger.events()]
    assert kinds.count("sealed_read") == 1
    assert "Proposal `" in (program.root / "report.md").read_text()


def test_a_program_resumes_from_its_files_one_unit_at_a_time(lab: Path):
    init_program(lab, "ids", _charter())
    director = ScriptedDirector()
    for _ in range(20):
        if Program(lab, "ids").load_state()["unit"] == "done":
            break
        Engine(Program(lab, "ids"), director_provider=director).step()
    state = Program(lab, "ids").load_state()
    assert state["status"] == "done" and state["experiments"][0]["kept"]


def test_a_change_that_does_nothing_is_not_kept_and_is_not_retried(lab: Path):
    program = init_program(
        lab, "noop", _charter(stop={"no_progress_rounds": 2}, budget={"usd": 1.0, "rounds": 4})
    )
    director = ScriptedDirector(change="Be polite and concise.")
    engine = Engine(program, director_provider=director)
    engine.run()
    state = program.load_state()
    assert state["stop_reason"]["condition"] == "no_progress"
    assert [e["kept"] for e in state["experiments"]] == [False]
    assert state["experiments"][0]["verdict"] in ("refuted", "inconclusive")
    assert state["experiments"][0]["stopped_early"] == "futile"  # 7 of 10 cells spent
    assert state["incumbent"] == "c0"
    # Round two re-registers the same idea, and it is refused.
    assert any("already futile" in refusal for refusal in director.refusals)
    assert state["confirmation"]["ran"] is False
    assert state["promotion"] is None


def test_the_director_can_stop_the_program(lab: Path):
    program = init_program(lab, "halt", _charter(stop={}))
    engine = Engine(program, director_provider=ScriptedDirector(decision="stop"))
    engine.run()
    state = program.load_state()
    assert state["round"] == 1
    assert state["stop_reason"]["condition"] == "director"


def test_a_user_stop_is_honoured_at_the_next_unit(lab: Path):
    program = init_program(lab, "user", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.step()  # baseline
    engine.request_stop("enough")
    engine.run()
    state = program.load_state()
    assert state["stop_reason"]["condition"] == "user"
    assert state["experiments"] == [] and state["status"] == "done"


def test_director_tools_refuse_what_the_charter_does_not_allow(lab: Path):
    program = init_program(lab, "tools", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")  # baseline
    engine.run(until="unit")  # survey → round 1
    state = program.load_state()
    session = DirectorSession(engine, state, "hypothesize")

    outside = json.loads(session.call("register_hypothesis", {
        "claim": "more turns", "levers": ["loop.max_turns"], "target": "success_rate",
        "expect": "increase", "falsifier": "no change"}))
    assert "outside the charter" in outside["refused"]
    unknown = json.loads(session.call("register_hypothesis", {
        "claim": "x", "levers": ["system_prompt"], "target": "tool_error:nothing",
        "expect": "decrease", "falsifier": "y"}))
    assert "not in the signal map" in unknown["refused"]
    ok = json.loads(session.call("register_hypothesis", {
        "claim": "x", "levers": ["system_prompt"], "target": "success_rate",
        "expect": "increase", "falsifier": "y"}))
    assert ok == {"registered": "h1"}
    lever = json.loads(session.call("design_experiment", {
        "hypothesis_id": "h1", "changes": [{"path": "loop.max_turns", "value": 3}]}))
    assert "outside the charter's levers" in lever["refused"]
    change = {"path": "system_prompt", "value": "Answer in JSON."}
    assert json.loads(session.call("design_experiment", {
        "hypothesis_id": "h1", "changes": [change]}))["candidate"] == "c1"
    again = json.loads(session.call("design_experiment", {
        "hypothesis_id": "h1", "changes": [change]}))
    assert "already tested as c1" in again["refused"]
    phase = json.loads(session.call("interpret", {
        "findings": [], "next_focus": [], "decision": "stop"}))
    assert "not available in the hypothesize phase" in phase["refused"]
    confirmation = json.loads(session.call("move_budget", {
        "source": "confirmation", "target": "experiments", "usd": 0.1}))
    assert "refused" in confirmation
    brief = json.loads(session.call("brief", {}))
    assert "holdout" not in json.dumps(brief["signal"])  # nothing sealed reaches it
    assert brief["power_plan"] and brief["levers"] == ["system_prompt"]


def test_a_program_refuses_to_start_on_levers_outside_evolution_or_unclassified_tools(lab):
    with pytest.raises(SpecError, match="frozen path"):
        init_program(lab, "bad", _charter(levers=["guardrails"]))
    with pytest.raises(SpecError, match="lookup_invoice"):
        init_program(lab, "bad", _charter(execution={}))
    with pytest.raises(SpecError, match="already exists"):
        init_program(lab, "ok", _charter())
        init_program(lab, "ok", _charter())


def test_the_ceiling_stops_a_program_whose_failures_no_lever_can_reach(lab: Path):
    from types import SimpleNamespace

    program = init_program(lab, "ceiling", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    state = program.load_state()

    def signal_map(content_share, signals=(), mechanisms=()):
        return SimpleNamespace(
            quality=SimpleNamespace(failures=6),
            loss=SimpleNamespace(content_share=content_share),
            signals=list(signals), mechanisms=list(mechanisms),
        )

    reachable = SimpleNamespace(addressable=True, strength="strong", direction="risk")
    engine._signal = lambda _state: signal_map(0.9)
    assert engine._ceiling(state) is None  # not before a round has tried and failed
    state["rounds_without_progress"] = 1
    ceiling = engine._ceiling(state)
    assert ceiling is not None and "stronger executor model" in ceiling["recommendation"]
    engine._signal = lambda _state: signal_map(0.9, signals=[reachable])
    assert engine._ceiling(state) is None
    engine._signal = lambda _state: signal_map(0.5)
    assert engine._ceiling(state) is None


FLAKY_EXTENSION = '''
import json
from hiveloom import EvalCase, RunMetric, ScorerOutput
from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider


class Worker(ModelProvider):
    """Calls fetch; after a refused call, calls again only when told to retry."""

    def complete(self, *, system, messages, tools, config):
        results = [b for m in messages if isinstance(m.get("content"), list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        if not results:
            return tool_response("fetch", {}, call_id="f1")
        if results[-1].get("is_error"):
            if len(results) == 1 and "retry" in system.lower():
                return tool_response("fetch", {}, call_id="f2")
            return text_response("fail")
        return text_response("ok")


class Cases:
    def load(self):
        return [EvalCase(id=f"case-{i:02d}", input=f"fetch item {i}", expected={})
                for i in range(1, 13)]


def score(context):
    return ScorerOutput(metrics=[RunMetric(
        run_id=context.run_result.run_id, name="ok", source="flaky", scope="case",
        value=float(context.run_result.output == "ok"))])


def hiveloom_extension(hive):
    hive.register_provider("flaky", lambda _ctx: Worker(),
                           models=[ModelInfo(id="worker", provider="flaky")],
                           api="local", open_catalog=False)
    hive.register_dataset("flaky_cases", lambda _p, _c: Cases(), description="twelve fetches")
    hive.register_scorer("flaky_ok", lambda _p, _c: score, description="ok or not")
'''

FLAKY_TOOL = '''
from hiveloom.tools import tool
from hiveloom.tools.registry import ToolError

_CALLS = {}


@tool(description="Fetch the item; the service refuses the first call for most items.")
def fetch(run_context=None):
    context = run_context or {}
    key = context.get("run_id")
    _CALLS[key] = _CALLS.get(key, 0) + 1
    item = int(str(context.get("input", "0")).rsplit(" ", 1)[-1])
    if item % 3 and _CALLS[key] == 1:
        raise ToolError("service busy (503)")
    return "item"
'''


def test_a_gain_whose_predicted_mechanism_did_not_move_is_kept_as_improved(
    tmp_path: Path, monkeypatch
):
    from hiveloom import construct

    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    flaky = tmp_path / "flaky"
    construct.init_harness(flaky, name="flaky", task="Fetch the item and say ok.")
    (flaky / "extensions").mkdir()
    (flaky / "extensions" / "flaky.py").write_text(FLAKY_EXTENSION)
    (flaky / "tools" / "fetch.py").write_text(FLAKY_TOOL)
    construct.set_value(flaky, "extensions", ["extensions/flaky.py"])
    construct.set_value(flaky, "model", {"provider": "flaky", "id": "worker"})
    construct.set_value(flaky, "loop.on_tool_error", "surface_to_model")
    construct.add_tool(flaky, code="tools/fetch.py:fetch", description="Fetch the item.")
    construct.add_validator(flaky, builtin="regex_match", pattern="^ok$")
    (flaky / "eval.yaml").write_text(
        "schema_version: 1\nharness: .\nextensions:\n  - extensions/flaky.py\n"
        "dataset:\n  loader: flaky_cases\nscorers:\n  - flaky_ok\nmodel_identity: warn\n"
    )
    program = init_program(flaky, "retry", _charter(
        execution={"tools": {"fetch": "allow"}}, holdout=0.2,
    ))
    # The director aims at the tool-error signal; retrying clears the failure
    # but not the error, which every run still logs once.
    director = ScriptedDirector(change="If fetch is refused, retry it once.")
    Engine(program, director_provider=director).run()
    state = program.load_state()
    [experiment] = state["experiments"]
    assert experiment["verdict"] == "improved" and experiment["kept"]
    assert experiment["measured_effect"] == 0.0 and experiment["success_gain"] > 0.5
    assert state["calibration"] == [] or state["calibration"][0]["measured"] == 0.0
    assert state["stop_reason"]["condition"] == "goal"


def test_rounds_budget_and_time_each_stop_a_program(lab: Path):
    from datetime import UTC, datetime, timedelta

    program = init_program(lab, "limits", _charter(stop={}))
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")  # baseline
    state = program.load_state()

    state["round"] = program.charter.budget.rounds
    assert engine._stop_condition(state)["condition"] == "rounds"
    state["round"] = 0

    started = datetime.fromisoformat(state["started_at"])
    state["started_at"] = (
        started - timedelta(minutes=program.charter.budget.wall_clock_minutes + 1)
    ).isoformat()
    assert engine._stop_condition(state)["condition"] == "time"
    state["started_at"] = datetime.now(UTC).isoformat()

    program.budget.debit("exploration", program.budget.left("exploration"), reason="test")
    assert engine._stop_condition(state)["condition"] == "budget"


def test_a_later_program_is_briefed_on_what_earlier_ones_learned(lab: Path):
    from hiveloom.research.engine import DirectorSession

    Engine(init_program(lab, "first", _charter()), director_provider=ScriptedDirector()).run()
    second = init_program(lab, "second", _charter())
    engine = Engine(second, director_provider=ScriptedDirector())
    engine.run(until="unit")
    engine.run(until="unit")
    brief = json.loads(DirectorSession(engine, second.load_state(), "hypothesize")
                       .call("brief", {}))
    [earlier] = brief["earlier_programs"]
    assert earlier["program"] == "first" and earlier["stopped"]["condition"] == "goal"
    assert earlier["kept"] == ["lookups fail because ids are not normalized"]


TWO_RULES_EXTENSION = '''
from hiveloom import EvalCase, RunMetric, ScorerOutput
from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response
from hiveloom.models.provider import ModelProvider


class Worker(ModelProvider):
    """Items tagged alpha need rule-a, items tagged beta need rule-b."""

    def complete(self, *, system, messages, tools, config):
        task = next(m["content"] for m in messages if m["role"] == "user")
        if "alpha" in task and "rule-a" not in system:
            return text_response("fail")
        if "beta" in task and "rule-b" not in system:
            return text_response("fail")
        return text_response("ok")


class Cases:
    def load(self):
        return [EvalCase(id=f"case-{i:02d}", input=f"item {i} {'alpha' if i % 2 else 'beta'}",
                         expected={}) for i in range(1, 25)]


def score(context):
    return ScorerOutput(metrics=[RunMetric(
        run_id=context.run_result.run_id, name="ok", source="two", scope="case",
        value=float(context.run_result.output == "ok"))])


def hiveloom_extension(hive):
    hive.register_provider("two", lambda _ctx: Worker(),
                           models=[ModelInfo(id="worker", provider="two")],
                           api="local", open_catalog=False)
    hive.register_dataset("two_cases", lambda _p, _c: Cases(), description="two kinds")
    hive.register_scorer("two_ok", lambda _p, _c: score, description="ok or not")
'''


class TwoFixDirector(ModelProvider):
    """Round one: two hypotheses, one rule each, both against the same incumbent."""

    def complete(self, *, system, messages, tools, config):
        from research_director_script import _task, _tool_results

        results = _tool_results(messages)
        if "hypothesize phase" not in _task(messages):
            if not results:
                return tool_response("brief", {}, call_id="b")
            if len(results) == 1:
                return tool_response("interpret", {"findings": [], "next_focus": [],
                                                   "decision": "stop",
                                                   "stop_reason": "done"}, call_id="i")
            return text_response("ok")
        steps = [
            ("brief", {}),
            ("register_hypothesis", {"claim": "alpha items need rule-a", "levers":
                                     ["system_prompt"], "target": "success_rate",
                                     "expect": "increase", "falsifier": "no change"}),
            ("register_hypothesis", {"claim": "beta items need rule-b", "levers":
                                     ["system_prompt"], "target": "success_rate",
                                     "expect": "increase", "falsifier": "no change"}),
        ]
        if len(results) < len(steps):
            name, args = steps[len(results)]
            return tool_response(name, args, call_id=f"s{len(results)}")
        prompt = json.loads(results[0])["incumbent"]["spec"]["system_prompt"]
        if len(results) == 3:
            return tool_response("design_experiment", {"hypothesis_id": "h1", "changes": [
                {"path": "system_prompt", "value": prompt + "\nrule-a"}]}, call_id="d1")
        if len(results) == 4:
            return tool_response("design_experiment", {"hypothesis_id": "h2", "changes": [
                {"path": "system_prompt", "value": prompt + "\nrule-b"}]}, call_id="d2")
        return text_response("designed")


def test_a_second_confirmed_fix_of_the_same_round_is_stacked_not_lost(tmp_path, monkeypatch):
    from hiveloom import construct

    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    two = tmp_path / "two"
    construct.init_harness(two, name="two", task="Answer ok.")
    (two / "extensions").mkdir()
    (two / "extensions" / "two.py").write_text(TWO_RULES_EXTENSION)
    construct.set_value(two, "extensions", ["extensions/two.py"])
    construct.set_value(two, "model", {"provider": "two", "id": "worker"})
    construct.add_validator(two, builtin="regex_match", pattern="^ok$")
    (two / "eval.yaml").write_text(
        "schema_version: 1\nharness: .\nextensions:\n  - extensions/two.py\n"
        "dataset:\n  loader: two_cases\nscorers:\n  - two_ok\nmodel_identity: warn\n"
    )
    program = init_program(two, "both", _charter(execution={}, holdout=0.25, stop={}))
    Engine(program, director_provider=TwoFixDirector()).run()
    state = program.load_state()
    by_id = {e["id"]: e for e in state["experiments"]}
    kept = [e for e in state["experiments"] if e["kept"]]
    assert len(kept) == 2, [(e["id"], e["verdict"], e["kept"]) for e in state["experiments"]]
    stacked = next(e for e in kept if e.get("stacked_from"))
    assert by_id[stacked["stacked_from"]]["verdict"] == "confirmed"
    final = load_spec(program.candidate_dir(state["incumbent"])).system_prompt
    assert "rule-a" in final and "rule-b" in final
    assert state["confirmation"]["strength"] in ("confirmed", "supported")


def test_three_way_merges_combine_what_each_winner_added():
    from hiveloom.research.engine import _CONFLICT, _merge3, _merge_entries

    base = "You are the desk.\nAnswer in JSON.\n"
    # Two rules appended at the same point: both kept, the incumbent's first.
    assert _merge3(base, base + "rule-a\n", base + "rule-b\n") == base + "rule-a\nrule-b\n"
    # Both re-wrapped the same paragraph, then each added a section.
    wrapped = "You are the desk. Answer in JSON."
    merged = _merge3(base, wrapped + "\n\nZones are upper case.",
                     wrapped + "\n\nConvert EUR with eur_per_usd.")
    assert "Zones are upper case." in merged and "Convert EUR" in merged
    # Both rewrote the same sentence differently: a real conflict.
    assert _merge3(base, "You are the clerk.\nAnswer in JSON.\n",
                   "You are the agent.\nAnswer in JSON.\n") is None
    # Lessons merge by id; one lesson changed two ways conflicts.
    one, two = {"id": "a", "content": "x"}, {"id": "b", "content": "y"}
    assert _merge_entries([], [one], [two]) == [one, two]
    assert _merge_entries([one], [{"id": "a", "content": "p"}],
                          [{"id": "a", "content": "q"}]) is _CONFLICT


def test_the_brief_names_confirmed_fixes_that_are_not_in_the_incumbent():
    from types import SimpleNamespace

    state = {
        "incumbent": "c2",
        "candidates": {"c0": {"parent": None}, "c1": {"parent": "c0"}, "c2": {"parent": "c0"}},
        "hypotheses": [{"id": "h1", "claim": "zones are upper case"},
                       {"id": "h2", "claim": "convert EUR"}],
        "experiments": [
            {"id": "e1", "hypothesis": "h1", "candidate": "c1", "verdict": "confirmed",
             "kept": False, "changes": []},
            {"id": "e2", "hypothesis": "h2", "candidate": "c2", "verdict": "confirmed",
             "kept": True, "changes": []},
        ],
    }
    [row] = DirectorSession._unmerged(SimpleNamespace(state=state))
    assert row["experiment"] == "e1" and row["claim"] == "zones are upper case"
    state["experiments"].append({"id": "e3", "hypothesis": "h1", "candidate": "c3",
                                 "verdict": "confirmed", "kept": True, "stacked_from": "e1",
                                 "changes": []})
    assert DirectorSession._unmerged(SimpleNamespace(state=state)) == []


def test_research_changes_name_the_harness_objectives_so_the_gate_accepts_them():
    """A harness with `evolution.objectives` refused every research experiment.

    The engine built proposals without objective expectations; the gate
    requires one for such a harness, so a program on ranked-retrieval designed
    nothing and spent its rounds probing the refusal.
    """
    from types import SimpleNamespace

    from hiveloom.evolve.evolver import MutationProposal, gate
    from hiveloom.research.engine import Engine
    from hiveloom.spec.loader import load_spec

    spec = load_spec(Path(__file__).resolve().parents[1] / "harnesses" / "ranked-retrieval")
    assert spec.evolution.objectives, "the fixture must declare metric objectives"
    expect = Engine._objective_expectations

    def owner(goal: dict) -> SimpleNamespace:
        return SimpleNamespace(charter=SimpleNamespace(stop=SimpleNamespace(goal=goal)))

    # The hypothesis's own target wins when it is an objective…
    named = expect(owner({}), spec, "metric:ndcg_at_3")
    assert [e["metric"] for e in named] == ["ndcg_at_3"]
    # …else the charter's goal metrics, else every objective.
    goal = expect(owner({"metric:recall_at_3": 0.9}), spec, "tool_error:search")
    assert [e["metric"] for e in goal] == ["recall_at_3"]
    every = expect(owner({}), spec, "success_rate")
    assert {e["metric"] for e in every} == {o.metric for o in spec.evolution.objectives}
    # Directions come from the objectives, never guessed.
    assert {e["metric"]: e["expected_change"] for e in every}["hallucination_rate"] == "decrease"

    proposal = MutationProposal.model_validate({
        "rationale": "rephrase the query in the knowledge base's terms",
        "target": {"signal": "metric:recall_at_3", "expect": "increase"},
        "objective_expectations": goal,
        "yaml_changes": [{"path": "system_prompt", "value": spec.system_prompt + "\\nMore."}],
    })
    assert not gate(spec, proposal).rejected
