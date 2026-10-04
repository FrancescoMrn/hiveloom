"""The program's own eval: its cases, the contract's scorer, and run outcomes.

In concepts mode a program measures against the contract the user approved.
Each candidate carries the same eval document under ``.research/``, so every
candidate's runs pair case by case exactly as with a given eval. Its scorer
records one metric per criterion (``criterion.<id>``, 1 or 0) — deterministic
checks directly, judged ones from the ensemble's unanimous verdict — and
records nothing where a criterion does not apply or the judges split.

Whether a run *passed* is decided later, by the engine, from those metrics and
the judges' trust at that moment: only deterministic and trusted criteria
count. The verdict is written as the run's outcome, which the signal map,
``assess`` and the pairing already treat as success or failure — so a
criterion losing its trust re-labels every run it decided, and nothing in the
engine needs a second notion of success.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from hiveloom.evals import EvalCase, ScorerOutput
from hiveloom.metrics import RunMetric

from .contract import Contract, applies, check_deterministic, load_contract
from .judges import JudgePanel

EVAL_DIR = ".research"
EVAL_FILE = f"{EVAL_DIR}/eval.yaml"
_SOURCE = "hiveloom-research"
_OUTCOME_SOURCE = "research-contract"

_panels: dict[str, JudgePanel] = {}
_panels_lock = threading.Lock()


def register_panel(root: Path, panel: JudgePanel) -> None:
    with _panels_lock:
        _panels[str(root.resolve())] = panel


def panel_for(root: Path, models: list[str], base: Path | None = None) -> JudgePanel:
    key = str(root.resolve())
    with _panels_lock:
        if key not in _panels:
            _panels[key] = JudgePanel(root, models, base=base)
        return _panels[key]


def metric_name(criterion_id: str) -> str:
    return f"criterion.{criterion_id}"


EXTENSION = '''"""Registered by hiveloom.research for this program's eval; not user code."""

from hiveloom.research.evaluation import register


def hiveloom_extension(hive):
    register(hive)
'''


def write_eval(candidate_dir: Path, program_root: Path, contract_version: int) -> Path:
    """The eval document every candidate carries; identical across candidates."""
    folder = candidate_dir / EVAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "research_eval.py").write_text(EXTENSION, encoding="utf-8")
    path = candidate_dir / EVAL_FILE
    path.write_text(
        "schema_version: 1\n"
        "harness: ..\n"
        "extensions:\n  - research_eval.py\n"
        "dataset:\n  loader: research_cases\n  params:\n"
        f"    file: {program_root.resolve() / 'eval_cases.jsonl'}\n"
        "scorers:\n  - name: research_contract\n    params:\n"
        f"      program: {program_root.resolve()}\n"
        f"      contract: {contract_version}\n"
        "repetitions: 1\nmodel_identity: warn\n",
        encoding="utf-8",
    )
    return path


class _Cases:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> list[EvalCase]:
        import json

        return [EvalCase.model_validate(json.loads(line))
                for line in self._path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _scorer(params: dict[str, Any], _context: Any):
    import yaml

    root = Path(str(params["program"]))
    contract = load_contract(root, int(params["contract"]))
    charter = yaml.safe_load((root / "research.yaml").read_text(encoding="utf-8"))
    panel = panel_for(root, list((charter.get("models") or {}).get("judges") or []))

    def score(context) -> ScorerOutput:
        return ScorerOutput(metrics=score_run(contract, panel, context.case_input,
                                              context.run_result.output or "",
                                              dict(context.expected or {}),
                                              context.run_result.run_id))

    return score


def score_run(contract: Contract, panel: JudgePanel, request: str, output: str,
              expected: dict[str, Any], run_id: str) -> list[RunMetric]:
    metrics = []
    for criterion in contract.criteria:
        if not applies(criterion, expected):
            continue
        if criterion.check.deterministic:
            passed: bool | None = check_deterministic(criterion, output, expected)
        elif panel.models:
            verdict = panel.verdict(panel.votes(criterion, request, output,
                                                expected.get(criterion.id)))
            passed = None if verdict is None else verdict == "pass"
        else:
            passed = None
        if passed is None:
            continue
        metrics.append(RunMetric(run_id=run_id, name=metric_name(criterion.id),
                                 value=1.0 if passed else 0.0, direction="maximize",
                                 unit="ratio", source=_SOURCE, scope="case"))
    return metrics


def register(hive) -> None:
    from hiveloom.catalog import ParamSpec

    hive.register_dataset(
        "research_cases",
        lambda params, _context: _Cases(Path(str(params["file"]))),
        description="A research program's working and sealed cases.",
        params=[ParamSpec(name="file", type="str", required=True)],
    )
    hive.register_scorer(
        "research_contract",
        _scorer,
        description="One metric per contract criterion: deterministic checks and judges.",
        params=[ParamSpec(name="program", type="str", required=True),
                ParamSpec(name="contract", type="int", required=True)],
    )


def label_outcomes(hive, harness_key: str, contract: Contract, measured: set[str]) -> int:
    """Record each program run's pass/fail against the measured criteria."""
    rows = hive._conn.execute(
        "SELECT run_id FROM runs WHERE harness_key=?", (harness_key,)
    ).fetchall()
    run_ids = [row["run_id"] for row in rows]
    values: dict[str, dict[str, float]] = {}
    for criterion in contract.criteria:
        for run_id, value in hive.metric_values(run_ids, metric_name(criterion.id)).items():
            values.setdefault(run_id, {})[criterion.id] = value
    labelled = 0
    for run_id in run_ids:
        counted = {cid: v for cid, v in values.get(run_id, {}).items() if cid in measured}
        if not counted:
            # Nothing measured decides this run any more (a judge lost its trust):
            # withdraw a verdict the contract gave it, leaving the run's own status.
            hive._conn.execute(
                "DELETE FROM run_outcomes WHERE run_id=? AND source=?", (run_id, _OUTCOME_SOURCE)
            )
            continue
        failed = [cid for cid, value in counted.items() if value < 0.5]
        hive.record_outcome(run_id, "failure" if failed else "success",
                            source=_OUTCOME_SOURCE,
                            detail="failed: " + ", ".join(failed) if failed else "")
        labelled += 1
    hive._conn.commit()
    return labelled
