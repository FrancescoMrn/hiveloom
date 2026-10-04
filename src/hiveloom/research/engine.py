"""The research engine: a deterministic state machine that owns a program.

The director proposes; the engine disposes. Every state is one *unit*, and
``step()`` advances exactly one, so a program is resumable from its files and
never depends on anyone's memory of a conversation:

    baseline → survey → hypothesize → experiment… → interpret → survey → …
                  └── (a stop condition) ──► confirm → report → done

The engine alone runs evals, debits the budget, measures, decides verdicts,
keeps or discards candidates, checks stop conditions, reads the sealed split
(once), and queues the promotion bundle. The director — a confined hiveloom
harness — reaches the program only through the typed tools of
:class:`DirectorSession`, and only the ones its current phase allows.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hiveloom import runner
from hiveloom.errors import SpecError
from hiveloom.eval_runner import case_key_for, resume_eval, run_eval
from hiveloom.evals import case_model_input
from hiveloom.evolve import stats
from hiveloom.evolve.assess import (
    MIN_PAIRS,
    _measure_metric,
    _measure_rate,
    _rate_indicator,
    assess_evolution,
)
from hiveloom.evolve.evolver import MutationProposal, apply_proposal, gate
from hiveloom.evolve.signal import locate_signal
from hiveloom.logging.hive import Hive
from hiveloom.spec.loader import dump_spec, load_spec, spec_to_dict
from hiveloom.spec.schema import HarnessSpec

from .concepts import ConceptsEngine, FrameTools
from .execution import ResearchToolPolicy, resolve
from .planning import CostModel, decide_look, look_sizes, power_plan
from .program import Program, _covered

DIRECTOR_DIR = Path(__file__).parent / "director"
PHASE_TOOLS = {
    "frame": {"brief", "propose_contract", "add_cases", "ask", "note"},
    "hypothesize": {"brief", "runs", "excerpt", "diff", "register_hypothesis",
                    "design_experiment", "move_budget", "note"},
    "interpret": {"brief", "runs", "excerpt", "diff", "interpret", "ask", "note"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Engine(ConceptsEngine):
    def __init__(
        self,
        program: Program,
        *,
        director_provider: Any = None,
        executor_provider: Any = None,
        model_probe: Any = None,
        models: dict[str, Any] | None = None,
    ):
        self.program = program
        self.charter = program.charter
        self._director_provider = director_provider
        self._executor_provider = executor_provider
        self._model_probe = model_probe
        # Loading c0 registers the harness's extensions, whose providers may serve
        # the director, the examiner and the judges before any eval has run.
        load_spec(program.candidate_dir("c0"))
        # Strong models by selector (examiner, judges); injected by tests and demos.
        self._models: dict[str, Any] = dict(models or {})
        if self.charter.concepts_mode:
            from .evaluation import register_panel
            from .judges import JudgePanel

            injected = {k: v for k, v in self._models.items() if k in self.charter.models.judges}
            if injected:
                register_panel(program.root, JudgePanel(program.root, self.charter.models.judges,
                                                        strong_models=injected,
                                                        base=program.base))

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #
    def step(self) -> dict[str, Any]:
        with self.program.lock():
            return self._step()

    def _step(self) -> dict[str, Any]:
        state = self.program.load_state()
        unit = state["unit"]
        stop_requested = self.program.stop_requested()
        if stop_requested and unit not in ("confirm", "report", "done") \
                and state["status"] != "blocked":
            state["stop_reason"] = state.get("stop_reason") or {
                "condition": "user", "detail": stop_requested}
            state["unit"] = "confirm"
            state["status"] = "active"
            self.program.save_state(state)
            return {"unit": unit, "outcome": "stopped by the user"}
        if state["status"] == "awaiting_user":
            return {"unit": unit, "outcome": "waiting for the contract to be approved",
                    "awaiting": "contract"}
        if state["status"] not in ("active", "confirming"):
            return {"unit": unit, "status": state["status"], "done": True,
                    "outcome": state.get("blocked_reason") or state["status"]}
        handler = getattr(self, f"_unit_{unit}", None)
        if handler is None:
            return {"unit": unit, "done": True}
        outcome = handler(state)
        self.program.save_state(state)
        return {"unit": unit, **outcome}

    def run(self, until: str = "done", max_steps: int = 500) -> list[dict[str, Any]]:
        return list(self.iter_run(until, max_steps))

    def iter_run(self, until: str = "done", max_steps: int = 500):
        """Yield each unit's result as it finishes (a job reports progress from this)."""
        for _ in range(max_steps):
            before = self.program.load_state()
            result = self.step()
            yield result
            after = self.program.load_state()
            if after["unit"] == "done" or result.get("done") or result.get("awaiting"):
                break
            # A round is done once it has been interpreted (or the program ended).
            if until == "round" and before["unit"] == "interpret":
                break
            if until == "unit":
                break

    def request_stop(self, reason: str = "requested") -> None:
        self.program.request_stop(reason)
        self.program.ledger.append("stop_requested", reason=reason)

    def status(self) -> dict[str, Any]:
        state = self.program.load_state()
        pools = {name: {"size": round(p.size, 6), "spent": round(p.spent, 6),
                        "left": round(p.left, 6)}
                 for name, p in self.program.budget.pools().items() if p.size or p.spent}
        return {
            "name": state["name"],
            "status": state["status"],
            "unit": state["unit"],
            "round": state["round"],
            "incumbent": state["incumbent"],
            "candidates": len(state["candidates"]),
            "hypotheses": len(state["hypotheses"]),
            "experiments": [
                {k: e.get(k) for k in ("id", "hypothesis", "candidate", "verdict", "kept",
                                       "stopped_early")}
                for e in state["experiments"]
            ],
            "budget": pools,
            "stop_reason": state.get("stop_reason"),
            "confirmation": state.get("confirmation"),
            "promotion": _live_promotion(state.get("promotion")),
            "ledger": self.program.ledger.verify(),
            "mode": "concepts" if self.charter.concepts_mode else "eval",
            "awaiting": "contract" if state["unit"] == "approve" else None,
            "blocked_reason": state.get("blocked_reason"),
            "questions": ({"open": len(self.questions.open()),
                           "asked": len(self.questions.all()),
                           "budget": self.charter.human.questions}
                          if self.charter.concepts_mode else None),
        }

    # ------------------------------------------------------------------ #
    # Units
    # ------------------------------------------------------------------ #
    def _unit_baseline(self, state: dict[str, Any]) -> dict[str, Any]:
        manifest, _cost = self._eval("c0", state["split"]["working"], pool="experiments",
                                     state=state, purpose="baseline")
        costs = [cell.cost_usd for cell in manifest.cells if cell.status == "completed"]
        model = CostModel.from_costs(costs)
        state["cost_model"] = {"runs": model.runs, "mean_usd": model.mean_usd,
                               "p90_usd": model.p90_usd}
        state["baseline_eval"] = manifest.eval_run_id
        self.program.ledger.append("baseline", eval_run_id=manifest.eval_run_id,
                                   cells=len(manifest.cells), cost_model=state["cost_model"])
        if self.charter.concepts_mode:
            self.relabel(state)
            self.ask_labels(state, "label")
        state["unit"] = "survey"
        return {"outcome": f"baseline measured on {len(manifest.cells)} working cells"}

    def _unit_survey(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.charter.concepts_mode:
            # Labels may have arrived since the last round: re-decide outcomes
            # before anything reads them, then audit the incumbent's judges.
            self.relabel(state)
            if state["round"] >= 1:
                self.ask_labels(state, "audit")
            self.ask_labels(state, "label")
        stop = self._stop_condition(state)
        if stop is not None:
            state["stop_reason"] = stop
            self.program.ledger.append("stopped", **stop)
            state["unit"] = "confirm"
            return {"outcome": f"stop: {stop['condition']}", "stop": stop}
        state["round"] += 1
        self.program.ledger.append("round_started", round=state["round"],
                                   incumbent=state["incumbent"])
        state["unit"] = "hypothesize"
        return {"outcome": f"round {state['round']}"}

    def _unit_hypothesize(self, state: dict[str, Any]) -> dict[str, Any]:
        session = DirectorSession(self, state, "hypothesize")
        self._run_director(session, state)
        designed = [e for e in state["pending_experiments"] if e["round"] == state["round"]]
        state["unit"] = "experiment" if designed else "interpret"
        return {"outcome": f"{len(designed)} experiment(s) designed",
                "hypotheses": [h["id"] for h in state["hypotheses"]
                               if h["round"] == state["round"]]}

    def _unit_experiment(self, state: dict[str, Any]) -> dict[str, Any]:
        # Peeked, not popped: an interrupted experiment is still pending on resume.
        experiment = state["pending_experiments"][0]
        result = self._run_experiment(experiment, state)
        state["pending_experiments"] = [e for e in state["pending_experiments"]
                                        if e["id"] != experiment["id"]]
        state["experiments"].append(result)
        state["unit"] = "experiment" if state["pending_experiments"] else "interpret"
        return {"outcome": f"{result['id']}: {result['verdict']}", "experiment": result["id"]}

    def _unit_interpret(self, state: dict[str, Any]) -> dict[str, Any]:
        undecided = [e for e in state["experiments"]
                     if e["round"] == state["round"] and not e.get("decided")]
        eligible = [e for e in undecided
                    if e["verdict"] in ("confirmed", "improved") and e["guard_ok"]
                    and e["base"] == state["incumbent"]]
        # A confirmed prediction outranks a success-only gain; then the larger effect.
        kept = max(eligible, key=lambda e: (e["verdict"] == "confirmed", e["measured_effect"],
                                            e.get("success_gain", 0.0)), default=None)
        for experiment in undecided:
            experiment["decided"] = True
        if kept is not None:
            kept["kept"] = True
            state["incumbent"] = kept["candidate"]
            state["kept_this_round"] = state["round"]
            self.program.ledger.append("kept", experiment=kept["id"],
                                       candidate=kept["candidate"])
            runners_up = [e for e in eligible if e is not kept and not e.get("stacked_from")]
            if runners_up and self._stack(state, kept, runners_up):
                state["unit"] = "experiment"
                return {"outcome": f"kept {kept['candidate']}; re-testing "
                                   f"{len(runners_up)} other winner(s) on top of it"}
        if state.get("kept_this_round") == state["round"]:
            state["rounds_without_progress"] = 0
        else:
            state["rounds_without_progress"] += 1
        session = DirectorSession(self, state, "interpret")
        self._run_director(session, state)
        handoff = session.handoff or {"findings": [], "next_focus": [],
                                      "decision": "continue", "stop_reason": None,
                                      "defaulted": True}
        state.setdefault("handoffs", []).append({"round": state["round"], **handoff})
        self.program.ledger.append("interpreted", round=state["round"], **handoff)
        if handoff["decision"] == "stop":
            state["stop_reason"] = {"condition": "director",
                                    "detail": handoff.get("stop_reason") or ""}
            self.program.ledger.append("stopped", **state["stop_reason"])
            state["unit"] = "confirm"
        else:
            state["unit"] = "survey"
        return {"outcome": f"kept {kept['candidate']}" if kept else "nothing kept",
                "decision": handoff["decision"]}

    def _unit_confirm(self, state: dict[str, Any]) -> dict[str, Any]:
        state["status"] = "confirming"
        incumbent, holdout = state["incumbent"], state["split"]["holdout"]
        if incumbent == "c0":
            state["confirmation"] = {"ran": False, "reason": "no change was kept",
                                     "strength": None}
        elif not holdout:
            state["confirmation"] = {"ran": False, "reason": "no held-out cases",
                                     "strength": "supported"}
        else:
            state["confirmation"] = self._confirm(state, incumbent, holdout)
            if self.charter.concepts_mode:
                state["confirmation"] = self.adjust_confirmation(state, state["confirmation"])
        self.program.ledger.append("confirmation", **state["confirmation"])
        state["unit"] = "report"
        return {"outcome": f"confirmation: {state['confirmation'].get('strength')}"}

    def _unit_report(self, state: dict[str, Any]) -> dict[str, Any]:
        if state["incumbent"] != "c0":
            state["promotion"] = self._queue_promotion(state)
        if self.charter.concepts_mode:
            withdrawn = self.questions.withdraw_open()
            if withdrawn:
                self.program.ledger.append("questions_withdrawn", count=withdrawn)
        (self.program.root / "report.md").write_text(self._report(state), encoding="utf-8")
        self.program.ledger.append("finished", promotion=state.get("promotion"))
        state["status"] = "done"
        state["unit"] = "done"
        return {"outcome": "report written", "done": True}

    # ------------------------------------------------------------------ #
    # Stop conditions
    # ------------------------------------------------------------------ #
    def _stop_condition(self, state: dict[str, Any]) -> dict[str, Any] | None:
        charter = self.charter
        started = datetime.fromisoformat(state["started_at"])
        if (datetime.now(UTC) - started).total_seconds() >= charter.budget.wall_clock_minutes * 60:
            return {"condition": "time", "detail": "wall clock budget elapsed"}
        if state["round"] >= charter.budget.rounds:
            return {"condition": "rounds", "detail": f"{charter.budget.rounds} rounds done"}
        if state["rounds_without_progress"] >= charter.stop.no_progress_rounds:
            return {"condition": "no_progress",
                    "detail": f"{state['rounds_without_progress']} rounds without a kept change"}
        if self.program.budget.left("exploration") <= 1e-9:
            return {"condition": "budget", "detail": "the exploration pool is spent"}
        plan = self._plan(state)
        if plan is not None and plan.experiments_affordable == 0:
            return {"condition": "budget", "detail": "the experiments pool cannot pay for "
                    "another experiment at this size"}
        goal = self._goal_met(state)
        if goal is not None:
            return {"condition": "goal", "detail": goal}
        ceiling = self._ceiling(state)
        if ceiling is not None:
            return {"condition": "ceiling", "detail": ceiling["detail"],
                    "recommendation": ceiling["recommendation"]}
        return None

    def _goal_met(self, state: dict[str, Any]) -> str | None:
        if self.charter.concepts_mode:
            reached = self.contract_goal(state)
            if reached is not None:
                return reached
        goals = self.charter.stop.goal
        if not goals:
            return None
        with Hive() as hive:
            runs = self._working_runs(hive, state["incumbent"], state)
            reached = []
            for key, threshold in goals.items():
                if key == "success_rate":
                    value = (sum(not r["failed"] for r in runs) / len(runs)) if runs else 0.0
                else:
                    values = hive.metric_values([r["run_id"] for r in runs], key.split(":", 1)[1])
                    value = (sum(values.values()) / len(values)) if values else float("-inf")
                if value < threshold:
                    return None
                reached.append(f"{key} {value:.3g} ≥ {threshold}")
        return "goal reached on the working split: " + ", ".join(reached)

    def _ceiling(self, state: dict[str, Any]) -> dict[str, Any] | None:
        # Content errors are not beyond every lever (a prompt can change what a
        # model answers), so "nothing here reaches them" needs a round that tried
        # and kept nothing — never a guess made before the first attempt.
        if state["rounds_without_progress"] < 1:
            return None
        signal_map = self._signal(state)
        if signal_map.quality.failures == 0:
            return None
        if signal_map.loss.content_share < self.charter.stop.ceiling_content_share:
            return None
        addressable = [s for s in signal_map.signals
                       if s.addressable and s.strength != "weak" and s.direction == "risk"]
        # A verifier rejecting finished answers is what content loss looks like
        # from the inside; it cannot also be the evidence that a lever remains.
        mechanisms = [m for m in signal_map.mechanisms
                      if m.addressable and m.failed_runs and m.category != "verifier_failure"]
        if addressable or mechanisms:
            return None
        share = signal_map.loss.content_share
        return {
            "detail": f"{share:.0%} of the remaining failures are content errors "
                      "(the model finished and was wrong) and no signal has a lever in "
                      "this charter",
            "recommendation": "consider a stronger executor model (frozen from evolution: "
                              "the operator's decision), widening the charter's levers so "
                              "the harness can fetch what the model lacks, or narrowing "
                              "the task",
        }

    # ------------------------------------------------------------------ #
    # Measurement
    # ------------------------------------------------------------------ #
    def _executor(self, *, manifest, cell, case, spec):
        return runner.run_harness(
            manifest.harness_path,
            case_model_input(case, spec.dataset),
            literal_input=True,
            run_id=cell.run_id,
            trace_dir=manifest.trace_root,
            model_override=manifest.requested_model,
            provider_override=manifest.requested_provider,
            provider=self._executor_provider,
            context={"eval_run_id": manifest.eval_run_id, "eval_cell_id": cell.cell_id,
                     "research_program": self.program.name},
            tool_policy=self._policy_for(manifest.harness_path),
            cost_cap_usd=self.charter.execution.max_cost_per_run,
        )

    def _policy_for(self, harness_path: str) -> ResearchToolPolicy:
        """Each candidate runs under the policy resolved from its own tools."""
        cache = self.__dict__.setdefault("_policies", {})
        if harness_path not in cache:
            spec = load_spec(harness_path)
            cache[harness_path] = ResearchToolPolicy(resolve(spec, self.charter.execution),
                                                     self.program.replay)
        return cache[harness_path]

    def _eval(self, candidate: str, case_ids: list[str], *, pool: str, state: dict[str, Any],
              purpose: str, max_cells: int | None = None, resume: str | None = None):
        """Run (or continue) an eval of one candidate; debit what it newly cost.

        Each eval has a purpose (``baseline``, ``experiment:e3``, ``confirm:c0``)
        whose eval id is saved *before* any cell runs, so an interrupted unit
        resumes the same eval on its next step rather than paying for it again.
        """
        from hiveloom.eval_runner import load_eval_manifest, new_eval_run_id

        judged_before = self.panel().spent_usd if self.charter.concepts_mode else 0.0
        inflight = state.setdefault("inflight", {})
        if resume is None and purpose in inflight:
            try:
                load_eval_manifest(inflight[purpose])
                resume = inflight[purpose]
            except (OSError, ValueError, LookupError):
                inflight.pop(purpose)
        if resume is None:
            inflight[purpose] = new_eval_run_id()
            self.program.save_state(state)
            manifest = run_eval(
                self.program.eval_path(candidate),
                case_ids=case_ids,
                execute_cell=self._executor,
                model_probe=self._model_probe,
                approve_trust=lambda _p: True,
                max_cells=max_cells,
                eval_run_id=inflight[purpose],
            )
        else:
            try:
                manifest = resume_eval(
                    resume, execute_cell=self._executor, model_probe=self._model_probe,
                    approve_trust=lambda _p: True, max_cells=max_cells,
                )
            except ValueError as exc:
                if inflight.get(purpose) != resume:
                    raise
                # The interrupted eval cannot continue (its harness or model moved):
                # start it again; what it already cost stays debited to its own id.
                self.program.ledger.append("eval_restarted", purpose=purpose,
                                           eval_run_id=resume, reason=str(exc)[:300])
                inflight.pop(purpose)
                return self._eval(candidate, case_ids, pool=pool, state=state,
                                  purpose=purpose, max_cells=max_cells)
        total = sum(cell.cost_usd for cell in manifest.cells)
        # What was already debited is read from the ledger, not from state that
        # an interruption may not have saved.
        debited = sum(float(e["data"]["usd"]) for e in self.program.ledger.of_kind("debit")
                      if e["data"].get("eval_run_id") == manifest.eval_run_id
                      and e["data"].get("role") != "judges")
        if round(total - debited, 6) > 0:
            self.program.budget.debit(pool, total - debited, eval_run_id=manifest.eval_run_id,
                                      candidate=candidate)
        if self.charter.concepts_mode:
            judged = self.panel().spent_usd - judged_before
            if judged > 0:
                self.program.budget.debit(pool, judged, role="judges",
                                          eval_run_id=manifest.eval_run_id)
            # Outcomes are what every measurement reads: decide them now.
            self.relabel(state)
        return manifest, total - debited

    def _working_runs(self, hive: Hive, candidate: str, state: dict[str, Any]) -> list[dict]:
        version = state["candidates"][candidate]["version"]
        working = {case_key_for(cid) for cid in state["split"]["working"]}
        keyed = self._run_case_keys(hive, version)
        return [run for run in hive.feature_population(self.program.key(), version=version)
                if keyed.get(run["run_id"]) in working]

    def _run_case_keys(self, hive: Hive, version: str) -> dict[str, str]:
        rows = hive._conn.execute(
            "SELECT c.run_id, c.case_key FROM eval_cells c JOIN runs r ON r.run_id = c.run_id "
            "WHERE r.harness_key=? AND r.harness_version_hash=?",
            (self.program.key(), version),
        )
        return {row["run_id"]: row["case_key"] for row in rows}

    def _signal(self, state: dict[str, Any]):
        incumbent = state["incumbent"]
        spec = load_spec(self.program.candidate_dir(incumbent))
        # "Addressable" means reachable by this charter's levers, not by every
        # path the harness would let evolution touch: that is what makes the
        # ceiling say "not from here".
        evolution = spec.evolution.model_copy(update={"mutable": list(self.charter.levers)})
        with Hive() as hive:
            return locate_signal(hive, self.program.key(),
                                 version=state["candidates"][incumbent]["version"],
                                 evolution=evolution)

    def _plan(self, state: dict[str, Any]):
        model = state.get("cost_model")
        if not model:
            return None
        with Hive() as hive:
            runs = self._working_runs(hive, state["incumbent"], state)
        rate = (sum(not r["failed"] for r in runs) / len(runs)) if runs else 0.5
        cells = len(runs) or len(state["split"]["working"])
        return power_plan(CostModel(model["runs"], model["mean_usd"], model["p90_usd"]),
                          cells, rate, self.program.budget.left("experiments"))

    def _pairs(self, hive: Hive, old: str, new: str, keys: set[str]) -> list[tuple[str, str]]:
        return [
            (pair["left"]["run_id"], pair["right"]["run_id"])
            for pair in hive.eval_pairs(self.program.key(), old, new)
            if pair["case_key"] in keys
        ]

    def _measure(self, hive: Hive, target: str, old: str, new: str, keys: set[str]):
        runs_before = [r for r in hive.feature_population(self.program.key(), version=old)]
        runs_after = [r for r in hive.feature_population(self.program.key(), version=new)]
        pairs = self._pairs(hive, old, new, keys)
        in_pairs_before = {left for left, _ in pairs}
        in_pairs_after = {right for _, right in pairs}
        before = [r for r in runs_before if r["run_id"] in in_pairs_before]
        after = [r for r in runs_after if r["run_id"] in in_pairs_after]
        success = _measure_rate("success_rate", _rate_indicator(hive, "success_rate", before),
                                _rate_indicator(hive, "success_rate", after), pairs)
        if target == "success_rate":
            target_measure = success
        elif target.startswith("metric:"):
            name = target.split(":", 1)[1]
            target_measure = _measure_metric(
                target, hive.metric_values([r["run_id"] for r in before], name),
                hive.metric_values([r["run_id"] for r in after], name), pairs)
        else:
            target_measure = _measure_rate(
                f"share of runs with {target}", _rate_indicator(hive, target, before),
                _rate_indicator(hive, target, after), pairs)
        return success, target_measure, before, after

    def _run_experiment(self, experiment: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        hypothesis = next(h for h in state["hypotheses"] if h["id"] == experiment["hypothesis"])
        incumbent, candidate = experiment["base"], experiment["candidate"]
        old = state["candidates"][incumbent]["version"]
        new = state["candidates"][candidate]["version"]
        working = state["split"]["working"]
        keys = {case_key_for(cid) for cid in working}
        plan = self._plan(state)
        if plan is not None and plan.cost_mean > 0 and \
                not self.program.budget.can_afford("experiments", plan.cost_mean):
            return {**experiment, "verdict": "unaffordable", "kept": False, "guard_ok": False,
                    "measured_effect": 0.0, "stopped_early": None}

        # Staged looks over the cells, in the eval runner's own order: harm may
        # stop early at any look, futility when even a perfect remainder cannot
        # reach significance; a benefit is only read at the planned size.
        purpose = f"experiment:{experiment['id']}"
        manifest, _ = self._eval(candidate, working, pool="experiments", state=state,
                                 purpose=purpose, max_cells=0)
        total = len(manifest.cells)
        decision = "continue"
        for size in look_sizes(total):
            done = sum(cell.status == "completed" for cell in manifest.cells)
            if size > done:
                manifest, _ = self._eval(candidate, working, pool="experiments", state=state,
                                         purpose=purpose, resume=manifest.eval_run_id,
                                         max_cells=size - done)
            with Hive() as hive:
                success, target, _b, _a = self._measure(hive, hypothesis["target"], old, new, keys)
            toward, against = _oriented(target, hypothesis["expect"])
            # A cell that exhausted its retries will never complete: not "remaining".
            remaining = sum(cell.status in ("pending", "running", "ran")
                            for cell in manifest.cells)
            decision = decide_look(
                success_improved=success.improved_pairs or 0,
                success_worsened=success.worsened_pairs or 0,
                target_toward=toward, target_against=against, remaining=remaining,
            )
            if decision == "futile" and decide_look(
                success_improved=0, success_worsened=0,
                target_toward=success.improved_pairs or 0,
                target_against=success.worsened_pairs or 0, remaining=remaining,
            ) == "continue":
                # The target cannot get there, but the success rate still can:
                # an "improved" verdict is still on the table.
                decision = "continue"
            if decision == "futile" and (target.pairs or 0) < MIN_PAIRS:
                # Too few pairs to count discordance yet: futility is not judged
                # on a sample the paired test would not even read.
                decision = "continue"
            if decision == "futile" and stats.binomial_two_sided(0, total) >= stats_alpha():
                # Even a perfect full sample could not be significant (the power
                # plan says so up front); run it out and let the verdict say so.
                decision = "continue"
            if decision != "continue":
                break

        with Hive() as hive:
            success, target, before, after = self._measure(
                hive, hypothesis["target"], old, new, keys)
            row = {
                "evolution_id": 0,
                "old_version_hash": old,
                "new_version_hash": new,
                "prediction": {"target": {"signal": hypothesis["target"],
                                          "expect": hypothesis["expect"],
                                          "by": hypothesis.get("by")},
                               "objective_expectations": []},
                "changes": {"paths": [c["path"] for c in experiment["changes"]], "code": []},
            }
            assessment = assess_evolution(hive, self.program.key(), row, min_runs=1)
        verdict = "regressed" if decision == "harm" else assessment.verdict
        if (verdict in ("refuted", "inconclusive") and decision == "complete"
                and success.moved() == "increase"):
            # The goal moved even though the predicted mechanism did not (a
            # retried call still logs its first error, say). Worth keeping, and
            # told apart from a confirmed prediction; calibration keeps the miss.
            verdict = "improved"
        shifted = 0
        if verdict == "confirmed" and hypothesis["target"] != "success_rate" \
                and (success.after or 0.0) <= (success.before or 0.0):
            with Hive() as hive:
                pairs = self._pairs(hive, old, new, keys)
                statuses = {row["run_id"]: row["status"] for row in [*before, *after]}
            shifted = _shifted(pairs, statuses)
            toward, _against = _oriented(target, hypothesis["expect"])
            if shifted and shifted * 2 >= max(1, toward):
                # The counted mechanism moved because runs stopped answering (the
                # turn limit, an error, a halt): the failures moved, they did not
                # go away. Not kept, and said so.
                verdict = "shifted"
        effect = _effect(target, hypothesis["expect"])
        guard_ok = verdict != "regressed" and self._guard_ok(before, after)
        calibration = None
        if hypothesis.get("by") is not None:
            calibration = {"hypothesis": hypothesis["id"], "predicted": hypothesis["by"],
                           "measured": round(effect, 4),
                           "gap": round(hypothesis["by"] - effect, 4)}
            state["calibration"].append(calibration)
        result = {
            **experiment,
            "shifted_pairs": shifted,
            "eval_run_id": manifest.eval_run_id,
            "verdict": verdict,
            "stopped_early": decision if decision in ("harm", "futile") else None,
            "guard_ok": guard_ok,
            "measured_effect": round(effect, 4),
            "success_gain": round((success.after or 0.0) - (success.before or 0.0), 4),
            "success": success.describe(),
            "target_measure": target.describe(),
            "summary": assessment.summary,
            "kept": False,
            "calibration": calibration,
        }
        # A futility stop is an answer too: the idea showed nothing where it
        # could have, so it is not re-tested without new evidence.
        hypothesis["status"] = "futile" if decision == "futile" else verdict
        self.program.ledger.append(
            "experiment_assessed",
            **{k: result[k] for k in ("id", "hypothesis", "candidate", "verdict",
                                      "stopped_early", "guard_ok", "measured_effect",
                                      "success", "target_measure")},
        )
        return result

    def _objective_expectations(
        self, spec: HarnessSpec, target: str | None
    ) -> list[dict[str, str]]:
        """The metric objectives a research change is accountable to.

        A harness that declares `evolution.objectives` only accepts a proposal
        that names at least one of them (the gate's rule, so a change can be
        assessed against what the harness is for). Without this every
        experiment on such a harness was refused and the director spent its
        rounds probing the gate. The direction always comes from the objective
        itself; which objectives, in order of preference: the hypothesis's own
        target when it is one, else the charter's goal metrics, else all.
        """
        objectives = {o.metric: o for o in spec.evolution.objectives}
        if not objectives:
            return []
        wanted: list[str] = []
        if target and target.startswith("metric:") and target[7:] in objectives:
            wanted = [target[7:]]
        if not wanted:
            wanted = [key[7:] for key in self.charter.stop.goal
                      if key.startswith("metric:") and key[7:] in objectives]
        if not wanted:
            wanted = list(objectives)
        return [
            {"metric": metric,
             "expected_change": "increase" if objectives[metric].direction == "maximize"
             else "decrease",
             "rationale": "a configured objective this research program answers to"}
            for metric in wanted
        ]

    def make_experiment(self, state: dict[str, Any], base: str, hypothesis: dict[str, Any],
                        changes: list[dict[str, Any]], **extra: Any) -> tuple[dict, str]:
        """A candidate from ``base`` plus ``changes`` (gated), queued as an experiment."""
        base_spec = load_spec(self.program.candidate_dir(base))
        proposal = MutationProposal.model_validate({
            "rationale": hypothesis["claim"],
            "target": {"signal": hypothesis["target"], "expect": hypothesis["expect"]},
            "objective_expectations": self._objective_expectations(
                base_spec, hypothesis["target"]),
            "yaml_changes": changes,
        })
        verdict = gate(base_spec, proposal)
        if verdict.rejected:
            raise ValueError("refused by the gate: " + "; ".join(
                f"{r['path']}: {r['reason']}" for r in verdict.rejected))
        # Numbered past every folder on disk too: an interrupted unit can leave a
        # candidate folder its state never recorded.
        on_disk = [int(p.name[1:]) for p in (self.program.root / "candidates").glob("c*")
                   if p.name[1:].isdigit()]
        candidate = f"c{max([len(state['candidates']) - 1, *on_disk]) + 1}"
        directory = self.program.copy_candidate(self.program.candidate_dir(base), candidate)
        try:
            applied = apply_proposal(directory, proposal, hive=None, apply_yaml=True)
        except Exception as exc:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError(f"the change does not produce a valid harness: {exc}") from exc
        problem = self._tool_problem(base, candidate)
        if problem:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError(problem)
        version = self.program.version_of(candidate)
        tested = next((cid for cid, info in state["candidates"].items()
                       if info["version"] == version), None)
        if not applied.changed or tested is not None:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)
            if not applied.changed or tested == base:
                raise ValueError("the changes produced no difference from the incumbent")
            raise ValueError(f"this exact harness was already tested as {tested}; its "
                             "verdict stands")
        state["candidates"][candidate] = {
            "parent": base, "hypothesis": hypothesis["id"],
            "changes": [c.model_dump(mode="json") for c in applied.applied_yaml],
            "version": version,
        }
        experiment = {"id": f"e{len(state['experiments']) + len(state['pending_experiments']) + 1}",
                      "round": state["round"], "hypothesis": hypothesis["id"],
                      "candidate": candidate, "base": base,
                      "changes": state["candidates"][candidate]["changes"], **extra}
        state["pending_experiments"].append(experiment)
        self.program.ledger.append("experiment_designed", experiment=experiment["id"],
                                   hypothesis=hypothesis["id"], candidate=candidate,
                                   paths=[c["path"] for c in experiment["changes"]], **extra)
        return experiment, _short_diff(base_spec, load_spec(directory))

    def _tool_problem(self, base: str, candidate: str) -> str | None:
        """A candidate may not bring tools, or tool declarations, the charter never saw.

        Research runs execute every tool under the policy resolved from the
        charter; a new effectful tool, or a widened declaration of one the
        charter classified (more hosts, other commands), would otherwise run
        as if allowed.
        """
        from .execution import classify

        before = load_spec(self.program.candidate_dir(base))
        after = load_spec(self.program.candidate_dir(candidate))
        policy = resolve(after, self.charter.execution)
        # A tool the charter classified but the candidate dropped is not a problem.
        errors = [e for e in policy.errors if "declares no such" not in e]
        if errors:
            return ("the change brings tools the charter does not classify: "
                    + "; ".join(errors))
        defaults = {c.name: c.default for c in classify(before)}
        def declared(ref) -> dict[str, Any]:
            # What the tool does, not how it is described to the model.
            return {k: v for k, v in ref.model_dump(mode="json").items()
                    if k not in ("description", "deferred")}

        raw_before = {_tool_name(ref): declared(ref) for ref in before.tools}
        for ref in after.tools:
            name = _tool_name(ref)
            if name in raw_before and raw_before[name] != declared(ref) \
                    and defaults.get(name) is None:
                return (f"tool '{name}' has effects the charter classified as declared; "
                        "its declaration cannot change inside a program")
        if [s.model_dump(mode="json") for s in after.mcp_servers] != \
                [s.model_dump(mode="json") for s in before.mcp_servers]:
            return "MCP servers cannot change inside a program"
        if after.delegation.model_dump(mode="json") != before.delegation.model_dump(mode="json"):
            return ("delegation cannot change inside a program: a peer runs its own tools, "
                    "outside the research execution policy")
        return None

    def _stack(self, state: dict[str, Any], kept: dict[str, Any],
               runners_up: list[dict[str, Any]]) -> list[str]:
        """Re-test each other winner of the round on top of the one kept.

        Every experiment of a round was measured against the same incumbent, so
        keeping one would silently discard another confirmed fix. Its changes are
        rebased three-way (its base, the new incumbent, its value) and measured
        again against the new incumbent; a change that cannot be rebased is
        reported, not forced.
        """
        queued, skipped = [], []
        incumbent_raw = spec_to_dict(load_spec(self.program.candidate_dir(kept["candidate"])))
        for experiment in runners_up:
            base_raw = spec_to_dict(load_spec(self.program.candidate_dir(experiment["base"])))
            rebased = []
            for change in experiment["changes"]:
                value = _rebase(change, base_raw, incumbent_raw)
                if value is _CONFLICT:
                    rebased = None
                    break
                rebased.append({**change, "value": value})
            hypothesis = next(h for h in state["hypotheses"]
                              if h["id"] == experiment["hypothesis"])
            if not rebased:
                skipped.append(experiment["id"])
                continue
            try:
                stacked, _ = self.make_experiment(state, kept["candidate"], hypothesis, rebased,
                                                  stacked_from=experiment["id"])
            except ValueError as exc:
                skipped.append(f"{experiment['id']} ({exc})")
                continue
            queued.append(stacked["id"])
        if queued or skipped:
            self.program.ledger.append("stacked", onto=kept["candidate"], queued=queued,
                                       skipped=skipped)
        return queued

    def _guard_ok(self, before: list[dict], after: list[dict]) -> bool:
        limit = self.charter.guard.cost_per_run_increase
        if limit is None or not before or not after:
            return True
        mean_before = sum(r["cost_usd"] for r in before) / len(before)
        mean_after = sum(r["cost_usd"] for r in after) / len(after)
        return mean_before == 0 or mean_after <= mean_before * (1 + limit)

    def _confirm(self, state: dict[str, Any], incumbent: str, holdout: list[str]) -> dict:
        """The one read of the sealed split: base vs final incumbent, paired."""
        model = state.get("cost_model") or {}
        estimate = 2 * len(holdout) * float(model.get("mean_usd", 0.0))
        resuming = bool(self.program.ledger.of_kind("sealed_read"))
        if estimate and not resuming and \
                not self.program.budget.can_afford("confirmation", estimate):
            return {"ran": False, "reason": "the confirmation pool cannot pay for it",
                    "strength": "provisional"}
        # The sealed split is read once: a confirmation resumed after an
        # interruption continues the same evals and records no second read.
        if not self.program.ledger.of_kind("sealed_read"):
            self.program.ledger.append("sealed_read", cases=len(holdout),
                                       candidates=["c0", incumbent])
        self._eval("c0", holdout, pool="confirmation", state=state, purpose="confirm:c0")
        self._eval(incumbent, holdout, pool="confirmation", state=state,
                   purpose=f"confirm:{incumbent}")
        keys = {case_key_for(cid) for cid in holdout}
        old = state["candidates"]["c0"]["version"]
        new = state["candidates"][incumbent]["version"]
        with Hive() as hive:
            success, _target, _b, _a = self._measure(hive, "success_rate", old, new, keys)
        if success.improved_pairs is not None:
            up, down = success.improved_pairs, success.worsened_pairs or 0
        else:  # too few pairs for a paired test: read the direction off the rates
            delta = (success.after or 0.0) - (success.before or 0.0)
            up, down = int(delta > 0), int(delta < 0)
        p = success.p_value
        if up > down and p < stats_alpha():
            strength = "confirmed"
        elif down > up and p < stats_alpha():
            strength = "contradicted"
        elif up > down:
            strength = "supported"  # the right direction, too few pairs to be sure
        else:
            strength = "provisional"
        return {"ran": True, "strength": strength, "success": success.describe(),
                "improved_pairs": success.improved_pairs, "worsened_pairs": success.worsened_pairs,
                "p_value": round(p, 4)}

    # ------------------------------------------------------------------ #
    # Director
    # ------------------------------------------------------------------ #
    def _run_director(self, session: DirectorSession, state: dict[str, Any]) -> None:
        left = self.program.budget.left("exploration")
        if left <= 0:
            # No money left for the director: it is not asked (its own YAML cap
            # is not the charter's), and the unit proceeds as if it had nothing to say.
            self.program.ledger.append("director_skipped", phase=session.phase,
                                       reason="the exploration pool is spent")
            return
        model = self.charter.models.director
        provider_name, _, model_id = model.partition("/")
        task = session.task_text()
        result = runner.run_harness(
            DIRECTOR_DIR,
            task,
            literal_input=True,
            context={"research": session},
            provider=self._director_provider,
            # An injected provider (a scripted director in tests and demos) is the
            # model; the charter's selector applies to a real one.
            model_override=None if self._director_provider else model_id,
            provider_override=None if self._director_provider else provider_name,
            trace_dir=self.program.director_dir,
            approve_trust=lambda _p: True,
            cost_cap_usd=left,
        )
        cost = float(result.cost_usd or 0.0)
        if cost:
            self.program.budget.debit("exploration", cost, director_run=result.run_id,
                                      phase=session.phase)
        self.program.ledger.append("director_run", phase=session.phase, run_id=result.run_id,
                                   status=result.status, cost_usd=cost,
                                   tool_calls=session.calls)

    # ------------------------------------------------------------------ #
    # Promotion and report
    # ------------------------------------------------------------------ #
    def _kept_chain(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        chain, current = [], state["incumbent"]
        while current and current != "c0":
            info = state["candidates"][current]
            chain.append({"candidate": current, **info})
            current = info["parent"]
        return list(reversed(chain))

    def _queue_promotion(self, state: dict[str, Any]) -> dict[str, Any]:
        from uuid import uuid4

        from hiveloom.logging.trace import spec_version_hash

        base = self.program.base
        live = load_spec(base)
        changes, conflicts = self._promotion_changes(state, live)
        strength = (state.get("confirmation") or {}).get("strength") or "provisional"
        first = next((h for h in state["hypotheses"]
                      if h["id"] == self._kept_chain(state)[0]["hypothesis"]), None)
        proposal = MutationProposal.model_validate({
            "rationale": f"research program '{self.program.name}': {len(changes)} change(s), "
                         f"evidence {strength}",
            "target": ({"signal": first["target"], "expect": first["expect"]}
                       if first else None),
            "objective_expectations": self._objective_expectations(
                live, first["target"] if first else None),
            "yaml_changes": changes,
        })
        result = gate(live, proposal)
        if conflicts:
            result.rejected.extend({"path": path, "reason": "the live harness changed this "
                                    "since the program started, and the edits conflict"}
                                   for path in conflicts)
            result.accepted = []
        now = _now()
        row = {
            "id": f"prop_{uuid4().hex[:16]}",
            "harness_name": live.identity,
            "spec_version_hash": spec_version_hash(live, base),
            "dedup_key": f"research:{self.program.key()}",
            "status": "pending" if result.accepted else "rejected",
            "trigger": "research",
            "rationale": proposal.rationale,
            "proposal_json": proposal.model_dump_json(),
            "gate_json": result.model_dump_json(),
            "evidence_json": json.dumps({
                "program": self.program.name, "strength": strength,
                "experiments": [e["id"] for e in state["experiments"] if e.get("kept")],
                "confirmation": state.get("confirmation"),
            }),
            "apply_result_json": None if result.accepted else json.dumps(
                {"reason": "the live harness no longer accepts these changes"
                 + (f" (conflicting edits to {', '.join(conflicts)})" if conflicts else "")}),
            "created_at": now,
            "resolved_at": None if result.accepted else now,
        }
        with Hive() as hive:
            stored = hive.insert_proposal(row)
        # An identical pending proposal already queued is returned instead: point at it.
        row = {**row, "id": stored.get("id", row["id"]),
               "status": stored.get("status", row["status"])}
        self.program.ledger.append("promotion_queued", proposal_id=row["id"],
                                   status=row["status"], strength=strength,
                                   changes=len(changes))
        return {"proposal_id": row["id"], "status": row["status"], "strength": strength,
                "changes": len(changes)}

    def _promotion_changes(self, state: dict[str, Any], live) -> tuple[list[dict], list[str]]:
        """What the program changed (c0 → incumbent), re-expressed on the live spec.

        The live harness may have been edited while the program ran; a promotion
        of full values computed against the old base would silently undo those
        edits. Each changed path is merged three-way instead, and a real
        conflict is reported rather than forced.
        """
        start = spec_to_dict(load_spec(self.program.candidate_dir("c0")))
        final = spec_to_dict(load_spec(self.program.candidate_dir(state["incumbent"])))
        now = spec_to_dict(live)
        paths: list[str] = []
        for step in self._kept_chain(state):
            for change in step["changes"]:
                path = change["path"][:-2] if change["path"].endswith(".+") else change["path"]
                if path not in paths:
                    paths.append(path)
        changes, conflicts = [], []
        for path in paths:
            value = _rebase({"path": path, "value": _at(final, path)}, start, now)
            if value is _CONFLICT:
                conflicts.append(path)
            elif value != _at(now, path):
                changes.append({"path": path, "value": value})
        return changes, conflicts

    def _report(self, state: dict[str, Any]) -> str:
        charter = self.charter
        lines = [f"# Research program: {self.program.name}", "", f"**Goal.** {charter.goal}", ""]
        stop = state.get("stop_reason") or {}
        lines += [f"**Stopped:** {stop.get('condition', '—')} — {stop.get('detail', '')}"]
        if stop.get("recommendation"):
            lines += ["", f"**Recommendation:** {stop['recommendation']}"]
        pools = self.program.budget.pools()
        lines += ["", "## Budget", ""]
        lines += [f"- {name}: ${p.spent:.4f} of ${p.size:.4f}" for name, p in pools.items()]
        lines += ["", "## Experiments", "", "| id | hypothesis | verdict | kept | effect |",
                  "|---|---|---|---|---|"]
        for e in state["experiments"]:
            lines.append(f"| {e['id']} | {e['hypothesis']} | {e['verdict']}"
                         f"{' (' + e['stopped_early'] + ')' if e.get('stopped_early') else ''}"
                         f" | {'yes' if e.get('kept') else 'no'} | {e.get('measured_effect')} |")
        lines += ["", "## Hypotheses", ""]
        for h in state["hypotheses"]:
            lines.append(f"- **{h['id']}** ({h.get('status', 'registered')}): {h['claim']} — "
                         f"target `{h['target']}` expected to {h['expect']}")
        if state["calibration"]:
            gaps = [c["gap"] for c in state["calibration"]]
            lines += ["", "## Calibration", "",
                      f"Mean predicted-minus-measured effect: {sum(gaps) / len(gaps):+.3f} "
                      f"over {len(gaps)} prediction(s)."]
        confirmation = state.get("confirmation") or {}
        lines += ["", "## Confirmation on the sealed split", "",
                  f"Evidence strength: **{confirmation.get('strength') or 'none'}**"
                  + (f" — {confirmation.get('success')}" if confirmation.get("success") else "")
                  + (f" ({confirmation.get('reason')})" if confirmation.get("reason") else "")]
        if confirmation.get("downgraded"):
            lines.append(f"Downgraded from confirmed: {confirmation['downgraded']}.")
        if self.charter.concepts_mode:
            lines += self.concepts_report(state)
        findings = [f for h in state.get("handoffs", []) for f in h.get("findings", [])]
        if findings:
            lines += ["", "## Findings", ""] + [f"- {f}" for f in findings]
        promotion = _live_promotion(state.get("promotion"))
        lines += ["", "## Promotion", ""]
        if promotion and promotion["status"] == "applied":
            lines.append(f"Proposal `{promotion['proposal_id']}` was applied"
                         + (f" as evolution #{promotion['counter']}" if promotion.get("counter")
                            else "")
                         + f" ({promotion['changes']} change(s)).")
        elif promotion and promotion["status"] != "pending":
            lines.append(f"Proposal `{promotion['proposal_id']}` was {promotion['status']}.")
        elif promotion:
            lines.append(f"Proposal `{promotion['proposal_id']}` ({promotion['status']}, "
                         f"{promotion['changes']} change(s)). Review and apply with "
                         f"`hiveloom proposals apply . {promotion['proposal_id']} --yes`.")
        else:
            lines.append("Nothing to promote.")
        return "\n".join(lines) + "\n"


def _live_promotion(promotion: dict[str, Any] | None) -> dict[str, Any] | None:
    """The promotion as it stands now, not as it stood when it was queued.

    The program's state records the proposal it queued with status "pending",
    and nothing writes back to it when that proposal is applied or rejected
    through the review queue — so reporting the stored status said "waiting on
    you" about a change that had already shipped. The Hive's proposal row is
    the source of truth; the stored copy is kept as what was queued.
    """
    if not promotion or not promotion.get("proposal_id"):
        return promotion
    try:
        with Hive() as hive:
            row = hive.get_proposal(promotion["proposal_id"])
    except Exception:  # noqa: BLE001 - a status read must not fail on the Hive
        return promotion
    if not row:
        return promotion
    live = {**promotion, "status": row.get("status") or promotion.get("status")}
    if row.get("resolved_at"):
        live["resolved_at"] = row["resolved_at"]
    try:
        applied = json.loads(row.get("apply_result_json") or "null") or {}
    except (TypeError, ValueError):
        applied = {}
    if live["status"] == "applied" and isinstance(applied, dict):
        for key in ("counter", "old_version_hash", "new_version_hash"):
            if applied.get(key) is not None:
                live[key] = applied[key]
    return live


def stats_alpha() -> float:
    from .planning import ALPHA

    return ALPHA


#: Statuses of a run that produced no answer to judge.
NO_ANSWER = frozenset({"max_turns", "truncated", "error", "guardrail_halt", "step_failed"})


def _shifted(pairs: list[tuple[str, str]], statuses: dict[str, str]) -> int:
    """Pairs whose candidate run stopped answering where the base run had answered."""
    return sum(1 for left, right in pairs
               if statuses.get(right) in NO_ANSWER and statuses.get(left) not in NO_ANSWER)


def _oriented(measure, expect: str) -> tuple[int, int]:
    up, down = measure.improved_pairs or 0, measure.worsened_pairs or 0
    return (up, down) if expect == "increase" else (down, up)


def _effect(measure, expect: str) -> float:
    if measure.before is None or measure.after is None:
        return 0.0
    delta = measure.after - measure.before
    return delta if expect == "increase" else -delta


# --------------------------------------------------------------------------- #
# The director's typed tools
# --------------------------------------------------------------------------- #
class DirectorSession(FrameTools):
    """What the director may do, for one phase of one round.

    Each tool validates against the charter and the program's state and either
    acts (recording it in the ledger) or returns a refusal with its reason. The
    director never receives sealed data, and nothing here writes a verdict.
    """

    def __init__(self, engine: Engine, state: dict[str, Any], phase: str):
        self.engine = engine
        self.state = state
        self.phase = phase
        self.calls: list[str] = []
        self.handoff: dict[str, Any] | None = None

    # -- dispatch ------------------------------------------------------------ #
    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        self.calls.append(tool)
        if tool not in PHASE_TOOLS[self.phase]:
            return _refuse(f"'{tool}' is not available in the {self.phase} phase")
        try:
            result = getattr(self, f"_{tool}")(**arguments)
        except (SpecError, ValueError, KeyError, TypeError) as exc:
            # Recorded, so a program that stalled on refusals says so afterwards.
            self.engine.program.ledger.append("refused", round=self.state["round"],
                                              phase=self.phase, tool=tool, reason=str(exc)[:400])
            return _refuse(str(exc))
        return json.dumps(result, ensure_ascii=False, default=str)

    def task_text(self) -> str:
        n = self.engine.charter.experiments_per_round
        if self.phase == "frame":
            per = self.engine.charter.dataset.cases_per_criterion
            return (
                "Frame phase. Call brief() first. Turn the user's concepts into an evaluation "
                "contract with propose_contract: a few criteria, each checked deterministically "
                "where the output allows it, by a judge rubric only where it does not. Then "
                f"write about {per} realistic working cases per criterion with add_cases, "
                "giving each the expected value its deterministic checks need. Never invent "
                "facts the concepts, seeds and harness do not give you (prices, rates, ids): "
                "a check that needs a value you cannot know belongs in a judge rubric or "
                "nowhere. If a concept is ambiguous, ask() one short question and keep going "
                "with your best reading. End with a one-line summary."
            )
        if self.phase == "hypothesize":
            return (
                f"Round {self.state['round']}, hypothesize phase. Call brief() first. Register "
                f"falsifiable hypotheses aimed at targets from the signal map, then design at "
                f"most {n} experiment(s) with concrete changes on the allowed levers. Prefer "
                "counted mechanisms when the power plan says a success-rate change is too small "
                "to see. Do not repeat a refuted idea. End with a one-line summary."
            )
        return (
            f"Round {self.state['round']}, interpret phase. Call brief() to read this round's "
            "verdicts (the engine measured them; you cannot change them), then call "
            "interpret() exactly once with your findings, the next focus, and continue, "
            "pivot or stop."
        )

    # -- reads -------------------------------------------------------------- #
    def _brief(self) -> dict[str, Any]:
        if self.phase == "frame":
            return self._frame_brief()
        engine, state = self.engine, self.state
        charter = engine.charter
        incumbent = state["incumbent"]
        spec = load_spec(engine.program.candidate_dir(incumbent))
        signal_map = engine._signal(state)
        plan = engine._plan(state)
        pools = engine.program.budget.pools()
        return {
            "goal": charter.goal,
            "phase": self.phase,
            "round": state["round"],
            "rounds_max": charter.budget.rounds,
            "levers": charter.levers,
            "guard": charter.guard.model_dump(),
            "experiments_per_round": charter.experiments_per_round,
            "budget": {name: {"left": round(p.left, 4), "size": round(p.size, 4)}
                       for name, p in pools.items()},
            "power_plan": plan.lines() if plan else [],
            "incumbent": {
                "candidate": incumbent,
                "spec": _spec_excerpt(spec),
            },
            "signal": {
                "verdict": signal_map.verdict,
                "headline": signal_map.headline,
                "signals": [s.model_dump(mode="json") for s in signal_map.signals[:8]],
                "failure_features": [f.model_dump(mode="json")
                                     for f in signal_map.failure_features[:8]],
                "mechanisms": [m.model_dump(mode="json") for m in signal_map.mechanisms[:8]],
                "loss": signal_map.loss.model_dump(mode="json"),
                "targets": signal_map.targets[:40],
            },
            "hypotheses": [{k: h.get(k) for k in ("id", "claim", "target", "expect", "by",
                                                  "status", "round")}
                           for h in state["hypotheses"]],
            "experiments": [{k: e.get(k) for k in ("id", "hypothesis", "verdict", "kept",
                                                   "stopped_early", "success",
                                                   "target_measure", "measured_effect")}
                            for e in state["experiments"]],
            "calibration": state["calibration"][-10:],
            "confirmed_not_in_incumbent": self._unmerged(),
            "findings": [f for h in state.get("handoffs", []) for f in h.get("findings", [])],
            # Counts say where; examples say what. Without them a director
            # theorizes from feature names instead of reading what failed.
            "evidence": self._evidence(signal_map),
            "earlier_programs": self._earlier_programs(),
            **self._contract_brief(),
        }

    def _unmerged(self) -> list[dict[str, Any]]:
        """Winners whose changes are not in the incumbent: re-express them on it."""
        state = self.state
        chain, current = set(), state["incumbent"]
        while current:
            chain.add(current)
            current = state["candidates"][current]["parent"]
        stacked = {e.get("stacked_from") for e in state["experiments"] if e.get("kept")}
        rows = []
        for experiment in state["experiments"]:
            if experiment["verdict"] not in ("confirmed", "improved") or experiment.get("kept"):
                continue
            if experiment["candidate"] in chain or experiment["id"] in stacked:
                continue
            claim = next((h["claim"] for h in state["hypotheses"]
                          if h["id"] == experiment["hypothesis"]), "")
            rows.append({"experiment": experiment["id"], "claim": claim,
                         "changes": experiment["changes"],
                         "note": "confirmed against an older incumbent but NOT in the current "
                                 "one; register it again and re-apply it to the current spec"})
        return rows

    def _earlier_programs(self) -> list[dict[str, Any]]:
        """What finished programs on this harness learned: kept, ruled out, and why they stopped."""
        from .program import list_programs, program_root

        engine = self.engine
        rows = []
        for row in list_programs(engine.program.base):
            if row["name"] == engine.program.name or row["status"] != "done":
                continue
            state = json.loads(
                (program_root(engine.program.base, row["name"]) / "state.json").read_text())
            claims = {h["id"]: h for h in state["hypotheses"]}
            rows.append({
                "program": row["name"],
                "stopped": state.get("stop_reason"),
                "kept": [claims[e["hypothesis"]]["claim"] for e in state["experiments"]
                         if e.get("kept") and e["hypothesis"] in claims],
                "ruled_out": [{"claim": h["claim"], "verdict": h.get("status")}
                              for h in state["hypotheses"]
                              if h.get("status") in ("refuted", "regressed", "futile")],
                "findings": [finding for handoff in state.get("handoffs", [])
                             for finding in handoff.get("findings", [])][:8],
            })
        return rows[-5:]

    def _contract_brief(self) -> dict[str, Any]:
        engine = self.engine
        if not engine.charter.concepts_mode:
            return {}
        contract = engine.contract(self.state)
        return {
            "contract": {
                "criteria": [c.model_dump(mode="json") for c in contract.criteria]
                if contract else [],
                "goal_thresholds": contract.goal_thresholds if contract else {},
                "measured": self.state.get("trust", []),
                "note": "success means passing every measured criterion; an unmeasured "
                        "judged criterion counts for nothing until the user's labels back it",
            },
            "answers": [q.model_dump(mode="json", include={"text", "answer"})
                        for q in engine.questions.all() if q.status == "answered"
                        and q.kind in ("disambiguate", "confirm")],
        }

    def _runs(self, status: str = "", feature: str = "", limit: int = 10) -> dict[str, Any]:
        engine = self.engine
        with Hive() as hive:
            runs = engine._working_runs(hive, self.state["incumbent"], self.state)
            chosen = [r for r in runs
                      if (not status or r["status"] == status)
                      and (not feature or feature in r["features"])][:max(1, min(limit, 25))]
            detail = []
            for run in chosen:
                row = hive.get_run(run["run_id"]) or {}
                detail.append({"run_id": run["run_id"], "status": run["status"],
                               "task": (row.get("task") or "")[:600],
                               "output": (row.get("output") or "")[:600],
                               "reason": (row.get("reason") or "")[:300],
                               "features": sorted(run["features"])[:20]})
        return {"runs": detail, "total_matching": len(chosen)}

    def _evidence(self, signal_map, per_signal: int = 3, general: int = 4) -> list[dict[str, Any]]:
        """A few failing working runs behind each top risk signal, and some others."""
        engine = self.engine
        with Hive() as hive:
            working = engine._working_runs(hive, self.state["incumbent"], self.state)
            failing = [run for run in working if run["failed"]]
            groups: list[tuple[str, list[dict]]] = []
            risks = [s.feature for s in signal_map.signals if s.direction == "risk"][:3]
            used: set[str] = set()
            for feature in risks:
                runs = [r for r in failing if feature in r["features"]][:per_signal]
                used.update(r["run_id"] for r in runs)
                groups.append((feature, runs))
            others = [r for r in failing if r["run_id"] not in used][:general]
            if others:
                groups.append(("other failing runs", others))
            return [{"behind": label, "runs": [_run_example(hive, run) for run in runs]}
                    for label, runs in groups if runs]

    def _excerpt(self, run_id: str, max_events: int = 40) -> dict[str, Any]:
        engine = self.engine
        with Hive() as hive:
            row = hive.get_run(run_id)
            allowed = {r["run_id"] for c in self.state["candidates"]
                       for r in engine._working_runs(hive, c, self.state)}
        if row is None or run_id not in allowed:
            raise ValueError(f"run {run_id} is not a working-split run of this program")
        events = []
        for line in Path(row["trace_path"]).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            etype = event.get("type")
            payload = event.get("payload") or {}
            if etype == "tool_call":
                events.append({"tool_call": payload.get("name"),
                               "input": json.dumps(payload.get("input"))[:300]})
            elif etype == "tool_result":
                events.append({"tool_result": payload.get("name"),
                               "is_error": payload.get("is_error"),
                               "content": str(payload.get("content"))[:400]})
            elif etype == "verification_result":
                events.append({"verification": payload.get("verifier"),
                               "passed": payload.get("passed"),
                               "feedback": str(payload.get("feedback"))[:300]})
            elif etype == "model_response" and payload.get("text"):
                events.append({"model_text": str(payload.get("text"))[:400]})
        return {"run_id": run_id, "status": row.get("status"),
                "events": events[:max(1, min(max_events, 80))]}

    def _diff(self, candidate: str) -> dict[str, Any]:
        info = self.state["candidates"].get(candidate)
        if info is None:
            raise ValueError(f"no candidate {candidate}")
        return {"candidate": candidate, "parent": info["parent"], "changes": info["changes"]}

    # -- acts --------------------------------------------------------------- #
    def _register_hypothesis(self, claim: str, levers: list[str], target: str, expect: str,
                             falsifier: str, prior: float = 0.5,
                             by: float | None = None) -> dict[str, Any]:
        charter = self.engine.charter
        if expect not in ("increase", "decrease"):
            raise ValueError("expect must be increase or decrease")
        outside = [lever for lever in levers
                   if not _covered(lever, charter.levers)]
        if outside:
            raise ValueError(f"levers outside the charter: {', '.join(outside)} "
                             f"(allowed: {', '.join(charter.levers)})")
        signal_map = self.engine._signal(self.state)
        if not (signal_map.knows_target(target) or target == "success_rate"):
            raise ValueError(f"target '{target}' is not in the signal map; choose one of: "
                             + ", ".join(signal_map.targets[:20]))
        fingerprint = _fingerprint(levers, target, claim)
        for h in self.state["hypotheses"]:
            settled = h.get("status") in ("refuted", "regressed", "futile")
            if h["fingerprint"] == fingerprint and settled:
                raise ValueError(f"this idea was already {h['status']} as {h['id']}; cite new "
                                 "evidence in a different claim if you believe it deserves "
                                 "another test")
        hypothesis = {
            "id": f"h{len(self.state['hypotheses']) + 1}", "round": self.state["round"],
            "claim": claim.strip()[:600], "levers": levers, "target": target,
            "expect": expect, "by": by, "prior": max(0.0, min(1.0, prior)),
            "falsifier": falsifier.strip()[:400], "fingerprint": fingerprint,
            "status": "registered",
        }
        self.state["hypotheses"].append(hypothesis)
        self.engine.program.ledger.append("hypothesis_registered", **{
            k: hypothesis[k] for k in ("id", "claim", "levers", "target", "expect", "by",
                                       "prior", "falsifier")})
        return {"registered": hypothesis["id"]}

    def _design_experiment(self, hypothesis_id: str,
                           changes: list[dict[str, Any]]) -> dict[str, Any]:
        engine, state = self.engine, self.state
        hypothesis = next((h for h in state["hypotheses"] if h["id"] == hypothesis_id), None)
        if hypothesis is None:
            raise ValueError(f"no hypothesis {hypothesis_id}; register it first")
        this_round = [e for e in state["pending_experiments"] if e["round"] == state["round"]]
        if len(this_round) >= engine.charter.experiments_per_round:
            raise ValueError(f"this round already has {len(this_round)} experiment(s), the "
                             "charter's maximum")
        if not changes:
            raise ValueError("an experiment needs at least one change")
        changes = [_normalize_change(change, engine.charter.levers) for change in changes]
        levers = engine.charter.levers
        for change in changes:
            path = str(change.get("path", ""))
            if not _covered(path, levers):
                raise ValueError(f"change path '{path}' is outside the charter's levers")
        plan = engine._plan(state)
        if plan is not None and plan.cost_mean > 0 and \
                not engine.program.budget.can_afford("experiments", plan.cost_mean):
            raise ValueError("the experiments pool cannot pay for another experiment")
        base = state["incumbent"]
        experiment, diff = engine.make_experiment(state, base, hypothesis, changes)
        hypothesis["status"] = "designed"
        return {"experiment": experiment["id"], "candidate": experiment["candidate"],
                "diff": diff}

    def _move_budget(self, source: str, target: str, usd: float) -> dict[str, Any]:
        refusal = self.engine.program.budget.move(source, target, float(usd))
        if refusal:
            raise ValueError(refusal)
        return {"moved": usd, "from": source, "to": target}

    def _interpret(self, findings: list[str], next_focus: list[str], decision: str,
                   stop_reason: str | None = None) -> dict[str, Any]:
        if self.handoff is not None:
            raise ValueError("interpret() was already called this round")
        if decision not in ("continue", "pivot", "stop"):
            raise ValueError("decision must be continue, pivot or stop")
        self.handoff = {"findings": [f[:400] for f in findings][:8],
                        "next_focus": [f[:300] for f in next_focus][:3],
                        "decision": decision, "stop_reason": (stop_reason or "")[:400] or None}
        return {"recorded": decision}

    def _note(self, text: str) -> dict[str, Any]:
        self.engine.program.ledger.append("note", round=self.state["round"], text=text[:1000])
        return {"noted": True}


_CHANGE_SHAPE = ('each change is {"path": "<dotted spec path>", "value": <new value>}, e.g. '
                 '{"path": "system_prompt", "value": "<the whole new prompt>"} or '
                 '{"path": "loop.max_turns", "value": 8}')


def _normalize_change(change: Any, levers: list[str]) -> dict[str, Any]:
    """Accept the shapes models reach for; refuse the rest with the right one."""
    if not isinstance(change, dict):
        raise ValueError(f"a change must be an object; {_CHANGE_SHAPE}")
    path = change.get("path", change.get("lever"))
    if isinstance(path, list):
        path = ".".join(str(part) for part in path)
    if path is None:
        named = [key for key in change if _covered(str(key), levers)]
        if len(named) == 1:
            return {"path": named[0], "value": change[named[0]]}
        raise ValueError(f"a change needs a path; {_CHANGE_SHAPE}")
    if "value" not in change:
        raise ValueError(f"change '{path}' has no value; {_CHANGE_SHAPE}")
    normalized = {"path": str(path), "value": change["value"]}
    if change.get("rationale"):
        normalized["rationale"] = str(change["rationale"])
    return normalized


def _first_problem(trace_path: str | None) -> str:
    """The first tool error or verifier feedback in a run's journal."""
    if not trace_path:
        return ""
    try:
        lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") or {}
        if event.get("type") == "tool_result" and payload.get("is_error"):
            return f"{payload.get('name')} error: {str(payload.get('content'))[:240]}"
        if event.get("type") == "verification_result" and payload.get("passed") is False:
            return f"verifier {payload.get('verifier')}: {str(payload.get('feedback'))[:240]}"
    return ""


def _run_example(hive: Hive, run: dict[str, Any]) -> dict[str, Any]:
    row = hive.get_run(run["run_id"]) or {}
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "task": (row.get("task") or "")[:300],
        "output": (row.get("output") or "")[:300],
        "problem": _first_problem(row.get("trace_path")),
    }


_CONFLICT = object()


def _at(raw: dict[str, Any], path: str) -> Any:
    node: Any = raw
    for part in path.split("."):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _rebase(change: dict[str, Any], base: dict[str, Any], incumbent: dict[str, Any]) -> Any:
    """The change's value re-expressed on top of the incumbent, or _CONFLICT."""
    path, value = change["path"], change["value"]
    if path.endswith(".+"):
        return value  # an append applies to whatever is there
    before, now = _at(base, path), _at(incumbent, path)
    if before == now or value == now:
        return value
    if value == before:
        return now
    if isinstance(value, str) and isinstance(before, str) and isinstance(now, str):
        merged = _merge3(before, now, value)
        return _CONFLICT if merged is None else merged
    if isinstance(value, list) and isinstance(now, list):
        return _merge_entries(before or [], now, value)
    return _CONFLICT


def _merge_entries(base: list, ours: list, theirs: list) -> Any:
    """Lists of id'd entries (memory lessons), merged three-way per id.

    ``ours`` is the side the result is built on (the live harness, or the kept
    candidate), ``theirs`` the side being brought over. Per id: the same on
    both sides is kept; changed (or removed) on one side only takes that side;
    changed differently on both is a conflict.
    """
    def by_id(items: list) -> dict[str, Any] | None:
        if not all(isinstance(item, dict) and item.get("id") for item in items):
            return None
        return {item["id"]: item for item in items}

    base_ids, mine, yours = by_id(base), by_id(ours), by_id(theirs)
    if base_ids is None or mine is None or yours is None:
        return _CONFLICT
    order = [item["id"] for item in ours] + [item["id"] for item in theirs
                                              if item["id"] not in mine]
    merged = []
    for entry_id in order:
        was, our, their = base_ids.get(entry_id), mine.get(entry_id), yours.get(entry_id)
        if our == their:
            chosen = our
        elif our == was:
            chosen = their
        elif their == was:
            chosen = our
        else:
            return _CONFLICT  # both changed one lesson, differently
        if chosen is not None:
            merged.append(chosen)
    return merged


def _merge3(base: str, ours: str, theirs: str) -> str | None:
    """Three-way merge of two prompts that both started from ``base``.

    By lines first; where that conflicts, by paragraphs compared without their
    whitespace, so a paragraph both sides merely re-wrapped counts as unchanged
    and the sections each added still combine. ``None`` when both really
    rewrote the same text.
    """
    # A missing final newline would make appending to the last line an edit of it.
    base, ours, theirs = (text if text.endswith("\n") else text + "\n"
                          for text in (base, ours, theirs))
    merged = _merge_sequences(base.splitlines(keepends=True), ours.splitlines(keepends=True),
                              theirs.splitlines(keepends=True), key=lambda line: line)
    if merged is not None:
        return "".join(merged)

    def paragraphs(text: str) -> list[str]:
        return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]

    merged = _merge_sequences(paragraphs(base), paragraphs(ours), paragraphs(theirs),
                              key=lambda paragraph: " ".join(paragraph.split()))
    return None if merged is None else "\n\n".join(merged) + "\n"


