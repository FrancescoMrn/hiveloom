"""Synthetic dataset and ranked-retrieval scorers for the example harness."""

from __future__ import annotations

import json
import math
from pathlib import Path

from hiveloom import EvalCase, RunMetric, ScorerOutput

_SOURCE = "ranked_retrieval_eval_v1"


class SyntheticRetrievalCases:
    def __init__(self, base: Path):
        self._path = base / "data" / "eval_cases.json"

    def load(self):
        return [EvalCase.model_validate(item) for item in json.loads(self._path.read_text())]


def _selected_ids(output: str) -> list[str]:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    selected = payload.get("selected", []) if isinstance(payload, dict) else []
    return [
        str(item["record_id"])
        for item in selected[:3]
        if isinstance(item, dict) and item.get("record_id") is not None
    ]


def _dcg(relevances: list[int]) -> float:
    return sum((2**value - 1) / math.log2(rank + 2) for rank, value in enumerate(relevances))


def score_ranked_retrieval(context):
    selected = _selected_ids(context.run_result.output)
    relevance = context.expected["relevance"]
    relevant_ids = set(relevance)
    hits = len(relevant_ids & set(selected))
    recall = hits / len(relevant_ids) if relevant_ids else 1.0

    gains = [int(relevance.get(record_id, 0)) for record_id in selected]
    ideal = sorted((int(value) for value in relevance.values()), reverse=True)[:3]
    ideal_dcg = _dcg(ideal)
    ndcg = _dcg(gains) / ideal_dcg if ideal_dcg else 1.0

    eligible = set(context.expected["eligible_ids"])
    hallucinations = sum(record_id not in eligible for record_id in selected)
    hallucination_rate = hallucinations / len(selected) if selected else 0.0
    values = [
        ("recall_at_3", recall, "maximize"),
        ("ndcg_at_3", ndcg, "maximize"),
        ("hallucination_rate", hallucination_rate, "minimize"),
    ]
    return ScorerOutput(
        metrics=[
            RunMetric(
                run_id=context.run_result.run_id,
                name=name,
                value=value,
                direction=direction,
                unit="ratio",
                source=_SOURCE,
                scope="case",
            )
            for name, value, direction in values
        ]
    )


def hiveloom_extension(hive):
    hive.register_dataset(
        "synthetic_retrieval_cases",
        lambda _params, context: SyntheticRetrievalCases(context.base),
        description="Load three synthetic ranked-retrieval cases.",
    )
    hive.register_scorer(
        "ranked_retrieval_metrics",
        lambda _params, _context: score_ranked_retrieval,
        description="Measure Recall@3, nDCG@3, and hallucinated identifier rate.",
    )
