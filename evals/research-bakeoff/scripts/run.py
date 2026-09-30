"""Run the research bake-off: a research program against `evolve --experiment`.

For every planted harness, each arm gets its own copy and its own Hive, the
same model, and the same rounds:

* **research** — `hiveloom research init/run` with the model as director, then
  `proposals apply` of the promotion it queued (if any);
* **evolve** — `hiveloom eval run` for a baseline, then
  `hiveloom evolve --experiment eval.yaml --yes --rounds N --model M`.

Both final harnesses then run the 40 fresh test cases neither arm saw. A
defect counts as fixed when its test cases pass at ≥ 80% (they start at 0%);
a kept change that fixed no defect is a false promotion; the contract decoy
must stay unfixed and the clean cases must not regress.

    uv run python evals/research-bakeoff/scripts/run.py \\
        --model openrouter/openai/gpt-5-mini [--arms research,evolve] [--only NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
from build import build  # noqa: E402

FIXED_AT = 0.8


def _hl(args: list[str], *, cwd: Path, env: dict[str, str], ok_codes=(0, 1)) -> dict:
    started = time.monotonic()
    proc = subprocess.run(["hiveloom", *args, "--json"], cwd=cwd, env=env,
                          capture_output=True, text=True)
    if proc.returncode not in ok_codes:
        raise RuntimeError(f"hiveloom {' '.join(args)} exited {proc.returncode}: "
                           f"{proc.stdout[-2000:]}{proc.stderr[-2000:]}")
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    payload["_seconds"] = round(time.monotonic() - started, 1)
    payload["_exit"] = proc.returncode
    return payload


def _by_kind(harness: Path, manifest: dict) -> dict[str, dict[str, int]]:
    """Pass counts per case kind, from an eval manifest on disk."""
    from hiveloom.eval_runner import case_key_for, load_eval_manifest

    kinds = {case_key_for(c["id"]): c["expected"]["kind"]
             for c in json.loads((harness / "data" / "test_cases.json").read_text())}
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "total": 0})
    for cell in load_eval_manifest(manifest["eval_run_id"]).cells:
        kind = kinds[cell.case_key]
        counts[kind]["total"] += 1
        counts[kind]["passed"] += int(cell.run_status == "success")
    return dict(counts)


def _test(harness: Path, env: dict[str, str]) -> dict[str, dict[str, int]]:
    manifest = _hl(["eval", "run", "test.yaml", "--approve"], cwd=harness, env=env)
    os.environ["HIVELOOM_HOME"] = env["HIVELOOM_HOME"]
    return _by_kind(harness, manifest)


def _rate(counts: dict[str, int]) -> float:
    return counts["passed"] / counts["total"] if counts["total"] else 0.0


def run_research(harness: Path, env: dict[str, str], model: str) -> dict:
    init = _hl(["research", "init", ".", "--name", "bake", "--charter", "research.yaml",
                "--approve"], cwd=harness, env=env, ok_codes=(0,))
    result = _hl(["research", "run", ".", "--name", "bake"], cwd=harness, env=env,
                 ok_codes=(0,))
    status = result["status"]
    kept = [e for e in status["experiments"] if e["kept"]]
    applied = None
    if status.get("promotion") and status["promotion"]["status"] == "pending":
        applied = _hl(["proposals", "apply", ".", status["promotion"]["proposal_id"], "--yes"],
                      cwd=harness, env=env, ok_codes=(0,))
    spent = sum(pool["spent"] for pool in status["budget"].values())
    return {
        "kept_changes": len(kept),
        "experiments": status["experiments"],
        "stop": status["stop_reason"],
        "confirmation": status["confirmation"],
        "promoted": bool(applied and applied.get("changed")),
        "spent_usd": round(spent, 4),
        "seconds": init["_seconds"] + result["_seconds"],
    }


def run_evolve(harness: Path, env: dict[str, str], model: str, rounds: int) -> dict:
    baseline = _hl(["eval", "run", "eval.yaml", "--approve"], cwd=harness, env=env)
    result = _hl(["evolve", ".", "--experiment", "eval.yaml", "--yes", "--rounds", str(rounds),
                  "--model", model], cwd=harness, env=env, ok_codes=(0, 1, 4))
    rounds_out = result.get("rounds") or []
    kept = [r for r in rounds_out if r.get("status") == "kept"]
    return {
        "kept_changes": len(kept),
        "rounds": [{k: r.get(k) for k in ("round", "status", "reason", "rationale", "target",
                                          "changed_paths", "cost_usd")} for r in rounds_out],
        "error": result.get("error"),
        "spent_usd": round(sum(float(r.get("cost_usd") or 0) for r in rounds_out), 4),
        "seconds": baseline["_seconds"] + result["_seconds"],
    }


def _total(counts: dict[str, dict[str, int]]) -> float:
    return sum(c["passed"] for c in counts.values()) / sum(c["total"] for c in counts.values())


def score(planted: list[str], before: dict, after: dict, kept: int) -> dict:
    fixed = [d for d in planted if _rate(after.get(d, {"passed": 0, "total": 0})) >= FIXED_AT]
    clean_before = _rate(before["clean"])
    # A false promotion is a kept change that fixed nothing, or one that left the
    # harness worse on cases it never saw.
    regressed = _total(after) < _total(before)
    return {
        "fixed": fixed,
        "false_promotions": max(0, kept - len(fixed)) + int(regressed and kept > 0),
        "decoy_fixed": _rate(after["contract"]) >= FIXED_AT,
        "clean_regressed": _rate(after["clean"]) < clean_before,
        "test_success": round(_total(after), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="provider/model for director and evolver")
    parser.add_argument("--evolver-model", help="override the evolve arm's model (smoke tests)")
    parser.add_argument("--arms", default="research,evolve")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--budget", type=float, default=1.0)
    parser.add_argument("--only", action="append", help="a planted harness name (repeatable)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="independent repetitions per harness and arm (model seeds)")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--seed-offset", type=int, default=0,
                        help="fresh case sets (a second run must not reuse the first's cases)")
    args = parser.parse_args()

    work = args.work or Path(tempfile.mkdtemp(prefix="research-bakeoff-"))
    planted_dir = work / "planted"
    harnesses = build(planted_dir, director=args.model, budget=args.budget, rounds=args.rounds,
                      seed_offset=args.seed_offset)
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = args.out / f"bakeoff-{stamp}.jsonl"
    print(f"work: {work}\nresults: {log}")

    jobs = [(source, arm, repeat) for repeat in range(1, args.repeat + 1)
            for source in harnesses if not args.only or source.name in args.only
            for arm in args.arms.split(",")]
    for source, arm, repeat in jobs:
        defects = json.loads((source / "planted.json").read_text())["defects"]
        harness = work / arm / f"{source.name}-{repeat}"
        shutil.copytree(source, harness)
        home = work / "homes" / f"{arm}-{source.name}-{repeat}"
        home.mkdir(parents=True)
        env = {**os.environ, "HIVELOOM_HOME": str(home), "HIVELOOM_TRUST": "always"}
        before = _test(harness, env)
        try:
            if arm == "research":
                outcome = run_research(harness, env, args.model)
            else:
                outcome = run_evolve(harness, env, args.evolver_model or args.model,
                                     args.rounds)
        except RuntimeError as exc:
            outcome = {"kept_changes": 0, "error": str(exc)}
        after = _test(harness, env)
        row = {
            "harness": source.name, "repeat": repeat, "arm": arm, "model": args.model,
            "seed_offset": args.seed_offset, "defects": defects,
            "before": before, "after": after, **outcome,
            **score(defects, before, after, outcome.get("kept_changes", 0)),
        }
        with log.open("a") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
        print(f"{source.name:<22} #{repeat} {arm:<9} fixed {row['fixed']} "
              f"false {row['false_promotions']} test {row['test_success']:.0%} "
              f"${row.get('spent_usd') or 0:.4f}"
              + (f"  ERROR {row['error'][:120]}" if row.get("error") else ""))
    print(summary(log))


def summary(log: Path) -> str:
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    arms: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        totals = arms[row["arm"]]
        totals["defects"] += len(row["defects"])
        totals["fixed"] += len(row["fixed"])
        totals["kept"] += row.get("kept_changes", 0)
        totals["false"] += row["false_promotions"]
        totals["decoy"] += int(row["decoy_fixed"])
        totals["regressed"] += int(row["clean_regressed"])
        totals["usd"] += float(row.get("spent_usd") or 0)
        totals["seconds"] += float(row.get("seconds") or 0)
        for c in row.get("experiments") or []:
            if (c.get("calibration") or {}).get("gap") is not None:
                totals["gap"] += abs(c["calibration"]["gap"])
                totals["predictions"] += 1
    lines = ["| arm | defects fixed | kept changes | false promotions | clean regressions "
             "| decoy fixed | spend | $ per fixed defect | wall clock | calibration error |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for arm, t in arms.items():
        per_fix = f"${t['usd'] / t['fixed']:.4f}" if t["fixed"] else "—"
        calibration = f"{t['gap'] / t['predictions']:.3f}" if t["predictions"] else "—"
        lines.append(f"| {arm} | {int(t['fixed'])}/{int(t['defects'])} | {int(t['kept'])} | "
                     f"{int(t['false'])} | {int(t['regressed'])} | {int(t['decoy'])} | "
                     f"${t['usd']:.4f} | {per_fix} | {t['seconds'] / 60:.1f} min | "
                     f"{calibration} |")
    if {"research", "evolve"} <= set(arms):
        r, e = arms["research"], arms["evolve"]
        ratio = (r["fixed"] / e["fixed"]) if e["fixed"] else float("inf") if r["fixed"] else 0
        fp_r = r["false"] / r["kept"] if r["kept"] else 0.0
        fp_e = e["false"] / e["kept"] if e["kept"] else 0.0
        go = ratio >= 1.5 and fp_r <= fp_e
        lines += ["", f"defects ratio research/evolve: {ratio:.2f} (go needs ≥ 1.5); "
                  f"false-promotion rate {fp_r:.0%} vs {fp_e:.0%} (go needs ≤) → "
                  f"**{'GO' if go else 'NO-GO'}**"]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
