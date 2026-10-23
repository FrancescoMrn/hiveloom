"""PPC orchestration, evidence boundaries and ablation without model credentials."""

import json
import runpy
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from research_director_script import UPPERCASE_RULE, _task, _tool_results
from test_research_engine import SIGNAL_LAB, _charter

from hiveloom.models.capabilities import IdentityEvidence, ModelProbeResult
from hiveloom.models.fake import text_response, tool_response
from hiveloom.models.provider import ModelProvider
from hiveloom.research.charter import Charter
from hiveloom.research.engine import Engine
from hiveloom.research.ppc import PPCSession
from hiveloom.research.program import Program, init_program


class PPCDirector(ModelProvider):
    def __init__(self, abstain=False, composed_rule=UPPERCASE_RULE):
        self.briefs = []
        self.abstain = abstain
        self.composed_rule = composed_rule

    def complete(self, *, system, messages, tools, config):
        results = [json.loads(r) for r in _tool_results(messages)]
        assert not any("refused" in r for r in results), results
        if not results:
            return tool_response("brief", {})
        brief = results[0]
        role = brief.get("ppc_role")
        if len(results) == 1:
            self.briefs.append(brief)
            if role is None:
                return tool_response("interpret", {
                    "findings": [], "next_focus": [], "decision": "stop"})
            return tool_response("register_hypothesis", {
                "claim": f"{role} improves invoice retrieval", "levers": ["system_prompt"],
                "target": "success_rate", "expect": "increase",
                "falsifier": "success does not increase"})
        if len(results) == 2 and role:
            prompt = brief["incumbent"]["spec"]["system_prompt"]
            # Two unsuccessful ideas; the composer can still create a useful third.
            rule = self.composed_rule if role == "compose" else f"Try the {role} method."
            args = {"hypothesis_id": results[1]["registered"],
                    "changes": [{"path": "system_prompt", "value": prompt + "\n" + rule}]}
            if role == "compose":
                run_ids = [r["run_id"] for c in brief["comparison"]["cases"]
                           for rows in c["runs"].values() for r in rows]
                args["decisions"] = [{"path": "system_prompt", "action": "revised",
                                      "reason": "Both probes still fail lowercase lookup.",
                                      "run_ids": run_ids[:2]}]
                if self.abstain:
                    args["changes"] = []
            return tool_response("design_experiment", args)
        assert "PPC" in _task(messages) or role is None
        return text_response("done")


def program_at(tmp_path, **overrides):
    lab = tmp_path / "lab"
    shutil.copytree(SIGNAL_LAB, lab)
    return init_program(lab, "ppc", _charter(strategy="ppc", **overrides))


@pytest.mark.parametrize("strategy,abstain,count", [
    ("ppc", False, 3), ("ppc", True, 2), ("ppc_select", False, 2),
])
def test_round_resumes_and_composes_losing_probes(tmp_path, strategy, abstain, count):
    lab = tmp_path / "lab"
    shutil.copytree(SIGNAL_LAB, lab)
    before = (lab / "harness.yaml").read_bytes()
    program = init_program(lab, "ppc", _charter(strategy=strategy))
    director = PPCDirector(abstain)
    units = []
    # Reload engine and program after every unit, including between the proposals.
    for _ in range(20):
        engine = Engine(Program(lab, "ppc"), director_provider=director)
        result = engine.step()
        units.append(result["unit"])
        if result.get("done"):
            break
    state = program.load_state()
    assert state["status"] == "done"
    assert len(state["experiments"]) == count
    assert {e["base"] for e in state["experiments"]} == {"c0"}
    a, b = director.briefs[:2]
    assert a["hypotheses"] == b["hypotheses"] == []
    assert a["incumbent"] == b["incumbent"]
    assert "compose" in units if strategy == "ppc" else "compose" not in units
    if count == 3:
        assert all(not e["kept"] for e in state["experiments"][:2])
        assert state["experiments"][2]["kept"]
        assert state["confirmation"]["ran"]
        assert state["promotion"]["status"] == "pending"
        comparison = next(b["comparison"] for b in director.briefs if "comparison" in b)
        assert {c["case_id"] for c in comparison["cases"]} <= set(state["split"]["working"])
        assert comparison["total_cases"] == len(state["split"]["working"])
        assert set(state["split"]["holdout"]).isdisjoint(
            c["case_id"] for c in comparison["cases"])
        assert state["compositions"][0]["decisions"]
        assert "Composition round 1" in (program.root / "report.md").read_text()
    else:
        assert state["incumbent"] == "c0"
    assert (lab / "harness.yaml").read_bytes() == before
    assert program.ledger.verify()["ok"]


