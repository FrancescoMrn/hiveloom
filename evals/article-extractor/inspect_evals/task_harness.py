"""Harness arms: the sample runs through the full hiveloom pipeline."""

from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.dataset import FieldSpec, json_dataset

from inspect_evals._shared import DATASET_PATH
from inspect_evals.scorer import article_extractor_scorer
from inspect_evals.solver_harness import hiveloom_subprocess


def article_dataset():
    return json_dataset(
        str(DATASET_PATH),
        FieldSpec(input="url", id="id", metadata=["golden", "category", "fingerprint"]),
    )


@task
def article_extractor_harness(harness_dir: str = "harnesses/harness-haiku"):
    return Task(
        dataset=article_dataset(),
        solver=hiveloom_subprocess(harness_dir=harness_dir),
        scorer=article_extractor_scorer(),
        epochs=3,
        time_limit=360,
    )
