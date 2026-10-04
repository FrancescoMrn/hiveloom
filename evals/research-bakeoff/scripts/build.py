"""Build the planted-defect harnesses for the research bake-off.

Each harness is the same shipping desk (``template/``) with two of the four
fixable defects planted in its request mix, plus the contract decoy no prompt
can reach. Built through the CLI only (init/add/set), never by editing
harness.yaml. Cases are generated deterministically: 40 for the eval the arms
improve against, and 40 fresh ones for the final test neither arm ever sees.

    uv run python evals/research-bakeoff/scripts/build.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TEMPLATE = HERE / "template"

#: name -> the two planted defects.
PLANTED = {
    "desk-unit-case": ("unit", "case"),
    "desk-retry-currency": ("retry", "currency"),
    "desk-unit-retry": ("unit", "retry"),
    "desk-case-currency": ("case", "currency"),
}
#: Per 40 cases: clean, each defect, the contract decoy.
MIX = {"clean": 10, "defect": 11, "contract": 8}

PROMPT = """\
You are the shipping desk. For each request, read the parcel's weight, zone
code and customer, call rate_quote, and answer with one JSON object:
{"zone": "<code>", "price": <number>, "currency": "<USD|EUR>"}. If the rate
service cannot quote the parcel, answer {"price": null, "error": "<why>"}.
"""

CHARTER = """\
goal: Every quote is what billing will invoice.
eval: eval.yaml
holdout: 0.25
levers:
  - system_prompt
  - loop.max_turns
  - memory.entries
budget:
  usd: {budget}
  rounds: {rounds}
stop:
  goal:
    success_rate: 0.95
  no_progress_rounds: 2
execution:
  tools:
    rate_quote: allow
models:
  director: {director}
experiments_per_round: 2
"""


def _case(rng: random.Random, kind: str, index: int) -> dict:
    zone = rng.choice(["Z1", "Z2", "Z3", "Z4"])
    kg = rng.choice([0.4, 0.75, 1.2, 2.5, 3.8, 5.0, 7.5, 12.0])
    customer = rng.choice(["C-100", "C-101", "C-102"])
    weight, zone_text, suffix = f"{kg:g} kg", zone, ""
    if kind == "unit":
        weight = f"{int(kg * 1000)} g"
    elif kind == "case":
        zone_text = zone.lower()
    elif kind == "retry":
        customer = rng.choice(["C-300", "C-301"])
    elif kind == "currency":
        suffix = " Quote in EUR."
    elif kind == "contract":
        customer = rng.choice(["C-200", "C-201"])
    request = (
        f"Quote shipping for a {weight} parcel to zone {zone_text} for customer "
        f"{customer}.{suffix}"
    )
    rates = json.loads((TEMPLATE / "data" / "rates.json").read_text())
    rate = rates["zones"][zone]
    price = round(rate["base"] + rate["per_kg"] * kg, 2)
    if kind == "contract":
        price = round(price * 0.88, 2)
    if kind == "currency":
        price = round(price * rates["eur_per_usd"], 2)
    return {"id": f"case-{index:02d}", "input": request,
            "expected": {"price": price, "kind": kind}}


def cases_for(defects: tuple[str, str], seed: int) -> list[dict]:
    rng = random.Random(seed)
    kinds = (["clean"] * MIX["clean"] + [defects[0]] * MIX["defect"]
             + [defects[1]] * MIX["defect"] + ["contract"] * MIX["contract"])
    rng.shuffle(kinds)
    return [_case(rng, kind, i) for i, kind in enumerate(kinds, 1)]


def _hl(*args: str, cwd: Path) -> None:
    subprocess.run(["hiveloom", *args, "--json"], cwd=cwd, check=True, capture_output=True)


def build(out: Path, *, director: str, budget: float, rounds: int,
          seed_offset: int = 0) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    built = []
    for index, (name, defects) in enumerate(PLANTED.items()):
        target = out / name
        if target.exists():
            shutil.rmtree(target)
        subprocess.run(
            ["hiveloom", "init", str(target), "--name", name, "--task",
             "Quote the shipping price for one parcel as JSON.", "--json"],
            check=True, capture_output=True,
        )
        for leftover in ("tools", "validators", "schemas", ".env.example"):
            path = target / leftover
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        shutil.copytree(TEMPLATE, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (target / "prompt.txt").write_text(PROMPT)
        _hl("set", "extensions", '["extensions/desk.py"]', cwd=target)
        _hl("set", "model", "desk/clerk", cwd=target)
        _hl("set", "system_prompt", "--file", "prompt.txt", cwd=target)
        (target / "prompt.txt").unlink()
        _hl("add", "tool", "--code", "tools/rate_quote.py:rate_quote", "--description",
            "The list price in USD by zone code, weight in kilograms, and customer.", cwd=target)
        _hl("add", "validator", "--code", "validators/billing_check.py:validate",
            "--description", "The quote must equal what billing will invoice.", cwd=target)
        _hl("set", "verify.on_fail.action", "abort", cwd=target)
        _hl("set", "loop.max_turns", "6", cwd=target)
        # The model decides what to do with a refused call; the loop's own
        # retry would otherwise hide the retry defect.
        _hl("set", "loop.on_tool_error", "surface_to_model", cwd=target)
        _hl("set", "memory.enabled", "true", cwd=target)
        _hl("set", "evolution.mutable", '["system_prompt", "loop.max_turns", "memory.entries"]',
            cwd=target)
        # The evolve arm's best configuration: incident excerpts in its prompt.
        _hl("set", "evolution.trace_excerpts.enabled", "true", cwd=target)
        (target / "data" / "cases.json").write_text(
            json.dumps(cases_for(defects, seed=1000 + seed_offset + index), indent=2))
        (target / "data" / "test_cases.json").write_text(
            json.dumps(cases_for(defects, seed=2000 + seed_offset + index), indent=2))
        for doc, file in (("eval.yaml", "data/cases.json"), ("test.yaml", "data/test_cases.json")):
            (target / doc).write_text(
                "schema_version: 1\nharness: .\nextensions:\n  - eval_extension.py\n"
                f"dataset:\n  loader: desk_cases\n  params:\n    file: {file}\n"
                "scorers:\n  - desk_quote\nrepetitions: 1\nmodel_identity: warn\n"
            )
        (target / "research.yaml").write_text(
            CHARTER.format(director=director, budget=budget, rounds=rounds))
        (target / "planted.json").write_text(json.dumps({"defects": list(defects)}))
        _hl("validate", cwd=target)
        built.append(target)
    return built


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "planted")
    parser.add_argument("--director", default="openrouter/openai/gpt-5-mini")
    parser.add_argument("--budget", type=float, default=1.0)
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args()
    for path in build(args.out, director=args.director, budget=args.budget, rounds=args.rounds):
        print(path)


if __name__ == "__main__":
    main()
