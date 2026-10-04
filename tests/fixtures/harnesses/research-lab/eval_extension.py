"""Dataset and scorer for the research-lab eval: twenty-four parcel quotes."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import EvalCase, RunMetric, ScorerOutput

_SOURCE = "research_lab_eval_v1"


class QuoteCases:
    def __init__(self, base: Path):
        self._path = base / "data" / "cases.json"

    def load(self):
        return [EvalCase.model_validate(item) for item in json.loads(self._path.read_text())]


def score_quote(context):
    try:
        answer = json.loads(context.run_result.output)
    except (json.JSONDecodeError, TypeError):
        answer = {}
    price = answer.get("price") if isinstance(answer, dict) else None
    correct = price is not None and abs(float(price) - context.expected["price"]) < 0.005
    return ScorerOutput(
        metrics=[
            RunMetric(
                run_id=context.run_result.run_id,
                name="quote_correct",
                value=1.0 if correct else 0.0,
                direction="maximize",
                unit="ratio",
                source=_SOURCE,
                scope="case",
            )
        ]
    )


def hiveloom_extension(hive):
    hive.register_dataset(
        "research_lab_cases",
        lambda _params, context: QuoteCases(context.base),
        description="Twenty-four parcel quotes: nine give the weight in grams, five are "
        "for contract customers.",
    )
    hive.register_scorer(
        "research_lab_quote",
        lambda _params, _context: score_quote,
        description="1 when the quoted price is what billing will invoice, else 0.",
    )
