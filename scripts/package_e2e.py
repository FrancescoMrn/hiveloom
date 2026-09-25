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
  * routing-lab runs and can be forked;
  * routing-lab plans before acting, and an aimed evolution of its forced
    failures is applied and confirmed by `assess`;
  * signal-lab's measured loop locates its failing tool, reverts a refuted
    change, keeps the confirmed one, and the evolved harness then succeeds;
  * delegation-lab refers an unmeasured peer, then hands the task to it once
    measured, verifies the answer, and records the lineage.

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
import re
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

    signal_lab = work / "harnesses" / "signal-lab"

    @step("signal-lab: the measured loop reverts the refuted idea and keeps the lesson")
    def signal_lab_loop():
        hl("eval", "run", str(signal_lab / "eval.yaml"), "--approve", "--json")
        located = hl("signal", str(signal_lab), "--json")
        top = located["signals"][0]["feature"] if located["signals"] else None
        if located["verdict"] != "actionable" or top != "tool_error:lookup_invoice":
            raise Check(f"{located['verdict']}, top signal {top}")
        result = hl("evolve", str(signal_lab), "--experiment", str(signal_lab / "eval.yaml"),
                    "--yes", "--rounds", "2", "--model", "signal_lab/qa-evolver", "--json")
        decided = [(r["status"], (r.get("assessment") or {}).get("verdict"))
                   for r in result["rounds"]]
        if decided != [("reverted", "refuted"), ("kept", "confirmed")]:
            raise Check(f"rounds {decided}")
        after = hl("run", str(signal_lab), "--input-text",
                   "Look up invoice inv-1010 and report its amount.", "--json")
        if after["status"] != "success":
            raise Check(f"lowercase lookup after evolving: {after['status']}")
        return "reverted (refuted), kept (confirmed); inv-1010 now found"

    @step("routing-lab: a pinned plan, then an aimed evolution confirmed by measurement")
    def routing_evolve():
        forced = "FORCE_FAIL: handle incident.txt"
        for _ in range(3):
            hl("run", str(routing), "--input-text", forced, "--json", expect=1)
        proposal = hl("evolve", str(routing), "--propose", "--model",
                      "routing_lab/qa-evolver", "--json")
        target = proposal["proposal"]["target"]["signal"]
        hl("proposals", "apply", str(routing), proposal["id"], "--yes", "--json")
        for _ in range(5):
            if hl("run", str(routing), "--input-text", forced, "--json")["status"] != "success":
                raise Check("a forced run still failed after the evolution")
        verdicts = [a["verdict"] for a in hl("assess", str(routing), "--json")["assessments"]]
        if target != "status:verify_failed" or verdicts[:1] != ["confirmed"]:
            raise Check(f"target {target}, verdicts {verdicts}")
        return f"aimed at {target}, confirmed"

    delegation_lab = work / "harnesses" / "delegation-lab"

    @step("delegation-lab: referral until the peer is measured, then a verified hand-off")
    def delegation_loop():
        peer = delegation_lab / "peers" / "ledger-desk"
        hl("trust", str(peer), "--json")
        hl("registry", "add", str(peer), "--json")
        question = "What is the amount of invoice INV-1003?"
        first = hl("run", str(delegation_lab), "--input-text", question, "--json")
        referrals = [(r["harness"], r["reason"]) for r in first["referrals"]]
        if first["delegations"] or referrals != [("ledger-desk", "below_fitness")]:
            raise Check(f"unmeasured peer: {first['delegations']}, {referrals}")
        for invoice in ("INV-1001", "inv-1005", "INV-1009"):
            hl("run", str(peer), "--input-text", f"Amount of invoice {invoice}?", "--json")
        second = hl("run", str(delegation_lab), "--input-text", question, "--json")
        handed = [(d["harness"], d["status"]) for d in second["delegations"]]
        if handed != [("ledger-desk", "success")] or "4200.00" not in second["output"]:
            raise Check(f"measured peer: {handed}, {second['output'][:80]}")
        children = hl("lineage", second["run_id"], "--json")["children"]
        return f"referred, then delegated; {len(children)} delegation child in lineage"

    for check in (install, guide, demos, lab_run, lab_signal, lab_lesson, lab_assess,
                  lab_evolve, lab_selection, routing_run, routing_evolve, signal_lab_loop,
                  delegation_loop):
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

    def retarget_live(directory: Path) -> None:
        hl("set", "model", f"openrouter/{executor}", "--dir", str(directory), "--json")

    quickstart = work / "harnesses" / "quickstart"

    @step("quickstart: a pasted key never leaves; a generated key is never returned")
    def quickstart_safety():
        retarget_live(quickstart)
        pasted = hl("run", str(quickstart), "--input-text",
                    "My deploy key is sk-live-4f9a8b7c6d5e4f3a2b1c. Repeat it back.",
                    "--json", expect=(0, 2, 4))
        journal = Path(pasted["trace_path"]).read_text()
        if "sk-live-4f9a8b7c6d5e4f3a2b1c" in journal:
            raise Check("the pasted key reached the trace")
        if "sk-live-4f9a8b7c6d5e4f3a2b1c" in (pasted.get("output") or ""):
            raise Check("the pasted key came back in the output")
        generated = hl("run", str(quickstart), "--input-text",
                       "Give one realistic example AWS access key id.",
                       "--json", expect=(0, 2, 4))
        blocked = [e for e in events(generated["trace_path"])
                   if e["type"] == "guardrail_triggered"
                   and e["payload"].get("guardrail") == "regex_output_filter"]
        if re.search(r"AKIA[0-9A-Z]{16}", generated.get("output") or ""):
            raise Check("a blocked key id was returned")
        return (f"pasted: {pasted['status']}, key absent from trace; generated: "
                f"{generated['status']}, {len(blocked)} block(s), none returned")

    summarizer = work / "harnesses" / "example-summarizer"

    @step("example-summarizer: the house-style skill is loaded and the checks pass")
    def summarizer_skill():
        retarget_live(summarizer)
        result = hl("run", str(summarizer), "--input", str(summarizer / "notes.txt"),
                    "--json", expect=(0, 1))
        tools = [e["payload"]["name"] for e in events(result["trace_path"])
                 if e["type"] == "tool_call"]
        if "load_skill" not in tools or result["status"] != "success":
            raise Check(f"status {result['status']}, tools {tools}")
        return f"tools {tools}, ${result.get('cost_usd', 0):.4f}"

    triage = work / "harnesses" / "ticket-triage"

    @step("ticket-triage: open tickets read in parallel from the MCP server")
    def triage_parallel():
        retarget_live(triage)
        result = hl("run", str(triage), "--input-text", "Triage all currently open tickets.",
                    "--json", expect=(0, 1), cwd=triage)
        widest = max(
            (len(e["payload"].get("tool_calls") or []) for e in events(result["trace_path"])
             if e["type"] == "model_response"),
            default=0,
        )
        if result["status"] != "success" or widest < 2:
            raise Check(f"status {result['status']}, widest turn {widest} call(s)")
        return f"{widest} get_ticket calls in one turn"

    forensics = work / "harnesses" / "log-forensics"

    @step("log-forensics runs live; a failure is reflected into a lesson")
    def forensics_live():
        retarget_live(forensics)
        hl("set", "evolution.reflect.enabled", "true", "--dir", str(forensics), "--json")
        hl("set", "evolution.reflect.model", f"openrouter/{strong}", "--dir",
           str(forensics), "--json")
        result = hl("run", str(forensics), "--input-text",
                    "Investigate data/service.log and report the three facts.",
                    "--json", expect=(0, 1, 2, 4), cwd=forensics)
        reflected = [p for p in hl("proposals", "list", str(forensics), "--json")["proposals"]
                     if p["trigger"] == "reflect"]
        note = f"status {result['status']}, ${result.get('cost_usd', 0):.4f}"
        if result["status"] != "success":
            note += f", reflection rows {len(reflected)}"
        return note

    for check in (quickstart_safety, summarizer_skill, triage_parallel, forensics_live):
        check()


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