def _merge_sequences(base: list[str], ours: list[str], theirs: list[str], *, key) -> list | None:
    from difflib import SequenceMatcher

    base_keys = [key(item) for item in base]

    def edits(other: list[str]) -> tuple[list[tuple[int, int, list[str]]], dict[int, str]]:
        matcher = SequenceMatcher(a=base_keys, b=[key(item) for item in other], autojunk=False)
        changed, same = [], {}
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                same.update({i1 + k: other[j1 + k] for k in range(i2 - i1)})
            else:
                changed.append((i1, i2, other[j1:j2]))
        return changed, same

    mine, kept_as = edits(ours)
    yours, _ = edits(theirs)
    yours = [edit for edit in yours if edit not in mine]  # the same edit made on both sides
    for a1, a2, _ in mine:
        for b1, b2, _ in yours:
            # Edits that replace overlapping base items conflict; two insertions
            # at one point do not — both are kept, ours first.
            if a1 < b2 and b1 < a2:
                return None
            # An insertion inside, or touching, a replaced range is a conflict
            # too (as in diff3): it was written against text the other side rewrote.
            if (a1 == a2 and b1 < b2 and b1 <= a1 <= b2) or \
                    (b1 == b2 and a1 < a2 and a1 <= b1 <= a2):
                return None
    out: list[str] = []
    cursor = 0
    ordered = sorted([(e, 0) for e in mine] + [(e, 1) for e in yours],
                     key=lambda item: (item[0][0], item[0][1], item[1]))
    for (start, end, replacement), _side in ordered:
        if start > cursor:
            out.extend(kept_as.get(i, base[i]) for i in range(cursor, start))
        out.extend(replacement)
        cursor = max(cursor, end)
    out.extend(kept_as.get(i, base[i]) for i in range(cursor, len(base)))
    return out


def _tool_name(ref) -> str:
    from hiveloom.spec.schema import BuiltinToolRef

    if isinstance(ref, BuiltinToolRef):
        return ref.builtin
    return str(getattr(ref, "code", "")).rsplit(":", 1)[-1]


def _refuse(reason: str) -> str:
    return json.dumps({"refused": reason})


def _fingerprint(levers: list[str], target: str, claim: str) -> str:
    import hashlib

    words = " ".join(sorted(set(claim.lower().split())))
    material = json.dumps([sorted(levers), target, words])
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _spec_excerpt(spec) -> dict[str, Any]:
    raw = spec_to_dict(spec)
    keep = ("system_prompt", "tools", "memory", "loop", "verify", "playbooks", "context")
    return {key: raw.get(key) for key in keep if raw.get(key) is not None}


def _short_diff(before, after) -> str:
    from difflib import unified_diff

    diff = "".join(unified_diff(dump_spec(before).splitlines(keepends=True),
                                dump_spec(after).splitlines(keepends=True),
                                fromfile="incumbent", tofile="candidate"))
    return diff[:3000]


def load_program_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
