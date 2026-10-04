"""Concepts mode: a program that builds its own evaluation, then keeps it honest.

When the user brings concepts (and perhaps a few examples) rather than an eval,
the program starts with three units before the M1 loop:

    frame ──► approve (the user) ──► examine ──► baseline ──► … as with a given eval

* **frame** — the director drafts an evaluation contract (criteria, each with a
  deterministic check or a judge rubric) and working cases for it, and may ask
  the user what a concept means.
* **approve** — a hard gate: the program waits until the user approves the
  contract, having seen sample cases with what each is expected to satisfy.
* **examine** — the examiner, a different model that never sees the working
  cases, writes sealed cases from the concepts and the approved contract. A
  working case that nearly copies one is dropped.

From then on every eval run is scored per criterion, and each run's outcome is
decided by the criteria that are *measured* — deterministic ones always, judged
ones only while the judges agree with the user's labels. The engine asks for
those labels itself (disagreements first), and audits the incumbent every
round. At the end, evidence strength accounts for criteria that were never
measured, and the report states how every criterion was measured and how far
the working and sealed results are apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from hiveloom.eval_runner import case_key_for
from hiveloom.logging.hive import Hive
from hiveloom.spec.loader import load_spec

from .contract import NEEDS_EXPECTED, Contract, applies, load_contract, save_contract
from .dataset import Case, CaseStore, drop_near_duplicates
from .evaluation import label_outcomes, panel_for, write_eval
from .questions import QuestionBox

MAX_FRAME_ATTEMPTS = 3
EXAMINER_SYSTEM = """\
You write held-out test cases for an AI system, from what its user cares about.
You are the examiner: your cases decide whether changes to the system really
helped, so write the cases a careless implementation of these concepts would
get wrong — boundaries, unusual but valid inputs, the tempting mistake. Keep
every case realistic: the kind of request the system will actually receive,
in the same format as the example requests.

Reply with a single JSON array and nothing else. Each item:
{"criterion": "<criterion id>", "input": "<the request, verbatim>",
 "expected": {"<criterion id>": <value>}}