def test_composer_refuses_forged_evidence_and_frozen_changes(tmp_path):
    program = program_at(tmp_path)
    engine = Engine(program, director_provider=PPCDirector())
    for _ in range(6):
        engine.step()
    state = program.load_state()
    assert state["unit"] == "compose"
    session = PPCSession(engine, state, "compose")
    pages = [session._runs(offset=offset, limit=4)
             for offset in range(0, len(state["split"]["working"]), 4)]
    assert {c["case_id"] for page in pages for c in page["cases"]} == set(
        state["split"]["working"])
    assert pages[-1]["next_offset"] is None
    # Every page stays inline in the director's configured 16 KiB tool budget.
    assert all(len(json.dumps(page).encode()) < 16384 for page in pages)
    h = session._register_hypothesis(claim="compose", levers=["system_prompt"],
                                     target="success_rate", expect="increase", falsifier="none")
    args = {"hypothesis_id": h["registered"],
            "changes": [{"path": "system_prompt", "value": "new prompt"}],
            "decisions": [{"path": "system_prompt", "action": "revised",
                           "reason": "observed", "run_ids": ["foreign-or-sealed"]}]}
    assert "refused" in json.loads(session.call("design_experiment", args))
    assert not state["pending_experiments"]
    with pytest.raises(ValueError, match="outside the charter"):
        session._register_hypothesis(claim="weaken", levers=["verify"],
                                     target="success_rate", expect="increase", falsifier="none")
    # A domain proposer cannot inspect the procedure variant through diff.
    domain = PPCSession(engine, state, "domain")
    assert "refused" in json.loads(domain.call("diff", {"candidate": "c1"}))
    assert "refused" in json.loads(session.call("excerpt", {"run_id": "foreign-or-sealed"}))


@pytest.mark.parametrize("override", [
    {"levers": ["verify"]}, {"levers": ["tools"]}, {"experiments_per_round": 3},
    {"concepts": "build my eval"}, {"strategy": "unknown"},
    {"holdout": 0},
])
def test_ppc_charter_restricts_scope(override):
    with pytest.raises(ValidationError):
        Charter.model_validate(_charter(strategy="ppc") | override)


def test_exhausted_director_budget_and_user_stop(tmp_path):
    program = program_at(tmp_path)
    engine = Engine(program, director_provider=PPCDirector())
    for _ in range(6):
        engine.step()
    program.budget.debit("exploration", program.budget.left("exploration"))
    assert engine.step()["unit"] == "compose"
    assert not program.load_state()["pending_experiments"]
    engine.request_stop()
    engine.step()
    assert program.load_state()["unit"] == "confirm"


def test_losing_composition_is_recorded_but_never_promoted(tmp_path):
    program = program_at(tmp_path)
    Engine(program, director_provider=PPCDirector(composed_rule="Keep guessing.")).run()
    state = program.load_state()
    assert len(state["experiments"]) == 3
    assert not any(e["kept"] for e in state["experiments"])
    assert state["incumbent"] == "c0"
    assert state["promotion"] is None
    assert state["compositions"][0]["experiment"] == "e3"


def test_missing_probe_skips_composition(tmp_path):
    class MissingDomain(PPCDirector):
        def complete(self, **kwargs):
            if "PPC domain proposal phase" in _task(kwargs["messages"]):
                return text_response("No domain hypothesis.")
            return super().complete(**kwargs)

    program = program_at(tmp_path)
    Engine(program, director_provider=MissingDomain()).run()
    assert len(program.load_state()["experiments"]) == 1
    assert any(e["kind"] == "composition_skipped" for e in program.ledger.events())


def test_saved_article_pilot_composes_format_and_complete_quote(tmp_path):
    pilot = Path(__file__).parents[1] / "evals" / "research-ppc"
    build = runpy.run_path(str(pilot / "build.py"))["build"]
    target = tmp_path / "articles"
    build(target, "claude/claude-haiku-4-5", "claude/claude-haiku-4-5")
    records = json.loads((target / "pages.json").read_text())
    expected = {r["input"]: r["expected"] for r in records}

    class ArticleExecutor(ModelProvider):
        def complete(self, *, system, messages, tools, config):
            answer = dict(expected[_task(messages)])
            if "Complete quotations as strict JSON" in system:
                return text_response(json.dumps(answer))
            if "domain method" in system:
                return text_response("Here is the article: " + json.dumps(answer))
            answer["quote"] = answer["quote"].split(",")[0]
            if "procedure method" not in system:
                answer["title"] = "Latest news"
            return text_response(json.dumps(answer))

    director = PPCDirector(composed_rule="Complete quotations as strict JSON")
    program = init_program(target, "pilot", target / "ppc.yaml")
    probe = ModelProbeResult(
        requested_provider="claude", requested_model="claude-haiku-4-5",
        effective_provider="claude", effective_models=["claude-haiku-4-5"],
        identity=IdentityEvidence(policy="warn", status="exact", accepted=True,
                                  accepted_models=["claude-haiku-4-5"]),
        capabilities={}, live=False, calls=0,
        adapter_digest="ppc-fake-v1", probed_at="2026-01-01T00:00:00+00:00",
        expires_at="2100-01-01T00:00:00+00:00",
    )
    Engine(program, director_provider=director, executor_provider=ArticleExecutor(),
           model_probe=probe).run()
    state = program.load_state()
    assert len(state["experiments"]) == 3
    assert state["experiments"][-1]["ppc_role"] == "compose"
    assert state["experiments"][-1]["kept"]
    assert state["confirmation"]["ran"]
    assert state["promotion"]["status"] == "pending"
