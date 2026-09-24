#!/usr/bin/env python3
"""End-to-end test of the *installed* hiveloom package against the demo harnesses.

Unit tests run the source tree. This runs the wheel a user would install, in
an isolated environment with no checkout on its path, and drives the CLI the
way a builder agent would: every call with --json, every exit code checked.

Offline (default, no credentials, no network beyond building):
  * the wheel installs, imports, reports its version, and ships every guide topic;
  * every demo harness validates and dry-runs;
  * memory-lab runs, its journal verifies, the signal map reads it, the lesson
    the executor queued is applied through the review queue, `assess` sees the
    evolution, an aimed proposal is drafted by the harness's scripted evolver,
    and relevance-selected memory is journalled and searchable;
  * routing-lab runs and can be forked.

Live (--live, needs OPENROUTER_API_KEY; spends real money, bounded):
  * ranked-retrieval, moved onto a small OpenRouter executor, is measured on its
    eval, its signal located, and evolved with `evolve --experiment` for a few
    rounds by a strong OpenRouter model; `assess` reports every decision;
  * quickstart, example-summarizer and log-forensics run live on the same
    executor, and any failure is reflected into a queued lesson.

Usage:
  python scripts/package_e2e.py [--wheel dist/hiveloom-X-py3-none-any.whl]
      [--live] [--executor openrouter-model-id] [--strong openrouter-model-id]
      [--rounds N] [--keep WORKDIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


class Check(Exception):
    pass


def step(name: str):
    def wrap(fn):
        def run(*args, **kwargs):
            try:
                detail = fn(*args, **kwargs) or ""
                RESULTS.append((name, True, str(detail)))
                print(f"  ok   {name}{': ' + str(detail) if detail else ''}", flush=True)
            except Exception as exc:  # noqa: BLE001 - reported, then the run fails
                RESULTS.append((name, False, str(exc)))
                print(f"  FAIL {name}: {exc}", flush=True)
        return run
    return wrap


class Hiveloom:
    """The installed CLI, run in an isolated environment with its own Hive."""

    def __init__(self, wheel: Path, home: Path, env: dict[str, str]):
        self.wheel = wheel
        self.env = {
            **os.environ,
            **env,
            "HIVELOOM_HOME": str(home),
            "HIVELOOM_DB": str(home / "hive.db"),
            # These are the repository's own demo folders (copied): the documented
            # CI trust policy, exactly as the harness job in ci.yml uses it.
            "HIVELOOM_TRUST": "always",
        }
        self.env.pop("VIRTUAL_ENV", None)

    def raw(self, *args: str, cwd: Path | None = None, timeout: int = 900):
        command = ["uv", "run", "--isolated", "--no-project", "--with", str(self.wheel),
                   "--", *args]
        return subprocess.run(command, cwd=cwd, env=self.env, capture_output=True,
                              text=True, timeout=timeout)

    def __call__(self, *args: str, cwd: Path | None = None, expect: int | tuple = 0,
                 timeout: int = 900) -> dict:
        proc = self.raw("hiveloom", *args, cwd=cwd, timeout=timeout)
        codes = expect if isinstance(expect, tuple) else (expect,)
        if proc.returncode not in codes:
            raise Check(
                f"`hiveloom {' '.join(args)}` exited {proc.returncode}, expected {codes}: "
                f"{(proc.stdout or proc.stderr)[-800:]}"
            )
        text = proc.stdout.strip()
        if "--json" not in args:
            return {"stdout": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise Check(f"`hiveloom {' '.join(args)}` did not emit JSON: {text[-400:]}") from exc


def events(trace_path: str) -> list[dict]:
    return [json.loads(line) for line in Path(trace_path).read_text().splitlines() if line]


# --------------------------------------------------------------------------- #
# Offline
# --------------------------------------------------------------------------- #
def offline(hl: Hiveloom, work: Path) -> None:
    @step("wheel installs, imports and reports its version")
    def install():
        proc = hl.raw("python", "-c", "import hiveloom; print(hiveloom.__version__)")
        if proc.returncode:
            raise Check(proc.stderr[-400:])
        version = proc.stdout.strip()
        source = (ROOT / "src/hiveloom/__init__.py").read_text()
        if f'__version__ = "{version}"' not in source:
            raise Check(f"installed {version} does not match the source tree")
        return version

    @step("guide ships every topic, including signal")
    def guide():
        topics = {t["name"] for t in hl("guide", "--list", "--json")["topics"]}
        missing = {"agents", "build", "run", "evolve", "signal", "spec"} - topics
        if missing:
            raise Check(f"missing topics {missing}")
        return f"{len(topics)} topics"

    @step("every demo harness validates and dry-runs")
    def demos():
        names = sorted(p.name for p in (work / "harnesses").iterdir() if p.is_dir())
        for name in names:
            directory = work / "harnesses" / name
            hl("validate", str(directory), "--json")
            hl("run", str(directory), "--input-text", "dry run", "--dry-run", "--json")
        return ", ".join(names)

    lab = work / "harnesses" / "memory-lab"
    task = "Investigate data/service.log and report the three facts."
    state: dict = {}

    @step("memory-lab runs offline and its journal verifies")
    def lab_run():
        result = hl("run", str(lab), "--input-text", task, "--json")
        if result["status"] != "success":
            raise Check(f"status {result['status']}")
        verified = hl("trace", result["run_id"], "--verify", "--json")
        if not verified["chained"] or verified["broken_at"] is not None:
            raise Check(f"journal broken: {verified}")
        state["run"] = result
        return f"{result['run_id']}, {verified['checked']} events chained"

    @step("signal locates nothing to fix on an all-success harness, for free")
    def lab_signal():
        signal = hl("signal", str(lab), "--json")
        if signal["verdict"] != "no_failures" or signal["quality"]["runs"] < 1:
            raise Check(f"unexpected map: {signal['verdict']}, {signal['quality']}")
        if signal["quality"]["unindexed_runs"]:
            raise Check("a fresh run was not feature-indexed")
        return signal["headline"][0][:90]

    @step("the executor's lesson reaches the spec only through proposals apply")
    def lab_lesson():
        queued = hl("proposals", "list", str(lab), "--json")["proposals"]
        executor = [p for p in queued if p["trigger"] == "executor" and p["status"] == "pending"]
        if not executor:
            seen = [(p["trigger"], p["status"]) for p in queued]
            raise Check(f"no executor lesson queued: {seen}")
        before = hl("memory", "list", str(lab), "--json")["count"]
        applied = hl("proposals", "apply", str(lab), executor[0]["id"], "--yes", "--json")
        result = applied.get("apply_result") or applied
        after = hl("memory", "list", str(lab), "--json")["count"]
        if not result["changed"] or after != before + 1:
            raise Check(f"memory {before} -> {after}")
        return f"memory {before} -> {after}"

    @step("assess reports the applied lesson against the runs since")
    def lab_assess():
        [latest, *_] = hl("assess", str(lab), "--json")["assessments"]
        if latest["verdict"] != "pending" or latest["target"] != "success_rate":
            raise Check(f"{latest['verdict']} on {latest['target']}")
        hl("run", str(lab), "--input-text", task, "--json")
        [latest, *_] = hl("assess", str(lab), "--min-runs", "1", "--json")["assessments"]
        if latest["verdict"] not in ("inconclusive", "confirmed"):
            raise Check(f"after one more run: {latest['verdict']}")
        return latest["summary"][:100]

    @step("evolve --propose drafts an aimed proposal")
    def lab_evolve():
        proposal = hl("evolve", str(lab), "--propose", "--model", "memory_lab/qa-evolver",
                      "--note", "The build digest is only ever on the last SUMMARY line.",
                      "--json")
        target = proposal["proposal"]["target"]
        if proposal["status"] != "pending" or not target or target["signal"] != "success_rate":
            raise Check(f"{proposal['status']}, target {target}")
        return f"{proposal['id']} aims at {target['signal']} ({target['expect']})"

    @step("relevance-selected memory is journalled and searchable")
    def lab_selection():
        hl("set", "memory.selection", "relevant", "--dir", str(lab), "--json")
        dry = hl("run", str(lab), "--input-text", task, "--dry-run", "--json")
        names = json.dumps(dry)
        if "search_memory" not in names:
            raise Check("search_memory not offered in relevant mode")
        result = hl("run", str(lab), "--input-text", task, "--json")
        selected = [e for e in events(result["trace_path"]) if e["type"] == "memory_selected"]
        if not selected:
            raise Check("no memory_selected event")
        payload = selected[0]["payload"]
        return f"{len(payload['ids'])} of {payload['stored']} entries shown"

    routing = work / "harnesses" / "routing-lab"

    @step("routing-lab runs offline and exposes fork points")
    def routing_run():
        incident = routing / "incident.txt"
        result = hl("run", str(routing), "--input", str(incident), "--json")
        if result["status"] != "success":
            raise Check(f"status {result['status']}")
        points = hl("fork", result["run_id"], "--list", "--json")["fork_points"]
        if not points:
            raise Check("no fork points")
        return f"{len(points)} fork points"

    for check in (install, guide, demos, lab_run, lab_signal, lab_lesson, lab_assess,
                  lab_evolve, lab_selection, routing_run):
        check()


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def live(hl: Hiveloom, work: Path, executor: str, strong: str, rounds: int) -> None:
    retrieval = work / "harnesses" / "ranked-retrieval"

    @step("ranked-retrieval moves onto the OpenRouter executor (builder-side set)")
    def retarget():
        # Provider and id validate against each other, so they move together.
        hl("set", "model", f"openrouter/{executor}", "--dir", str(retrieval), "--json")
        # The eval pinned Anthropic's served snapshot; an aggregator serves its
        # own ids, so the copy accepts what it serves and records it.
        eval_path = retrieval / "eval.yaml"
        text = eval_path.read_text()
        text = text.replace("model_identity: alias", "model_identity: warn")
        text = text.replace("repetitions: 2", "repetitions: 3")
        eval_path.write_text(text)
        hl("eval", "validate", str(eval_path), "--approve", "--json")
        return executor

    @step("the baseline is measured on the eval")
    def baseline():
        manifest = hl("eval", "run", str(retrieval / "eval.yaml"), "--approve", "--json",
                      timeout=3600)
        summary = manifest.get("summary") or {}
        return json.dumps(summary)[:160]

    @step("signal locates where the executor fails")
    def locate():
        signal = hl("signal", str(retrieval), "--json")
        return f"{signal['verdict']}: " + " | ".join(signal["headline"][:3])[:400]

    @step(f"evolve --experiment runs {rounds} measured round(s)")
    def experiment():
        result = hl("evolve", str(retrieval), "--experiment", str(retrieval / "eval.yaml"),
                    "--yes", "--rounds", str(rounds), "--model", f"openrouter/{strong}",
                    "--json", timeout=7200)
        lines = []
        for item in result["rounds"]:
            verdict = (item.get("assessment") or {}).get("verdict")
            target = (item.get("target") or {}).get("signal")
            lines.append(f"r{item['round']} {item['status']} ({verdict}, target {target})")
        return "; ".join(lines) + f"; kept {result['kept']}"

    @step("assess reports every experiment decision")
    def verdicts():
        assessments = hl("assess", str(retrieval), "--json")["assessments"]
        return "; ".join(
            f"#{a['counter']} {a['verdict']}"
            + (f" [{a['decision']['action']}]" if a.get("decision") else "")
            for a in assessments
        ) or "no evolutions applied"

    @step("the evolved harness still validates")
    def still_valid():
        hl("validate", str(retrieval), "--json")

    for check in (retarget, baseline, locate, experiment, verdicts, still_valid):
        check()

    for name, text in (
        ("quickstart", "Explain in two sentences what a harness is."),
        ("example-summarizer", None),
        ("log-forensics", None),
    ):
        directory = work / "harnesses" / name

        @step(f"{name} runs live, and a failure is reflected into a lesson")
        def live_run(directory=directory, text=text):
            hl("set", "model", f"openrouter/{executor}", "--dir", str(directory), "--json")
            hl("set", "evolution.reflect.enabled", "true", "--dir", str(directory), "--json")
            hl("set", "evolution.reflect.model", f"openrouter/{strong}", "--dir",
               str(directory), "--json")
            args = ["run", str(directory), "--json"]
            readme = (directory / "README.md").read_text()
            if text is None:
                # Use the task the demo documents for itself.
                sample = next(
                    (line.split("--input-text", 1)[1].strip().strip("\\").strip().strip('"')
                     for line in readme.splitlines() if "--input-text" in line), None,
                )
                sample_file = next(
                    (line.split("--input ", 1)[1].split()[0]
                     for line in readme.splitlines() if "run . --input " in line), None,
                )
                if sample_file and (directory / sample_file).exists():
                    args += ["--input", str(directory / sample_file)]
                else:
                    args += ["--input-text", sample or "Run the documented task."]
            else:
                args += ["--input-text", text]
            result = hl(*args, expect=(0, 1, 2, 4), cwd=directory, timeout=1800)
            queued = [
                p for p in hl("proposals", "list", str(directory), "--json")["proposals"]
                if p["trigger"] == "reflect"
            ]
            note = f"status {result['status']}, ${result.get('cost_usd', 0):.4f}"
            if result["status"] != "success":
                note += f", reflection rows {len(queued)}"
            return note

        live_run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--executor", default="mistralai/ministral-8b-2512")
    parser.add_argument("--strong", default="anthropic/claude-sonnet-5")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--keep", type=Path, help="Work directory to keep for inspection.")
    args = parser.parse_args()

    wheel = args.wheel
    if wheel is None:
        dist = Path(tempfile.mkdtemp(prefix="hiveloom-dist-"))
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=ROOT,
                       check=True, capture_output=True)
        wheel = next(dist.glob("hiveloom-*.whl"))
    wheel = wheel.resolve()
    work = args.keep or Path(tempfile.mkdtemp(prefix="hiveloom-e2e-"))
    work.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT / "harnesses", work / "harnesses", dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".hiveloom", "__pycache__", ".env", "runtime"),
    )
    env = {}
    if args.live:
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("--live needs OPENROUTER_API_KEY", file=sys.stderr)
            return 3
    else:
        # Offline means offline: no provider key may leak into the demo runs.
        for key in list(os.environ):
            if key.endswith("_API_KEY"):
                env[key] = ""
    hl = Hiveloom(wheel, work / "home", env)
    print(f"hiveloom package e2e — wheel {wheel.name}, work {work}")
    print("offline:")
    offline(hl, work)
    if args.live:
        print(f"live (executor {args.executor}, strong {args.strong}):")
        live(hl, work, args.executor, args.strong, args.rounds)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
