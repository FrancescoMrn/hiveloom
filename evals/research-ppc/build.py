"""Build the offline extraction pilot through validated construction APIs."""

import argparse
import shutil
from pathlib import Path

import yaml

from hiveloom import construct


def build(destination: Path, executor: str, director: str, budget: float = 1.0):
    source = Path(__file__).parent
    construct.init_harness(destination, name="ppc-articles",
                           task="Extract the article title and its complete quotation as JSON.")
    for name in ("pages.json", "checks.py", "evaluation.py"):
        shutil.copyfile(source / name, destination / name)
    provider, model = executor.split("/", 1)
    construct.set_value(destination, "model", {"provider": provider, "id": model})
    construct.set_value(destination, "system_prompt",
                        'Read the saved HTML page. Return only JSON with keys "title", "quote", '
                        'and "url". Extract the article title and the full article quotation; '
                        'ignore navigation, comments and related content.')
    construct.set_value(destination, "evolution.mutable", ["system_prompt", "loop.max_turns"])
    construct.add_validator(destination, code="checks.py:validate")
    (destination / "eval.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "harness": ".", "extensions": ["evaluation.py"],
        "dataset": {"loader": "ppc_pages", "include_expected_in_input": False},
        "scorers": ["ppc_extraction"], "repetitions": 1, "model_identity": "warn",
    }))
    for strategy in ("autonomous", "ppc_select", "ppc"):
        (destination / f"{strategy}.yaml").write_text(yaml.safe_dump({
            "goal": "Correct article JSON with complete quotations from saved pages",
            "strategy": strategy, "eval": "eval.yaml", "holdout": 0.3,
            "levers": ["system_prompt"], "experiments_per_round": 2,
            "budget": {"usd": budget, "rounds": 2},
            "models": {"director": director},
            "guard": {"success_rate": "no_regression", "cost_per_run_increase": 0.2},
        }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--executor", required=True, help="provider/model-id")
    parser.add_argument("--director", required=True, help="provider/model-id")
    parser.add_argument("--budget", type=float, default=1.0)
    args = parser.parse_args()
    build(args.destination, args.executor, args.director, args.budget)
