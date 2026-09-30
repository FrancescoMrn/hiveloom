"""M2 live check: do real judges earn trust from a user's labels within budget?

research-lab in concepts mode with real models for the director (framing), the
examiner and two judges; the executor stays the free scripted desk. A test
user approves an edited contract with two known criteria (so labels can be
given from ground truth) and answers every label question truthfully. The
question is the design's M2 exit criterion: does the judged criterion reach
κ ≥ 0.6 and agreement ≥ 0.85 within the question budget — and what did the
live director frame on its own?

    uv run python evals/research-bakeoff/scripts/concepts_live.py \\
        --director openrouter/openai/gpt-5-mini --examiner openrouter/google/gemma-4-31b-it \\
        --judge openrouter/openai/gpt-5-mini --judge openrouter/google/gemma-4-31b-it
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
LAB = REPO / "harnesses" / "research-lab"

CONTRACT = {
    "criteria": [
        {"id": "quoted", "says": "Every request gets a quoted price",
         "check": {"kind": "json_present", "field": "price"}},
        {"id": "one-json", "says": "The answer is one JSON object with zone, weight_kg and "
         "price, and nothing else",
         "check": {"kind": "judge", "rubric": "Pass only if the whole answer is a single JSON "
                   "object whose keys are exactly zone, weight_kg and price, with no other "
                   "keys and no text around it."}},
    ],
    "goal_thresholds": {"quoted": 0.9, "one-json": 0.9},
}


def truth(output: str) -> str:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return "fail"
    return "pass" if isinstance(data, dict) and set(data) == {"zone", "weight_kg",
                                                              "price"} else "fail"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--director", required=True)
    parser.add_argument("--examiner", required=True)
    parser.add_argument("--judge", action="append", required=True)
    parser.add_argument("--questions", type=int, default=12)
    parser.add_argument("--min-anchors", type=int, default=8)
    parser.add_argument("--out", type=Path, default=REPO / "evals" / "research-bakeoff" / "results")
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="concepts-live-"))
    os.environ["HIVELOOM_HOME"] = str(home)
    os.environ["HIVELOOM_TRUST"] = "always"
    from hiveloom.research.engine import Engine
    from hiveloom.research.program import Program, init_program

    lab = home / "research-lab"
    shutil.copytree(LAB, lab, ignore=shutil.ignore_patterns(".hiveloom"))
    charter = yaml.safe_load((lab / "research-concepts.yaml").read_text())
    charter["models"] = {"director": args.director, "examiner": args.examiner,
                         "judges": args.judge}
    charter["human"] = {"questions": args.questions, "batch": args.questions}
    charter["trust"] = {"min_anchors": args.min_anchors}
    (lab / "live.yaml").write_text(yaml.safe_dump(charter, sort_keys=False))

    started = time.monotonic()
    program = init_program(lab, "live", lab / "live.yaml")
    Engine(Program(lab, "live")).run()  # frame, until the approval gate
    state = program.load_state()
    framed = state.get("draft_contract")
    if state["unit"] != "approve":
        print(json.dumps({"stuck": state["unit"], "blocked": state.get("blocked_reason")}))
        return
    Engine(Program(lab, "live")).approve_contract(CONTRACT)
    Engine(Program(lab, "live")).run(until="unit")  # examine
    Engine(Program(lab, "live")).run(until="unit")  # baseline, asks for labels

    engine = Engine(Program(lab, "live"))
    answered = 0
    for question in engine.questions.open():
        if question.kind in ("label", "audit"):
            engine.answer(question.id, truth(question.output or ""))
            answered += 1
    engine.relabel(state := Program(lab, "live").load_state())
    trust = engine.trust_states(state)

    # Agreement of each judge with the truth on every judged output, not just anchors.
    panel = engine.panel()
    judged = [json.loads(line) for line in
              (program.root / "judgments.jsonl").read_text().splitlines() if line.strip()]
    report = {
        "director_framed": framed,
        "sealed_cases": len(engine.cases.sealed()),
        "working_cases": len(engine.cases.working()),
        "labels_answered": answered,
        "trust": trust,
        "judge_calls": len(judged),
        "judge_spend_usd": round(panel.spent_usd, 4),
        "budget": {k: round(v.spent, 4) for k, v in program.budget.pools().items()},
        "seconds": round(time.monotonic() - started, 1),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"concepts-live-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str)[:4000])
    print(f"\nwritten: {path}")


if __name__ == "__main__":
    main()
