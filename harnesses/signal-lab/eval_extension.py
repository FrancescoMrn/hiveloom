"""Dataset and scorer for the signal-lab eval: twelve invoice lookups."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import EvalCase, RunMetric, ScorerOutput

_SOURCE = "signal_lab_eval_v1"


class InvoiceCases:
    def __init__(self, base: Path):
        self._path = base / "data" / "cases.json"

    def load(self):
        return [EvalCase.model_validate(item) for item in json.loads(self._path.read_text())]


def score_amount(context):
    try:
        answer = json.loads(context.run_result.output)
    except (json.JSONDecodeError, TypeError):
        answer = {}
    expected = context.expected
    correct = (
        isinstance(answer, dict)
        and answer.get("status") == "found"
        and answer.get("invoice_id") == expected["invoice_id"]
        and answer.get("amount") == expected["amount"]
    )
    return ScorerOutput(
        metrics=[
            RunMetric(
                run_id=context.run_result.run_id,
                name="amount_correct",
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
        "signal_lab_cases",
        lambda _params, context: InvoiceCases(context.base),
        description="Twelve invoice lookups; eight write the id in lowercase.",
    )
    hive.register_scorer(
        "signal_lab_amount",
        lambda _params, _context: score_amount,
        description="1 when the reported amount matches the ledger, else 0.",
    )
