"""Dataset and scorer for a planted-defect desk: cases from a JSON file."""

from __future__ import annotations

import json

from hiveloom import EvalCase, RunMetric, ScorerOutput
from hiveloom.catalog import ParamSpec


class Cases:
    def __init__(self, path):
        self._path = path

    def load(self):
        return [EvalCase.model_validate(item) for item in json.loads(self._path.read_text())]


def score(context):
    try:
        answer = json.loads(context.run_result.output)
    except (json.JSONDecodeError, TypeError):
        answer = {}
    price = answer.get("price") if isinstance(answer, dict) else None
    try:
        correct = price is not None and abs(float(price) - context.expected["price"]) <= 0.011
    except (TypeError, ValueError):
        correct = False
    return ScorerOutput(metrics=[RunMetric(
        run_id=context.run_result.run_id, name="quote_correct", value=1.0 if correct else 0.0,
        direction="maximize", unit="ratio", source="research_bakeoff_v1", scope="case",
    )])


def hiveloom_extension(hive):
    hive.register_dataset(
        "desk_cases",
        lambda params, context: Cases(context.base / params.get("file", "data/cases.json")),
        description="Parcel quote requests from a JSON file (params.file).",
        params=[ParamSpec(name="file", type="str", default="data/cases.json",
                          description="Cases file, relative to the harness folder.")],
    )
    hive.register_scorer(
        "desk_quote", lambda _params, _context: score,
        description="1 when the quoted price is what billing will invoice.",
    )
