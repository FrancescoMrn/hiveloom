"""Versioned saved-page dataset and separate format/content metrics."""

import importlib.util
import json
from pathlib import Path

from hiveloom import EvalCase, RunMetric, ScorerOutput


class Pages:
    def __init__(self, base):
        self.base = base

    def load(self):
        return [EvalCase.model_validate(row)
                for row in json.loads((self.base / "pages.json").read_text())]


def score(context):
    spec = importlib.util.spec_from_file_location(
        "ppc_checks", Path(__file__).with_name("checks.py"))
    checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    values = checks.measurements(context.run_result.output, context.expected)
    correct = all(values.values())
    values["correct"] = float(correct)
    values["false_success"] = float(context.run_result.status == "success" and not correct)
    return ScorerOutput(metrics=[
        RunMetric(run_id=context.run_result.run_id, name=name, value=value,
                  direction="minimize" if name == "false_success" else "maximize",
                  unit="ratio", source="ppc_pages_v1", scope="case")
        for name, value in values.items()
    ])


def hiveloom_extension(hive):
    hive.register_dataset("ppc_pages", lambda _params, context: Pages(context.base),
                          description="Synthetic saved HTML pages, version 1.")
    hive.register_scorer("ppc_extraction", lambda _params, _context: score,
                         description="JSON, title, quotation, source and false-success metrics.")