Give "expected" only for criteria whose check needs a value.
"""


def read_concepts(base: Path, value: str) -> str:
    candidate = base / value
    if len(value) < 200 and "\n" not in value and candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return value


def read_seeds(base: Path, value: str | None) -> list[Case]:
    if not value:
        return []
    path = base / value
    if not path.is_file():
        raise ValueError(f"seeds file {value} not found in the harness folder")
    cases = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        cases.append(Case(id=f"seed-{index}", input=str(row["input"]),
                          expected=dict(row.get("expected") or {}),
                          criterion=row.get("criterion"), provenance="seed",
                          notes=str(row.get("notes") or "")))
    return cases


class ConceptsEngine:
    """Mixed into :class:`~hiveloom.research.engine.Engine`."""

    # -- plumbing ------------------------------------------------------------- #
    @property
    def cases(self) -> CaseStore:
        return CaseStore(self.program.root)

    @property
    def questions(self) -> QuestionBox:
        return QuestionBox(self.program.root, total=self.charter.human.questions,
                           batch=self.charter.human.batch)

    def contract(self, state: dict[str, Any] | None = None) -> Contract | None:
        state = state or self.program.load_state()
        version = state.get("contract_version")
        return load_contract(self.program.root, version) if version else None

    def panel(self):
        return panel_for(self.program.root, list(self.charter.models.judges),
                         base=self.program.base)

    def _strong(self, selector: str):
        if selector in self._models:
            return self._models[selector]
        from hiveloom.generate.llm import build_strong_model

        self._models[selector] = build_strong_model(selector, self.program.base)
        return self._models[selector]

    def trust_states(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        contract = self.contract(state)
        if contract is None:
            return []
        trust, panel, anchors = self.charter.trust, self.panel(), self.questions.anchors()
        states = []
        for criterion in contract.criteria:
            if criterion.check.deterministic:
                states.append({"criterion": criterion.id, "measured": True,
                               "how": f"deterministic ({criterion.check.kind})"})
                continue
            if not panel.models:
                states.append({"criterion": criterion.id, "measured": False,
                               "how": "judged, but the charter names no judges"})
                continue
            result = panel.trust(criterion, anchors, kappa=trust.kappa,
                                 agreement=trust.agreement, min_anchors=trust.min_anchors)
            states.append({**result.as_dict(), "measured": result.trusted,
                           "how": f"judge ({result.reason})"})
        return states

    def measured(self, state: dict[str, Any]) -> set[str]:
        return {row["criterion"] for row in self.trust_states(state) if row["measured"]}

    def relabel(self, state: dict[str, Any]) -> None:
        """Re-decide every program run's outcome from the criteria measured now."""
        contract = self.contract(state)
        if contract is None:
            return
        trust = self.trust_states(state)
        measured = {row["criterion"] for row in trust if row["measured"]}
        previous = {row["criterion"] for row in state.get("trust", []) if row.get("measured")}
        with Hive() as hive:
            label_outcomes(hive, self.program.key(), contract, measured)
        if measured != previous and state.get("trust"):
            self.program.ledger.append("trust_changed", gained=sorted(measured - previous),
                                       lost=sorted(previous - measured))
        state["trust"] = trust

    # -- frame ---------------------------------------------------------------- #
    def _unit_frame(self, state: dict[str, Any]) -> dict[str, Any]:
        from .engine import DirectorSession

        session = DirectorSession(self, state, "frame")
        self._run_director(session, state)
        state["frame_attempts"] = state.get("frame_attempts", 0) + 1
        missing = self._frame_missing(state)
        if not missing:
            state["unit"] = "approve"
            state["status"] = "awaiting_user"
            state["sample_cases"] = self._sample_cases(state)
            drafted = [c["id"] for c in state["draft_contract"]["criteria"]]
            self.program.ledger.append("contract_drafted", criteria=drafted,
                                       working_cases=len(self.cases.working()))
            return {"outcome": "contract drafted; waiting for approval", "awaiting": "contract"}
        if state["frame_attempts"] >= MAX_FRAME_ATTEMPTS:
            state["status"] = "blocked"
            state["blocked_reason"] = "the director could not frame an evaluation: " + missing
            self.program.ledger.append("blocked", reason=state["blocked_reason"])
            return {"outcome": state["blocked_reason"], "done": True}
        return {"outcome": f"framing incomplete ({missing}); the director will continue"}

    def _frame_missing(self, state: dict[str, Any]) -> str:
        draft = state.get("draft_contract")
        if not draft:
            return "no contract proposed"
        contract = Contract.model_validate(draft)
        working = self.cases.working()
        short = []
        for criterion in contract.criteria:
            covering = [case for case in working
                        if applies(criterion, case.expected)
                        and (criterion.check.kind not in NEEDS_EXPECTED
                             or criterion.id in case.expected)]
            wanted = min(self.charter.dataset.cases_per_criterion, 2)
            if len(covering) < wanted:
                short.append(f"{criterion.id} has {len(covering)} case(s)")
        return "; ".join(short)

    def _sample_cases(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        contract = Contract.model_validate(state["draft_contract"])
        working = self.cases.working()
        chosen: list[Case] = []
        for criterion in contract.criteria:
            match = next((c for c in working if c not in chosen and
                          (c.criterion == criterion.id or criterion.id in c.expected)), None)
            if match:
                chosen.append(match)
        for case in working:
            if len(chosen) >= self.charter.dataset.sample_cases:
                break
            if case not in chosen:
                chosen.append(case)
        return [{"id": c.id, "input": c.input, "expected": c.expected,
                 "criteria": [cr.id for cr in contract.criteria if applies(cr, c.expected)],
                 "provenance": c.provenance} for c in chosen]

    # -- approve (the user's gate) --------------------------------------------- #
    def _unit_approve(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"outcome": "waiting for the contract to be approved", "awaiting": "contract"}

    def approve_contract(self, edited: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.program.lock():
            return self._approve_contract(edited)

    def _approve_contract(self, edited: dict[str, Any] | None) -> dict[str, Any]:
        state = self.program.load_state()
        if state["unit"] != "approve":
            raise ValueError(f"there is no contract waiting for approval (unit {state['unit']})")
        raw = edited if edited is not None else state["draft_contract"]
        try:
            contract = Contract.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"the contract is not valid: {exc}") from exc
        if contract.judged() and not self.charter.models.judges:
            raise ValueError("the contract has judged criteria but the charter names no "
                             "models.judges")
        version = int(state.get("contract_version") or 0) + 1
        save_contract(self.program.root, version, contract)
        state["contract_version"] = version
        state["unit"] = "examine"
        state["status"] = "active"
        self.program.ledger.append("contract_approved", version=version,
                                   edited=edited is not None,
                                   criteria=[c.id for c in contract.criteria])
        self.program.save_state(state)
        return {"approved": version}

    def answer(self, question_id: str, answer: str) -> dict[str, Any]:
        question = self.questions.answer(question_id, answer)
        self.program.ledger.append("answered", question=question.id,
                                   question_kind=question.kind, criterion=question.criterion,
                                   answer=question.answer)
        return question.model_dump(mode="json")

    # -- examine -------------------------------------------------------------- #
    def _unit_examine(self, state: dict[str, Any]) -> dict[str, Any]:
        contract = self.contract(state)
        concepts = (self.program.root / "concepts.md").read_text(encoding="utf-8")
        spec = load_spec(self.program.candidate_dir("c0"))
        seeds = [case for case in self.cases.working() if case.provenance == "seed"][:5]
        per = self.charter.dataset.sealed_per_criterion
        user = (
            f"<concepts>\n{concepts}\n</concepts>\n"
            f"<system>\n{spec.description}\n</system>\n"
            "<criteria>\n"
            + json.dumps([c.model_dump(mode="json") for c in contract.criteria], indent=1)
            + "\n</criteria>\n"
            + (f"<example_requests>\n{json.dumps([s.input for s in seeds], indent=1)}\n"
               "</example_requests>\n" if seeds else "")
            + f"Write {per} cases for each criterion ({per * len(contract.criteria)} in all)."
        )
        examiner = self._strong(self.charter.models.examiner)
        before = float(getattr(examiner, "spent_usd", 0.0) or 0.0)
        text = examiner.generate(system=EXAMINER_SYSTEM, user=user, max_tokens=6000)
        spent = float(getattr(examiner, "spent_usd", 0.0) or 0.0) - before
        if spent:
            self.program.budget.debit("data", spent, role="examiner")
        sealed = self._parse_sealed(text, contract)
        if not sealed:
            state["status"] = "blocked"
            state["blocked_reason"] = "the examiner wrote no usable sealed cases"
            self.program.ledger.append("blocked", reason=state["blocked_reason"])
            return {"outcome": state["blocked_reason"], "done": True}
        working, dropped = drop_near_duplicates(self.cases.working(), sealed)
        self.cases.set_sealed(sealed)
        self.cases.set_working(working)
        # Only counts reach the ledger: the sealed cases' content never enters a
        # record the director can read.
        self.program.ledger.append("sealed_written", cases=len(sealed),
                                   working_dropped=dropped)
        self.cases.write_eval_cases(self.program.root / "eval_cases.jsonl")
        write_eval(self.program.candidate_dir("c0"), self.program.root, state["contract_version"])
        state["candidates"]["c0"]["version"] = self.program.version_of("c0")
        state["split"] = {"working": [c.id for c in working], "holdout": [c.id for c in sealed]}
        state["unit"] = "baseline"
        return {"outcome": f"{len(sealed)} sealed case(s) written; "
                           f"{dropped} working case(s) dropped as near-copies"}

    def _parse_sealed(self, text: str, contract: Contract) -> list[Case]:
        match = re.search(r"\[.*\]", text or "", re.DOTALL)
        try:
            rows = json.loads(match.group(0)) if match else []
        except json.JSONDecodeError:
            rows = []
        ids = {c.id for c in contract.criteria}
        sealed: list[Case] = []
        seen: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not str(row.get("input", "")).strip():
                continue
            expected = {k: v for k, v in dict(row.get("expected") or {}).items() if k in ids}
            criterion = row.get("criterion") if row.get("criterion") in ids else None
            if criterion:
                wanted = contract.get(criterion)
                if wanted.check.kind in NEEDS_EXPECTED and criterion not in expected:
                    continue
            key = str(row["input"]).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            sealed.append(Case(id=f"sealed-{len(sealed) + 1}", input=str(row["input"]),
                               expected=expected, criterion=criterion, provenance="examiner"))
        return sealed

    # -- labels and audits ---------------------------------------------------- #
    def _case_by_key(self) -> dict[str, Case]:
        return {case_key_for(case.id): case for case in self.cases.working()}

    def ask_labels(self, state: dict[str, Any], kind: str = "label") -> int:
        """Ask the user to label outputs where a judged criterion needs trust (or an audit)."""
        contract = self.contract(state)
        if contract is None or not contract.judged() or not self.panel().models:
            return 0
        box, panel, asked = self.questions, self.panel(), 0
        trust = {row["criterion"]: row for row in state.get("trust", [])}
        by_key = self._case_by_key()
        with Hive() as hive:
            runs = self._working_runs(hive, state["incumbent"], state)
            keys = self._run_case_keys(hive, state["candidates"][state["incumbent"]]["version"])
            rows = {run["run_id"]: hive.get_run(run["run_id"]) or {} for run in runs}
        labelled = {(a["criterion"], a["output"]) for a in box.anchors()}
        for criterion in contract.judged():
            if box.room() <= 0:
                break
            is_trusted = bool(trust.get(criterion.id, {}).get("measured"))
            if kind == "label" and is_trusted:
                continue
            if kind == "audit" and not is_trusted:
                continue
            candidates = []
            for run in runs:
                case = by_key.get(keys.get(run["run_id"], ""))
                row = rows[run["run_id"]]
                output = row.get("output") or ""
                if case is None or not applies(criterion, case.expected) or \
                        (criterion.id, output) in labelled:
                    continue
                votes = panel.votes(criterion, case.input, output,
                                    case.expected.get(criterion.id))
                split = panel.verdict(votes) is None
                candidates.append((split, panel.verdict(votes), run["run_id"], case, output, votes))
            # Disagreements first; then alternate verdicts so labels cover both.
            candidates.sort(key=lambda item: (not item[0], item[1] or ""))
            quota = 1 if kind == "audit" else max(1, box.room() // max(1, len(contract.judged())))
            for _split, _verdict, run_id, case, output, votes in candidates[:quota]:
                if box.room() <= 0:
                    break
                try:
                    box.ask(kind, f"Does this answer meet: “{criterion.says}”?",
                            criterion=criterion.id, options=["pass", "fail"], run_id=run_id,
                            case_id=case.id, request=case.input, output=output,
                            reference=case.expected.get(criterion.id), judges=votes)
                except ValueError:
                    continue
                asked += 1
                labelled.add((criterion.id, output))
        if asked:
            self.program.ledger.append("questions_asked", question_kind=kind, count=asked)
        return asked

    # -- goal, confirmation, report ------------------------------------------- #
    def contract_goal(self, state: dict[str, Any]) -> str | None:
        contract = self.contract(state)
        if contract is None or not contract.goal_thresholds:
            return None
        from .evaluation import metric_name

        measured = self.measured(state)
        reached = []
        with Hive() as hive:
            runs = [r["run_id"] for r in self._working_runs(hive, state["incumbent"], state)]
            for criterion_id, threshold in contract.goal_thresholds.items():
                if criterion_id not in measured:
                    return None
                values = hive.metric_values(runs, metric_name(criterion_id))
                rate = (sum(values.values()) / len(values)) if values else 0.0
                if rate < threshold:
                    return None
                reached.append(f"{criterion_id} {rate:.2f} ≥ {threshold}")
        return "contract goal reached on the working cases: " + ", ".join(reached)

    def adjust_confirmation(self, state: dict[str, Any], confirmation: dict[str, Any]) -> dict:
        """Evidence strength that accounts for unmeasured criteria, and the proxy gap."""
        trust = self.trust_states(state)
        unmeasured = [row["criterion"] for row in trust if not row["measured"]]
        if confirmation.get("strength") == "confirmed" and unmeasured:
            confirmation["strength"] = "supported"
            confirmation["downgraded"] = ("criteria never measured: " + ", ".join(unmeasured))
        with Hive() as hive:
            working = self._working_runs(hive, state["incumbent"], state)
            sealed_keys = {case_key_for(cid) for cid in state["split"]["holdout"]}
            keyed = self._run_case_keys(hive, state["candidates"][state["incumbent"]]["version"])
            sealed = [r for r in hive.feature_population(
                self.program.key(), version=state["candidates"][state["incumbent"]]["version"])
                if keyed.get(r["run_id"]) in sealed_keys]
        if working and sealed:
            w = sum(not r["failed"] for r in working) / len(working)
            s = sum(not r["failed"] for r in sealed) / len(sealed)
            confirmation["proxy_gap"] = {"working": round(w, 3), "sealed": round(s, 3),
                                         "gap": round(w - s, 3), "flagged": w - s > 0.2}
        return confirmation

    def concepts_report(self, state: dict[str, Any]) -> list[str]:
        contract = self.contract(state)
        if contract is None:
            return []
        lines = ["", "## Evaluation contract", "",
                 f"Contract v{state['contract_version']}, approved by the user.", "",
                 "| criterion | says | measured by |", "|---|---|---|"]
        how = {row["criterion"]: row for row in self.trust_states(state)}
        for criterion in contract.criteria:
            row = how.get(criterion.id, {})
            label = row.get("how", "")
            if not row.get("measured"):
                label = f"**unmeasured** — {label}"
            lines.append(f"| {criterion.id} | {criterion.says} | {label} |")
        box = self.questions
        answered = [q for q in box.all() if q.status == "answered"]
        lines += ["", f"Questions: {len(answered)} answered of {len(box.all())} asked "
                      f"(budget {self.charter.human.questions}); {len(box.anchors())} labels."]
        gap = (state.get("confirmation") or {}).get("proxy_gap")
        if gap:
            lines.append(f"Working vs sealed success of the final version: {gap['working']:.0%} "
                         f"vs {gap['sealed']:.0%}"
                         + (" — **a large gap: possible overfitting to the working cases**"
                            if gap["flagged"] else "."))
        return lines


class FrameTools:
    """The director's tools for building the evaluation; mixed into DirectorSession."""

    def _frame_brief(self) -> dict[str, Any]:
        engine, state = self.engine, self.state
        spec = load_spec(engine.program.candidate_dir("c0"))
        store = engine.cases
        working = store.working()
        draft = state.get("draft_contract")
        return {
            "phase": "frame",
            "goal": engine.charter.goal,
            "concepts": (engine.program.root / "concepts.md").read_text(encoding="utf-8"),
            "harness": {"name": spec.name, "description": spec.description,
                        "system_prompt": spec.system_prompt[:3000],
                        "tools": [getattr(t, "description", None) or getattr(t, "builtin", None)
                                  or getattr(t, "code", "") for t in spec.tools]},
            "check_kinds": {
                "contains / not_contains / exact": "compare the output with the case's "
                "expected value for this criterion",
                "json_field": "the output is JSON and field equals the expected value",
                "json_present": "the output is JSON and field is present and non-empty",
                "regex": "the output matches pattern (no expected value needed)",
                "judge": "models score the output against rubric; counts only once they "
                         "agree with the user's labels",
            },
            "judges_available": bool(engine.charter.models.judges),
            "dataset": engine.charter.dataset.model_dump(),
            "draft_contract": draft,
            "cases": {"count": len(working),
                      "examples": [c.model_dump(mode="json") for c in working[:12]]},
            "answers": [q.model_dump(mode="json", include={"text", "answer"})
                        for q in engine.questions.all() if q.status == "answered"
                        and q.kind in ("disambiguate", "confirm")],
            "open_questions": [q.text for q in engine.questions.open()],
            "missing": engine._frame_missing(state) if draft else "no contract proposed",
        }

    def _propose_contract(self, criteria: list[dict[str, Any]],
                          goal_thresholds: dict[str, float] | None = None) -> dict[str, Any]:
        try:
            contract = Contract.model_validate({"criteria": criteria,
                                                "goal_thresholds": goal_thresholds or {}})
        except ValidationError as exc:
            raise ValueError(f"the contract is not valid: {exc.errors()[0]['msg']} "
                             f"at {'.'.join(str(p) for p in exc.errors()[0]['loc'])}") from exc
        if contract.judged() and not self.engine.charter.models.judges:
            raise ValueError("no judges are configured: use deterministic checks only")
        self.state["draft_contract"] = contract.model_dump(mode="json")
        self.engine.program.ledger.append("contract_proposed",
                                          criteria=[c.id for c in contract.criteria])
        return {"drafted": [c.id for c in contract.criteria],
                "next": "add cases; each criterion needs cases it applies to"}

    def _add_cases(self, cases: list[dict[str, Any]]) -> dict[str, Any]:
        draft = self.state.get("draft_contract")
        if not draft:
            raise ValueError("propose a contract first: cases are written for its criteria")
        contract = Contract.model_validate(draft)
        ids = {c.id for c in contract.criteria}
        store = self.engine.cases
        built, refused = [], []
        for index, row in enumerate(cases):
            if not isinstance(row, dict) or not str(row.get("input", "")).strip():
                refused.append(f"case {index}: no input")
                continue
            expected = dict(row.get("expected") or {})
            unknown = sorted(set(expected) - ids)
            if unknown:
                refused.append(f"case {index}: unknown criteria in expected: {unknown}")
                continue
            criterion = row.get("criterion")
            if criterion is not None and criterion not in ids:
                refused.append(f"case {index}: unknown criterion {criterion!r}")
                continue
            if criterion and contract.get(criterion).check.kind in NEEDS_EXPECTED \
                    and criterion not in expected:
                refused.append(f"case {index}: {criterion} needs an expected value")
                continue
            built.append(Case(id=f"draft-{index}", input=str(row["input"]),
                              expected=expected, criterion=criterion, provenance="director"))
        added, rejected = store.add_working(built, limit=self.engine.charter.dataset.max_cases,
                                            prefix="work")
        refused += rejected
        if added:
            self.engine.program.ledger.append("cases_added", count=len(added))
        return {"added": len(added), "total": len(store.working()), "refused": refused[:10],
                "missing": self.engine._frame_missing(self.state)}

    def _ask(self, text: str, kind: str = "disambiguate", options: list[str] | None = None,
             criterion: str | None = None) -> dict[str, Any]:
        if kind not in ("disambiguate", "confirm"):
            raise ValueError("the director asks 'disambiguate' or 'confirm' questions; the "
                             "engine asks for labels itself")
        question = self.engine.questions.ask(kind, text, options=list(options or []),
                                             criterion=criterion, asked_by="director")
        self.engine.program.ledger.append("question_asked", question=question.id,
                                          question_kind=kind)
        return {"asked": question.id, "note": "answers arrive asynchronously; continue"}


def seed_program(root: Path, base: Path, concepts: str, seeds: str | None) -> int:
    """Write concepts.md and the seed cases into a new program folder."""
    (root / "concepts.md").write_text(read_concepts(base, concepts), encoding="utf-8")
    cases = read_seeds(base, seeds)
    if cases:
        CaseStore(root).set_working(cases)
    return len(cases)
