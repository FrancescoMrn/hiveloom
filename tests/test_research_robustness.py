"""Research programs under failure: crashes, broken models, a moved live harness, spent budgets."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from research_director_script import ScriptedDirector

from hiveloom import construct
from hiveloom.logging.hive import Hive
from hiveloom.models.provider import ModelProvider
from hiveloom.research.engine import Engine
from hiveloom.research.program import Program, init_program

SIGNAL_LAB = Path(__file__).resolve().parent / "fixtures" / "harnesses" / "signal-lab"
RESEARCH_LAB = Path(__file__).resolve().parent / "fixtures" / "harnesses" / "research-lab"


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    target = tmp_path / "signal-lab"
    shutil.copytree(SIGNAL_LAB, target, ignore=shutil.ignore_patterns(".hiveloom"))
    return target


def _charter(**overrides) -> dict:
    charter = {
        "goal": "every lookup finds its invoice", "holdout": 0.2,
        "levers": ["system_prompt"], "budget": {"usd": 1.0, "rounds": 3},
        "stop": {"goal": {"success_rate": 1.0}},
        "execution": {"tools": {"lookup_invoice": "allow"}},
        "models": {"director": "claude/claude-haiku-4-5"},
    }
    charter.update(overrides)
    return charter


def _debits(program: Program) -> list[dict]:
    return [e["data"] for e in program.ledger.of_kind("debit")]


def test_a_crash_mid_experiment_resumes_without_losing_or_repeating_work(lab: Path):
    program = init_program(lab, "crash", _charter())
    director = ScriptedDirector()
    engine = Engine(program, director_provider=director)
    engine.run(until="unit")  # baseline
    engine.run(until="unit")  # survey
    engine.run(until="unit")  # hypothesize
    assert program.load_state()["unit"] == "experiment"

    calls = {"n": 0}
    real = engine._executor

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("the laptop lid closed")
        return real(**kwargs)

    engine._executor = flaky
    with pytest.raises(KeyboardInterrupt):
        engine.step()
    state = program.load_state()
    assert state["unit"] == "experiment" and state["pending_experiments"]  # nothing lost

    resumed = Engine(Program(lab, "crash"), director_provider=director)
    resumed.run()
    state = program.load_state()
    assert state["status"] == "done" and state["experiments"][0]["kept"]
    assert program.ledger.verify()["ok"]
    # Every eval's cost is debited once, whatever was interrupted.
    per_eval: dict[str, int] = {}
    for debit in _debits(program):
        if "eval_run_id" in debit:
            per_eval[debit["eval_run_id"]] = per_eval.get(debit["eval_run_id"], 0) + 1
    assert all(count >= 1 for count in per_eval.values())
    # The interrupted experiment resumed its own eval instead of starting another.
    [experiment] = [e for e in state["experiments"] if e["id"] == "e1"]
    from hiveloom.eval_runner import _eval_dir

    manifests = {
        path.parent.name
        for path in _eval_dir(experiment["eval_run_id"]).parent.glob("eval_*/manifest.json")
        if json.loads(path.read_text())["harness_path"].endswith(
            f"candidates/{experiment['candidate']}")
    }
    # One eval for the experiment (resumed, not restarted) and one to confirm it.
    assert manifests == {state["inflight"]["experiment:e1"],
                         state["inflight"][f"confirm:{experiment['candidate']}"]}
    assert state["inflight"]["experiment:e1"] == experiment["eval_run_id"]


class _BrokenDirector(ModelProvider):
    def complete(self, **_):
        raise ConnectionError("provider unreachable")


def test_a_director_whose_provider_fails_does_not_wedge_the_program(lab: Path):
    program = init_program(lab, "broken", _charter(stop={"no_progress_rounds": 2}))
    Engine(program, director_provider=_BrokenDirector()).run()
    state = program.load_state()
    assert state["status"] == "done"
    assert state["stop_reason"]["condition"] == "no_progress"
    runs = [e["data"] for e in program.ledger.of_kind("director_run")]
    assert runs and all(run["status"] != "success" for run in runs)


def test_a_promotion_the_live_harness_no_longer_accepts_is_queued_as_rejected(lab: Path):
    program = init_program(lab, "moved", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="round")
    # Someone freezes the lever on the live harness while the program runs.
    construct.set_value(lab, "evolution.frozen", ["guardrails", "model", "system_prompt"])
    engine.run()
    state = program.load_state()
    assert state["promotion"]["status"] == "rejected"
    with Hive() as hive:
        row = hive.get_proposal(state["promotion"]["proposal_id"])
    assert row["status"] == "rejected"
    assert "no longer accepts" in (row["apply_result_json"] or "")


def test_a_spent_exploration_pool_stops_the_program_cleanly(lab: Path):
    program = init_program(lab, "poor", _charter(stop={}))
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")  # baseline
    program.budget.debit("exploration", program.budget.left("exploration"), reason="test")
    engine.run()
    state = program.load_state()
    assert state["stop_reason"]["condition"] == "budget"
    assert state["status"] == "done" and (program.root / "report.md").exists()


def test_the_ledger_detects_an_edited_event(lab: Path):
    program = init_program(lab, "tamper", _charter())
    Engine(program, director_provider=ScriptedDirector()).run(until="unit")
    lines = (program.root / "ledger.jsonl").read_text().splitlines()
    event = json.loads(lines[1])
    event["data"]["cells"] = 999
    lines[1] = json.dumps(event)
    (program.root / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    verdict = program.ledger.verify()
    assert not verdict["ok"] and verdict["broken_at"] == event["seq"]


class _GarbageExaminer:
    spent_usd = 0.0

    def generate(self, **_):
        return "I cannot help with that."


def test_an_examiner_that_writes_nothing_blocks_the_program_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    lab = tmp_path / "research-lab"
    shutil.copytree(RESEARCH_LAB, lab, ignore=shutil.ignore_patterns(".hiveloom"))
    program = init_program(lab, "c", lab / "research-concepts.yaml")
    Engine(Program(lab, "c")).run()
    Engine(Program(lab, "c")).approve_contract()
    engine = Engine(Program(lab, "c"), models={"research_lab/examiner": _GarbageExaminer()})
    result = engine.run()
    state = program.load_state()
    assert state["status"] == "blocked" and "examiner" in state["blocked_reason"]
    assert result[-1]["done"]
    # A blocked program answers every later step without spinning.
    assert Engine(Program(lab, "c")).run()[-1]["done"]


def test_a_program_cannot_be_stepped_by_two_processes_at_once(lab: Path):
    from hiveloom.research.program import ProgramBusy

    program = init_program(lab, "busy", _charter())
    with program.lock():
        with pytest.raises(ProgramBusy):
            Engine(Program(lab, "busy"), director_provider=ScriptedDirector()).step()
    Engine(Program(lab, "busy"), director_provider=ScriptedDirector()).step()
    assert program.load_state()["unit"] == "survey"


def test_a_stop_sent_while_a_unit_runs_survives_that_unit(lab: Path):
    program = init_program(lab, "race", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    real = engine._executor
    sent = {"done": False}

    def executor(**kwargs):
        if not sent["done"]:  # another process asks to stop mid-baseline
            Engine(Program(lab, "race")).request_stop("from another terminal")
            sent["done"] = True
        return real(**kwargs)

    engine._executor = executor
    engine.step()  # baseline saves its state after the stop arrived
    engine._executor = real
    engine.run()
    state = program.load_state()
    assert state["stop_reason"] == {"condition": "user", "detail": "from another terminal"}
    assert state["experiments"] == []


def test_case_ids_stay_unique_when_the_director_repeats_itself(tmp_path):
    from hiveloom.research.dataset import Case, CaseStore

    store = CaseStore(tmp_path)
    for batch in (["a", "b", "a2"], ["b", "c"], ["d", "e"]):
        store.add_working([Case(id="draft", input=text, provenance="director") for text in batch],
                          limit=50, prefix="work")
    ids = [case.id for case in store.working()]
    assert ids == ["work-1", "work-2", "work-3", "work-4", "work-5", "work-6"]


def test_an_orphan_candidate_folder_does_not_block_new_experiments(lab: Path):
    program = init_program(lab, "orphan", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")
    engine.run(until="unit")
    # An interrupted hypothesize left c1 on disk that state.json never recorded.
    (program.root / "candidates" / "c1").mkdir()
    engine.run()
    state = program.load_state()
    assert state["experiments"] and state["experiments"][0]["candidate"] == "c2"
    assert state["experiments"][0]["kept"]


def test_the_director_is_not_asked_once_its_pool_is_spent(lab: Path):
    program = init_program(lab, "spent", _charter(stop={}))
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")
    engine.run(until="unit")  # survey → round 1, then the pool empties
    program.budget.debit("exploration", program.budget.left("exploration"), reason="test")
    engine.run(until="unit")  # hypothesize
    assert program.ledger.of_kind("director_skipped")
    assert not program.ledger.of_kind("director_run")


def test_two_programs_never_share_a_run_key(lab: Path):
    long = "a-very-long-program-name-that-goes-on-and-"
    first = init_program(lab, long + "one", _charter())
    second = init_program(lab, long + "two", _charter())
    first_key = first.key()
    assert first_key != second.key()
    shutil.rmtree(first.root)
    again = init_program(lab, long + "one", _charter())
    assert again.key() != first_key  # a recreated program starts from no runs


def test_a_judge_that_errors_abstains_and_is_asked_again(tmp_path):
    from hiveloom.research.contract import Contract
    from hiveloom.research.judges import JudgePanel

    criterion = Contract.model_validate({"criteria": [
        {"id": "c", "says": "says hello", "check": {"kind": "judge", "rubric": "hello"}}
    ]}).criteria[0]

    class Flaky:
        spent_usd = 0.0

        def __init__(self):
            self.calls = 0

        def generate(self, **_):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("429")
            return '{"verdict": "pass"}'

    steady = type("Steady", (), {"spent_usd": 0.0,
                                 "generate": lambda self, **_: '{"verdict": "pass"}'})()
    flaky = Flaky()
    panel = JudgePanel(tmp_path, ["x/a", "x/b"], strong_models={"x/a": steady, "x/b": flaky})
    first = panel.votes(criterion, "greet", "hello")
    assert first["x/b"] is None and panel.verdict(first) is None  # no lone-judge verdict
    second = panel.votes(criterion, "greet", "hello")
    assert panel.verdict(second) == "pass" and flaky.calls == 2


def test_a_repeated_promotion_points_at_the_proposal_actually_queued(lab: Path):
    program = init_program(lab, "twice", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run()
    first = program.load_state()["promotion"]
    state = program.load_state()
    again = engine._queue_promotion(state)  # the report unit re-run after a crash
    assert again["proposal_id"] == first["proposal_id"]


def test_a_small_experiment_is_not_called_futile_before_it_has_pairs(tmp_path, monkeypatch):
    from research_director_script import _task, _tool_results
    from test_research_engine import TWO_RULES_EXTENSION

    from hiveloom.models.fake import text_response, tool_response

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
        "dataset:\n  loader: two_cases\nscorers:\n  - two_ok\nmodel_identity: warn\n")

    class BothRules(ModelProvider):
        def complete(self, *, system, messages, tools, config):
            results = _tool_results(messages)
            if "hypothesize phase" not in _task(messages):
                return (tool_response("interpret", {"findings": [], "next_focus": [],
                                                    "decision": "stop", "stop_reason": "x"},
                                      call_id="i") if not results else text_response("ok"))
            if not results:
                return tool_response("brief", {}, call_id="b")
            if len(results) == 1:
                return tool_response("register_hypothesis", {
                    "claim": "both kinds need their rule", "levers": ["system_prompt"],
                    "target": "success_rate", "expect": "increase", "falsifier": "none"},
                    call_id="h")
            if len(results) == 2:
                prompt = json.loads(results[0])["incumbent"]["spec"]["system_prompt"]
                return tool_response("design_experiment", {"hypothesis_id": "h1", "changes": [
                    {"path": "system_prompt", "value": prompt + "\nrule-a\nrule-b"}]},
                    call_id="d")
            return text_response("done")

    program = init_program(two, "small", _charter(execution={}, holdout=0.75, stop={}))
    assert len(program.load_state()["split"]["working"]) == 6
    Engine(program, director_provider=BothRules()).run()
    [experiment] = program.load_state()["experiments"]
    assert experiment["stopped_early"] is None
    assert experiment["verdict"] == "confirmed" and experiment["kept"]


def test_a_promotion_keeps_what_the_user_edited_on_the_live_harness_meanwhile(lab: Path):
    program = init_program(lab, "merge", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="round")
    from hiveloom.spec.loader import load_spec

    live = load_spec(lab).system_prompt
    construct.set_value(lab, "system_prompt", "Be brief.\n\n" + live)  # the user's own edit
    engine.run()
    state = program.load_state()
    with Hive() as hive:
        row = hive.get_proposal(state["promotion"]["proposal_id"])
    assert row["status"] == "pending"
    [change] = json.loads(row["proposal_json"])["yaml_changes"]
    assert change["value"].startswith("Be brief.")  # the user's edit survives
    assert "uppercase the invoice id first" in change["value"]  # and so does the fix


def test_a_promotion_that_conflicts_with_a_live_edit_is_rejected_with_the_path(lab: Path):
    program = init_program(lab, "clash", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="round")
    construct.set_value(lab, "system_prompt", "An entirely different prompt.")
    engine.run()
    state = program.load_state()
    assert state["promotion"]["status"] == "rejected"
    with Hive() as hive:
        row = hive.get_proposal(state["promotion"]["proposal_id"])
    assert "system_prompt" in row["apply_result_json"]


def test_a_candidate_cannot_bring_tools_the_charter_never_classified(lab: Path):
    from hiveloom.research.engine import DirectorSession

    program = init_program(lab, "tools", _charter(levers=["system_prompt", "tools"]))
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")
    engine.run(until="unit")
    state = program.load_state()
    session = DirectorSession(engine, state, "hypothesize")
    registered = json.loads(session.call("register_hypothesis", {
        "claim": "tools", "levers": ["tools"], "target": "success_rate",
        "expect": "increase", "falsifier": "none"}))["registered"]
    tools = [ref.model_dump(mode="json", exclude_none=True)
             for ref in load_spec_of(program, "c0").tools]

    def design(new_tools):
        return json.loads(session.call("design_experiment", {
            "hypothesis_id": registered, "changes": [{"path": "tools", "value": new_tools}]}))

    network = design([*tools, {"builtin": "http_get", "hosts": ["example.com"]}])
    assert "does not classify" in network["refused"]
    code = design([{**tools[0], "code": "tools/lookup_invoice.py:other"}])
    assert "refused" in code
    described = design([{**tools[0], "description": "Look up one invoice (ids are uppercase)."}])
    assert "candidate" in described, described
    reader = design([*tools, {"builtin": "file_read"}])
    assert "candidate" in reader, reader  # a read-only builtin is allowed by default
    assert not (program.root / "candidates" / "c3").exists()  # refused copies are removed


def load_spec_of(program, candidate):
    from hiveloom.spec.loader import load_spec

    return load_spec(program.candidate_dir(candidate))


def test_merges_accept_the_same_change_twice_and_lessons_merge_per_id():
    from hiveloom.research.engine import _CONFLICT, _merge3, _merge_entries, _rebase

    base = "one\ntwo\n"
    assert _merge3(base, "one\nTWO\n", "one\nTWO\n") == "one\nTWO\n"  # both made the same edit
    assert _rebase({"path": "loop.max_turns", "value": 8},
                   {"loop": {"max_turns": 6}}, {"loop": {"max_turns": 8}}) == 8
    a, a2, b, c = ({"id": "a", "v": 1}, {"id": "a", "v": 2}, {"id": "b", "v": 1},
                   {"id": "c", "v": 1})
    # The program edited a lesson the live side left alone; live added one.
    assert _merge_entries([a], [a, b], [a2]) == [a2, b]
    # The program removed a lesson; live added another: the removal holds.
    assert _merge_entries([a, c], [a, c, b], [c]) == [c, b]
    # Both edited one lesson differently.
    assert _merge_entries([a], [a2], [{"id": "a", "v": 3}]) is _CONFLICT


def test_delegation_is_never_a_research_lever_and_is_off_in_research_runs(lab: Path):
    from hiveloom.errors import SpecError
    from hiveloom.spec.loader import load_spec

    with pytest.raises(SpecError, match="delegation"):
        init_program(lab, "deleg", _charter(levers=["system_prompt", "delegation"]))
    construct.set_value(lab, "delegation.enabled", True)
    program = init_program(lab, "off", _charter())
    assert load_spec(program.candidate_dir("c0")).delegation.enabled is False
    assert load_spec(lab).delegation.enabled is True  # the live harness keeps it
    [started] = program.ledger.of_kind("program_started")
    assert started["data"]["delegation_disabled"] is True


def test_dropping_a_classified_tool_is_allowed(lab: Path):
    from hiveloom.research.engine import DirectorSession

    construct.add_tool(lab, builtin="file_read")
    program = init_program(lab, "drop", _charter(levers=["system_prompt", "tools"]))
    engine = Engine(program, director_provider=ScriptedDirector())
    engine.run(until="unit")
    engine.run(until="unit")
    session = DirectorSession(engine, program.load_state(), "hypothesize")
    registered = json.loads(session.call("register_hypothesis", {
        "claim": "fewer tools", "levers": ["tools"], "target": "success_rate",
        "expect": "increase", "falsifier": "none"}))["registered"]
    tools = [ref.model_dump(mode="json", exclude_none=True)
             for ref in load_spec_of(program, "c0").tools
             if getattr(ref, "builtin", None) != "file_read"]
    result = json.loads(session.call("design_experiment", {
        "hypothesis_id": registered, "changes": [{"path": "tools", "value": tools}]}))
    assert "candidate" in result, result


def test_an_eval_that_cannot_resume_is_started_again(lab: Path):
    program = init_program(lab, "restart", _charter())
    engine = Engine(program, director_provider=ScriptedDirector())
    state = program.load_state()
    state["inflight"] = {"baseline": "eval_" + "0" * 16}
    program.save_state(state)
    # A manifest that exists but can no longer be resumed.
    from hiveloom import eval_runner

    calls = {"n": 0}
    real = eval_runner.load_eval_manifest

    def fake_load(eval_run_id):
        if eval_run_id == "eval_" + "0" * 16:
            return object()
        return real(eval_run_id)

    import hiveloom.research.engine as engine_mod

    original = engine_mod.resume_eval

    def refuse(eval_run_id, **kwargs):
        if eval_run_id == "eval_" + "0" * 16:
            calls["n"] += 1
            raise ValueError("harness behavior changed; refusing resume")
        return original(eval_run_id, **kwargs)

    eval_runner.load_eval_manifest, engine_mod.resume_eval = fake_load, refuse
    try:
        engine.step()
    finally:
        eval_runner.load_eval_manifest, engine_mod.resume_eval = real, original
    assert calls["n"] == 1
    state = program.load_state()
    assert state["unit"] == "survey" and state["inflight"]["baseline"] != "eval_" + "0" * 16
    assert program.ledger.of_kind("eval_restarted")


def test_an_unreadable_judge_reply_is_asked_again(tmp_path):
    from hiveloom.research.contract import Contract
    from hiveloom.research.judges import JudgePanel

    criterion = Contract.model_validate({"criteria": [
        {"id": "c", "says": "says hello", "check": {"kind": "judge", "rubric": "hello"}}
    ]}).criteria[0]
    replies = iter(["hmm, let me think", '{"verdict": "pass"}'])
    judge = type("J", (), {"spent_usd": 0.0,
                           "generate": lambda self, **_: next(replies)})()
    panel = JudgePanel(tmp_path, ["x/a"], strong_models={"x/a": judge})
    assert panel.verdict(panel.votes(criterion, "greet", "hello")) is None
    assert panel.verdict(panel.votes(criterion, "greet", "hello")) == "pass"


def test_an_unknown_director_is_refused_before_anything_runs(lab: Path):
    from hiveloom.errors import SpecError

    with pytest.raises(SpecError, match="models.director 'claude/Hiveloom Copilot'"):
        init_program(lab, "who", _charter(models={"director": "claude/Hiveloom Copilot"}))
    with pytest.raises(SpecError, match="models.director"):
        init_program(lab, "who", _charter(models={"director": "nosuch/model"}))
    assert not (lab / ".hiveloom" / "research" / "who").exists()


LOOPER_EXTENSION = '''
from hiveloom import EvalCase, RunMetric, ScorerOutput
from hiveloom.ext import ModelInfo
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider


class Worker(ModelProvider):
    """Answers wrongly; told to 'double-check forever', it never answers at all."""

    def complete(self, *, system, messages, tools, config):
        if "double-check forever" in system:
            return tool_response("ping", {}, call_id=f"p{len(messages)}")
        return text_response("wrong")


class Cases:
    def load(self):
        return [EvalCase(id=f"case-{i:02d}", input=f"item {i}", expected={})
                for i in range(1, 25)]


def score(context):
    return ScorerOutput(metrics=[RunMetric(
        run_id=context.run_result.run_id, name="ok", source="l", scope="case",
        value=float(context.run_result.output == "ok"))])


def hiveloom_extension(hive):
    hive.register_provider("looper", lambda _ctx: Worker(),
                           models=[ModelInfo(id="worker", provider="looper")],
                           api="local", open_catalog=False)
    hive.register_dataset("loop_cases", lambda _p, _c: Cases(), description="items")
    hive.register_scorer("loop_ok", lambda _p, _c: score, description="ok or not")
'''

PING_TOOL = '''
from hiveloom.tools import tool


@tool(description="Ping.")
def ping(note: str = "") -> str:
    return "pong"
'''


def test_a_mechanism_that_moves_because_runs_stop_answering_is_shifted_not_kept(
        tmp_path, monkeypatch):
    from research_director_script import _task, _tool_results

    from hiveloom.models.fake import text_response, tool_response

    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    looper = tmp_path / "looper"
    construct.init_harness(looper, name="looper", task="Answer ok.")
    (looper / "extensions").mkdir()
    (looper / "extensions" / "looper.py").write_text(LOOPER_EXTENSION)
    (looper / "tools" / "ping.py").write_text(PING_TOOL)
    construct.set_value(looper, "extensions", ["extensions/looper.py"])
    construct.set_value(looper, "model", {"provider": "looper", "id": "worker"})
    construct.set_value(looper, "loop.max_turns", 3)
    construct.set_value(looper, "verify.on_fail.action", "abort")
    construct.add_tool(looper, code="tools/ping.py:ping", description="Ping.")
    construct.add_validator(looper, builtin="regex_match", pattern="^ok$")
    (looper / "eval.yaml").write_text(
        "schema_version: 1\nharness: .\nextensions:\n  - extensions/looper.py\n"
        "dataset:\n  loader: loop_cases\nscorers:\n  - loop_ok\nmodel_identity: warn\n")

    class Gamer(ModelProvider):
        def complete(self, *, system, messages, tools, config):
            results = _tool_results(messages)
            if "hypothesize phase" not in _task(messages):
                return (tool_response("interpret", {"findings": [], "next_focus": [],
                                                    "decision": "stop", "stop_reason": "x"},
                                      call_id="i") if not results else text_response("ok"))
            if not results:
                return tool_response("brief", {}, call_id="b")
            brief = json.loads(results[0])
            if len(results) == 1:
                target = next(t for t in brief["signal"]["targets"]
                              if t.startswith("friction:verifier_failure"))
                return tool_response("register_hypothesis", {
                    "claim": "verifier failures come from answering too fast",
                    "levers": ["system_prompt"], "target": target, "expect": "decrease",
                    "falsifier": "verifier failures stay"}, call_id="h")
            if len(results) == 2:
                prompt = brief["incumbent"]["spec"]["system_prompt"]
                return tool_response("design_experiment", {"hypothesis_id": "h1", "changes": [
                    {"path": "system_prompt", "value": prompt + "\ndouble-check forever"}]},
                    call_id="d")
            return text_response("done")

    program = init_program(looper, "gamed", _charter(
        execution={"tools": {"ping": "allow"}}, holdout=0.25, stop={}))
    Engine(program, director_provider=Gamer()).run()
    [experiment] = program.load_state()["experiments"]
    assert experiment["verdict"] == "shifted" and not experiment["kept"]
    assert experiment["shifted_pairs"] > 0
    assert program.load_state()["promotion"] is None
