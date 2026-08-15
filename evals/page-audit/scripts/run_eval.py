#!/usr/bin/env python3
"""Run the page-audit arms via `hiveloom run --json`. Resumable."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
HIVELOOM = str((ROOT / ".." / ".." / ".venv" / "bin" / "hiveloom").resolve())
DATASET = ROOT.parent / "article-extractor" / "dataset" / "samples.jsonl"

# Calibrated by scripts/preflight.py: s15/s22/s14 have MORE H2s on the page
# than the fetch_clean digest shows (truncation stress); s09 is complete
# (control); s25 has a published date (arithmetic); s01 has neither (null path).
SAMPLE_IDS = ["s09", "s15", "s14", "s22", "s25", "s01"]
ARMS = ["opus-harness", "opus-raw", "sonnet-harness", "sonnet-raw"]


def load_samples() -> list[tuple[str, str]]:
    rows = {}
    for line in DATASET.read_text().splitlines():
        d = json.loads(line)
        rows[d["id"]] = d["url"]
    return [(sid, rows[sid]) for sid in SAMPLE_IDS]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--samples", type=int, default=len(SAMPLE_IDS))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--out", default="results/results.jsonl")
    args = ap.parse_args()

    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["arm"], r["sample_id"], r["epoch"]))

    samples = load_samples()[: args.samples]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    total = len(arms) * len(samples) * args.epochs
    n = 0
    with out_path.open("a") as out:
        for arm in arms:
            arm_dir = ROOT / "harnesses" / arm
            for epoch in range(1, args.epochs + 1):
                for sid, url in samples:
                    n += 1
                    if (arm, sid, epoch) in done:
                        print(f"[{n}/{total}] {arm} {sid} e{epoch}: already done")
                        continue
                    t0 = time.monotonic()
                    try:
                        proc = subprocess.run(
                            [HIVELOOM, "run", str(arm_dir), "--input", url, "--json"],
                            capture_output=True,
                            text=True,
                            timeout=420,
                        )
                        wall = time.monotonic() - t0
                        try:
                            payload = json.loads(proc.stdout)
                        except (json.JSONDecodeError, ValueError):
                            payload = {"raw_stdout": proc.stdout[-2000:]}
                        rec = {
                            "arm": arm,
                            "sample_id": sid,
                            "url": url,
                            "epoch": epoch,
                            "exit_code": proc.returncode,
                            "wall_seconds": round(wall, 2),
                            "stderr_tail": proc.stderr[-500:] if proc.returncode else "",
                            **{k: payload.get(k) for k in (
                                "status", "output", "turns", "cost_usd",
                                "duration_seconds", "run_id", "reason",
                            )},
                        }
                        if "raw_stdout" in payload:
                            rec["raw_stdout"] = payload["raw_stdout"]
                    except subprocess.TimeoutExpired:
                        rec = {
                            "arm": arm, "sample_id": sid, "url": url, "epoch": epoch,
                            "exit_code": -1, "status": "timeout",
                            "wall_seconds": round(time.monotonic() - t0, 2),
                        }
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    print(
                        f"[{n}/{total}] {arm} {sid} e{epoch}: "
                        f"{rec.get('status')} exit={rec['exit_code']} "
                        f"cost=${(rec.get('cost_usd') or 0):.4f} {rec['wall_seconds']}s"
                    )
    print("done — score with: python scripts/score.py")


if __name__ == "__main__":
    main()
