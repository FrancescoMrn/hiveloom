"""Concepts mode: contracts, cases, judges, questions, and a program that builds its eval."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hiveloom.errors import SpecError
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider
from hiveloom.research.charter import Charter
from hiveloom.research.contract import Contract, applies, check_deterministic
from hiveloom.research.dataset import Case, CaseStore, drop_near_duplicates
from hiveloom.research.engine import Engine
from hiveloom.research.judges import JudgePanel, cohen_kappa, parse_verdict
from hiveloom.research.program import Program, init_program
from hiveloom.research.questions import QuestionBox

RESEARCH_LAB = Path(__file__).resolve().parents[1] / "harnesses" / "research-lab"


def _criterion(**check):
    return Contract.model_validate(
        {"criteria": [{"id": "c", "says": "a criterion", "check": check}]}
    ).criteria[0]


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def test_deterministic_checks_read_the_output_and_the_expected_value():
    field = _criterion(kind="json_field", field="price")
    assert check_deterministic(field, '{"price": 9.0}', {"c": 9})
    assert not check_deterministic(field, '{"price": 9.5}', {"c": 9})
    assert not check_deterministic(field, "not json", {"c": 9})
    present = _criterion(kind="json_present", field="price")
    assert check_deterministic(present, '```json\n{"price": 1}\n```', {})
    assert not check_deterministic(present, '{"price": null}', {})
    assert check_deterministic(_criterion(kind="contains"), "Total: EUR 12", {"c": "eur"})
    assert check_deterministic(_criterion(kind="regex", pattern=r"^\{"), "{}", {})
    # A check that needs a value applies only where the case gives one.
    assert not applies(_criterion(kind="contains"), {})
    assert applies(_criterion(kind="regex", pattern="x"), {})


def test_a_contract_refuses_what_it_could_not_check():
    with pytest.raises(ValueError, match="rubric"):
        _criterion(kind="judge")
    with pytest.raises(ValueError, match="field"):
        _criterion(kind="json_field")
    with pytest.raises(ValueError, match="one JSON field"):
        _criterion(kind="json_present", field="zone,weight_kg,price")
    with pytest.raises(ValueError, match="unknown criterion"):
        Contract.model_validate({"criteria": [{"id": "a", "says": "one thing",
                                               "check": {"kind": "regex", "pattern": "x"}}],
                                 "goal_thresholds": {"b": 0.9}})


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
def test_a_working_case_that_copies_a_sealed_one_is_dropped(tmp_path):
    quote = "Quote a 2 kg parcel to zone 3 for C-100"
    working = [Case(id="w1", input=f"{quote}.", provenance="director"),
               Case(id="w2", input="Refund policy for damaged goods?", provenance="director")]
    sealed = [Case(id="s1", input=f"{quote}!", provenance="examiner")]
    kept, dropped = drop_near_duplicates(working, sealed)
    assert [c.id for c in kept] == ["w2"] and dropped == 1

    store = CaseStore(tmp_path)
    added, refused = store.add_working([*working, working[0]], limit=10)
    assert len(added) == 2 and "duplicate" in refused[0]
    _, full = store.add_working([Case(id="w3", input="another", provenance="director")], limit=2)
    assert "full" in full[0]


# --------------------------------------------------------------------------- #
# Judges
# --------------------------------------------------------------------------- #
class _Strong:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0
        self.spent_usd = 0.0

    def generate(self, *, system, user, max_tokens=None):
        self.calls += 1
        self.spent_usd += 0.001
        answer = user.split("<answer>\n", 1)[1].split("\n</answer>", 1)[0]
        return json.dumps({"verdict": self.verdict(answer)})


def test_judges_are_unanimous_or_nothing_cached_and_trusted_only_on_labels(tmp_path):
    rubric = _criterion(kind="judge", rubric="says hello")
    a = _Strong(lambda answer: "pass" if "hello" in answer else "fail")
    b = _Strong(lambda answer: "pass" if "hello" in answer and "!" not in answer else "fail")
    panel = JudgePanel(tmp_path, ["x/a", "x/b"], strong_models={"x/a": a, "x/b": b})
    assert panel.verdict(panel.votes(rubric, "greet", "hello")) == "pass"
    assert panel.verdict(panel.votes(rubric, "greet", "hello!")) is None  # a split
    panel.votes(rubric, "greet", "hello")
    assert a.calls == 2 and panel.spent_usd == pytest.approx(0.004)  # cached, and counted

    anchors = [{"criterion": "c", "input": "greet", "output": out, "label": label}
               for out, label in [("hello", "pass"), ("bye", "fail"), ("hello there", "pass"),
                                  ("go away", "fail")]]
    few = panel.trust(rubric, anchors[:2], kappa=0.6, agreement=0.85, min_anchors=4)
    assert not few.trusted and "2 of 4" in few.reason
    enough = panel.trust(rubric, anchors, kappa=0.6, agreement=0.85, min_anchors=4)
    assert enough.trusted and enough.kappa == 1.0
    wrong = [{**anchor, "label": "fail" if anchor["label"] == "pass" else "pass"}
             for anchor in anchors]
    assert not panel.trust(rubric, wrong, kappa=0.6, agreement=0.85, min_anchors=4).trusted


def test_kappa_and_verdict_parsing():
    assert cohen_kappa([("pass", "pass"), ("fail", "fail")]) == 1.0
    assert cohen_kappa([("pass", "fail"), ("fail", "pass")]) == -1.0
    assert parse_verdict('ok {"verdict": "PASS", "reason": "x"}') == "pass"
    assert parse_verdict("no idea") is None


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
def test_questions_are_capped_deduplicated_and_labels_become_anchors(tmp_path):
    box = QuestionBox(tmp_path, total=3, batch=2)
    first = box.ask("label", "Does it pass?", criterion="c", request="r", output="o1")
    box.ask("label", "Does it pass?", criterion="c", request="r", output="o2")
    with pytest.raises(ValueError, match="no room"):
        box.ask("disambiguate", "What does exact mean?")
    with pytest.raises(ValueError, match="a label is"):
        box.answer(first.id, "maybe")
    box.answer(first.id, "PASS")
    assert box.anchors()[0]["label"] == "pass" and box.anchors()[0]["output"] == "o1"
    with pytest.raises(ValueError, match="already asked"):
        box.ask("label", "Does it pass?", criterion="c", request="r", output="o2")
    box.ask("disambiguate", "What does exact mean?")
    assert box.room() == 0  # three asked over the program


# --------------------------------------------------------------------------- #
# Charter
# --------------------------------------------------------------------------- #
def _concepts_charter(**overrides) -> dict:
    charter = {
        "goal": "g", "concepts": "c", "levers": ["system_prompt"],
        "budget": {"usd": 1, "split": {"data": 0.2, "exploration": 0.25, "experiments": 0.4,
                                       "confirmation": 0.15}},
        "models": {"director": "a/d", "examiner": "a/e", "judges": ["a/j"]},
    }
    charter.update(overrides)
    return charter


def test_a_concepts_charter_needs_its_own_examiner_and_a_data_budget():
    assert Charter.model_validate(_concepts_charter()).concepts_mode
    with pytest.raises(ValueError, match="either an eval or concepts"):
        Charter.model_validate(_concepts_charter(eval="eval.yaml"))
    with pytest.raises(ValueError, match="must differ"):
        Charter.model_validate(_concepts_charter(models={"director": "a/d", "examiner": "a/d"}))
    with pytest.raises(ValueError, match="data"):
        Charter.model_validate(_concepts_charter(budget={"usd": 1}))
    assert Charter.model_validate({**_concepts_charter(), "concepts": None,
                                   "budget": {"usd": 1}}).eval == "eval.yaml"


# --------------------------------------------------------------------------- #
# A program that builds its evaluation (research-lab, scripted models)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def lab(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    target = tmp_path / "research-lab"
    shutil.copytree(RESEARCH_LAB, target, ignore=shutil.ignore_patterns(".hiveloom"))
    return target


def _label_like_a_user(engine: Engine) -> int:
    answered = 0
    for question in engine.questions.open():
        try:
            data = json.loads(question.output or "")
        except json.JSONDecodeError:
            data = None
        good = isinstance(data, dict) and set(data) == {"zone", "weight_kg", "price"}
        engine.answer(question.id, "pass" if good else "fail")
        answered += 1
    return answered


def test_a_program_frames_waits_for_approval_then_measures_by_the_contract(lab: Path):
    program = init_program(lab, "concepts", lab / "research-concepts.yaml")
    steps = Engine(Program(lab, "concepts")).run()
    assert steps[-1]["awaiting"] == "contract"
    state = program.load_state()
    assert state["status"] == "awaiting_user" and state["sample_cases"]
    assert [c["id"] for c in state["draft_contract"]["criteria"]] == ["quoted", "one-json"]
    # The gate holds: running again does nothing until the user approves.
    assert Engine(Program(lab, "concepts")).run()[-1]["awaiting"] == "contract"

    Engine(Program(lab, "concepts")).approve_contract()
    Engine(Program(lab, "concepts")).run(until="unit")  # examine
    engine = Engine(Program(lab, "concepts"))
    engine.run(until="unit")  # baseline, which asks for labels
    assert _label_like_a_user(engine) >= 4
    Engine(Program(lab, "concepts")).run()

    state = program.load_state()
    trust = {row["criterion"]: row for row in state["trust"]}
    assert trust["quoted"]["measured"] and trust["one-json"]["measured"]
    assert state["stop_reason"]["condition"] == "goal"
    assert state["experiments"][0]["kept"]
    assert state["confirmation"]["ran"] and "proxy_gap" in state["confirmation"]
    report = (program.root / "report.md").read_text()
    assert "## Evaluation contract" in report and "judge (trusted)" in report
    assert Engine(Program(lab, "concepts")).questions.open() == []  # withdrawn at the end

    # The sealed cases never reached the director: not in any of its journals.
    sealed = [case.input for case in engine.cases.sealed()]
    director_text = "".join(p.read_text() for p in program.director_dir.glob("*.jsonl"))
    assert sealed and not any(text in director_text for text in sealed)


def test_an_unlabelled_judge_measures_nothing_and_a_stop_ends_the_wait(lab: Path):
    program = init_program(lab, "halt", lab / "research-concepts.yaml")
    Engine(Program(lab, "halt")).run()
    Engine(Program(lab, "halt")).request_stop("not now")
    Engine(Program(lab, "halt")).run()
    state = program.load_state()
    assert state["status"] == "done" and state["stop_reason"]["condition"] == "user"
    assert state.get("contract_version") is None


def test_seeds_must_exist_and_concepts_file_is_read(lab: Path):
    charter = (lab / "research-concepts.yaml").read_text()
    (lab / "bad.yaml").write_text(charter.replace("seeds.jsonl", "missing.jsonl"))
    with pytest.raises(SpecError, match="missing.jsonl"):
        init_program(lab, "bad", lab / "bad.yaml")
    program = init_program(lab, "ok", lab / "research-concepts.yaml")
    assert "shipping desk" in (program.root / "concepts.md").read_text()
    assert [c.provenance for c in CaseStore(program.root).working()] == ["seed"] * 3


# --------------------------------------------------------------------------- #
# M3: a director that games the working cases is caught by the sealed ones
# --------------------------------------------------------------------------- #
def _gaming_provider(frame_provider):
    class Gaming(ModelProvider):
        def complete(self, *, system, messages, tools, config):
            task = next(m["content"] for m in messages if m["role"] == "user"
                        and isinstance(m["content"], str))
            results = [b for m in messages if isinstance(m.get("content"), list)
                       for b in m["content"] if b.get("type") == "tool_result"]
            if "hypothesize phase" not in task:
                return frame_provider.complete(system=system, messages=messages, tools=tools,
                                               config=config)
            bodies = [json.loads(r["content"] if isinstance(r["content"], str)
                                 else r["content"][0]["text"]) for r in results]
            if not bodies:
                return tool_response("brief", {}, call_id="b")
            if len(bodies) == 1:
                return tool_response("runs", {"limit": 25}, call_id="r")
            if len(bodies) == 2:
                signal = [s["feature"] for s in bodies[0]["signal"]["signals"]
                          if s["direction"] == "risk"][0]
                return tool_response("register_hypothesis", {
                    "claim": "the desk should answer the requests it has seen from memory",
                    "levers": ["system_prompt"], "target": signal, "expect": "decrease",
                    "falsifier": "nothing"}, call_id="h")
            if len(bodies) == 3:
                known = "\n".join(f"KNOWN: {run['task']} => 10.0" for run in bodies[1]["runs"])
                prompt = bodies[0]["incumbent"]["spec"]["system_prompt"].rstrip()
                return tool_response("design_experiment", {
                    "hypothesis_id": bodies[2]["registered"],
                    "changes": [{"path": "system_prompt", "value": f"{prompt}\n\n{known}\n"}],
                }, call_id="e")
            return text_response("done")
    return Gaming()


def test_a_director_that_games_the_working_cases_is_caught_by_the_sealed_ones(lab: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "research_lab_provider", lab / "extensions" / "research_lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scripted = module.ResearchLabProvider()

    # The contract is the only measure here: billing's own validator would
    # otherwise reject the made-up prices before the contract is read.
    from hiveloom import construct

    construct.remove_item(lab, "validators/billing_check.py:validate")
    init_program(lab, "gamed", lab / "research-concepts.yaml")
    Engine(Program(lab, "gamed")).run()
    Engine(Program(lab, "gamed")).approve_contract()
    Engine(Program(lab, "gamed")).run(until="unit")
    engine = Engine(Program(lab, "gamed"))
    engine.run(until="unit")
    _label_like_a_user(engine)
    gamed = _gaming_provider(scripted)
    Engine(Program(lab, "gamed"), director_provider=gamed).run()

    state = Program(lab, "gamed").load_state()
    kept = [e for e in state["experiments"] if e["kept"]]
    assert kept, "the memorized answers look like progress on the working cases"
    confirmation = state["confirmation"]
    assert confirmation["strength"] in ("provisional", "contradicted")
    assert confirmation["proxy_gap"]["flagged"]
    report = (Program(lab, "gamed").root / "report.md").read_text()
    assert "possible overfitting" in report


def test_a_run_only_a_distrusted_judge_decided_loses_that_verdict(tmp_path, monkeypatch):
    from hiveloom import construct, runner
    from hiveloom.research.evaluation import label_outcomes

    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    construct.init_harness(tmp_path / "h", name="h", task="t")
    result = runner.run_harness(tmp_path / "h", "hi", provider=__import__(
        "hiveloom.models.fake", fromlist=["FakeModelProvider"]).FakeModelProvider(
        [text_response("hello")]))
    contract = Contract.model_validate({"criteria": [
        {"id": "tone", "says": "a friendly tone", "check": {"kind": "judge", "rubric": "kind"}}]})
    from hiveloom.logging.hive import Hive

    with Hive() as hive:
        # The judge scored the run a failure on its only criterion.
        hive.metric_values = lambda run_ids, name: (
            {result.run_id: 0.0} if name == "criterion.tone" else {})
        key = hive.get_run(result.run_id)["harness_key"]
        label_outcomes(hive, key, contract, measured={"tone"})
        assert hive.get_outcome(result.run_id)["outcome"] == "failure"
        label_outcomes(hive, key, contract, measured=set())  # the judge lost its trust
        assert hive.get_outcome(result.run_id) is None


def test_a_fresh_process_can_frame_with_a_director_the_harness_extension_provides(lab: Path):
    from hiveloom import ext

    init_program(lab, "fresh", lab / "research-concepts.yaml")
    ext.reset()  # a new `hiveloom research run` process has loaded nothing yet
    steps = Engine(Program(lab, "fresh")).run()
    assert steps[-1]["awaiting"] == "contract"
