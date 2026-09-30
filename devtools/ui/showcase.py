"""Seed a workbench showcase: the offline demos, with history to look at.

A checkout's `harnesses/` are clean folders — no runs, no forks, no proposals —
so every workbench tab opens empty on them. This copies the offline demos
(they run on scripted providers: no API key, the same journals every time) and
drives each through the scenario its README describes, so the Runs, Trace,
Versions and Improve tabs, the fork rail and lineage all have something real
to show. The steps are the ones `scripts/package_e2e.py` checks in CI.

    uv run python devtools/ui/showcase.py            # seed once; a no-op after
    uv run python devtools/ui/showcase.py --reset    # throw it away and reseed
    devtools/ui/dev.sh --showcase                    # seed if needed, then serve it

Everything lands in `devtools/ui/.hiveloom/showcase/` (gitignored): the copies
under `harnesses/`, and a `home/` of their own — Hive, trust store, registry —
so the seeded history never mixes with your own runs of the same harnesses,
which share their ids. The home links your `~/.hiveloom/.env`, `models.yaml`
and `extensions/` when you have them, so the copilot keeps its key.

Some things are left for you to do in the interface rather than done here:
the research program's proposal and memory-lab's lessons stay pending, to
review and apply from Improve; research-lab's `concepts` program waits for its
contract to be approved in the Research tab; and every demo has runs to fork
from.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = Path(__file__).resolve().parent / ".hiveloom" / "showcase"
HARNESSES = ROOT / "harnesses"
HOME = ROOT / "home"
DONE = ROOT / "seeded.json"

OFFLINE = ["memory-lab", "routing-lab", "signal-lab", "research-lab", "delegation-lab"]


class Failed(RuntimeError):
    pass


def hl(*args: str, expect: tuple[int, ...] = (0,)) -> dict:
    """The checkout's CLI against the showcase home; JSON in, JSON out."""
    env = {**os.environ, "HIVELOOM_HOME": str(HOME), "HIVELOOM_DB": str(HOME / "hive.db")}
    env.pop("VIRTUAL_ENV", None)
    env.pop("HIVELOOM_TRUST", None)
    command = [str(Path(sys.executable).parent / "hiveloom"), *args, "--json"]
    proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=900)
    if proc.returncode not in expect:
        raise Failed(f"hiveloom {' '.join(args)} exited {proc.returncode}: "
                     f"{(proc.stdout or proc.stderr)[-600:]}")
    return json.loads(proc.stdout)


def _home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    user = Path(os.environ.get("HIVELOOM_HOME", "~/.hiveloom")).expanduser()
    for name in (".env", "models.yaml", "extensions"):
        if (user / name).exists() and not (HOME / name).exists():
            (HOME / name).symlink_to(user / name)


def _copy() -> dict[str, Path]:
    dirs = {}
    for name in OFFLINE:
        target = HARNESSES / name
        shutil.copytree(REPO / "harnesses" / name, target,
                        ignore=shutil.ignore_patterns(".hiveloom", "__pycache__", "out"))
        dirs[name] = target
    return dirs


def _fork(run_id: str, name: str, *extra: str) -> dict:
    """Fork at the middle model call, so the replayed prefix has something in it."""
    points = hl("fork", run_id, "--list")["fork_points"]
    at = points[len(points) // 2]["seq"]
    return hl("fork", run_id, "--at", str(at), "--name", name, *extra)


def memory_lab(d: Path) -> str:
    task = "Investigate data/service.log and report the three facts."
    first = hl("run", str(d), "--input-text", task)
    hl("run", str(d), "--input-text", task)
    hl("evolve", str(d), "--propose", "--model", "memory_lab/qa-evolver",
       "--note", "The build digest is only ever on the last SUMMARY line.")
    fork = _fork(first["run_id"], "probe")
    hl("run", fork["directory"], "--resume")
    pending = [p for p in hl("proposals", "list", str(d))["proposals"] if p["status"] == "pending"]
    return f"2 runs, fork 'probe' resumed, {len(pending)} proposal(s) pending"


def routing_lab(d: Path) -> str:
    base = hl("run", str(d), "--input", str(d / "incident.txt"))
    forced = "FORCE_FAIL: handle incident.txt"
    for _ in range(3):
        hl("run", str(d), "--input-text", forced, expect=(1,))
    proposal = hl("evolve", str(d), "--propose", "--model", "routing_lab/qa-evolver")
    hl("proposals", "apply", str(d), proposal["id"], "--yes")
    for _ in range(5):
        hl("run", str(d), "--input-text", forced)
    verdict = hl("assess", str(d))["assessments"][0]["verdict"]
    # Assessed before the forks: a resumed fork replays the pre-evolution
    # version under the same key, and would be read as one more sample of it.
    replay = _fork(base["run_id"], "replay")
    hl("run", replay["directory"], "--resume")
    alt = hl("fork", base["run_id"], "--name", "on-alt", "--model", "qa-alt",
             "--provider", "routing_lab")
    hl("run", alt["directory"], "--resume")
    return f"3 failures, evolution applied and {verdict}, forks 'replay' and 'on-alt'"


def signal_lab(d: Path) -> str:
    # One lookup that works, then lowercase ids that fail: a reflection, and at
    # the third failure an auto-drafted proposal, both left pending.
    for invoice in ("INV-1003", "inv-1004", "inv-1001", "inv-1002"):
        hl("run", str(d), "--input-text", f"Look up invoice {invoice} and report its amount.",
           expect=(0, 1))
    hl("eval", "run", str(d / "eval.yaml"), "--approve")
    result = hl("evolve", str(d), "--experiment", str(d / "eval.yaml"), "--yes",
                "--rounds", "2", "--model", "signal_lab/qa-evolver")
    hl("run", str(d), "--input-text", "Look up invoice inv-1010 and report its amount.")
    rounds = ", ".join(r["status"] for r in result["rounds"])
    return f"eval, then evolve --experiment rounds: {rounds}"


def research_lab(d: Path) -> str:
    for text in ("Quote shipping for a 2500 g parcel to zone 2 for customer C-100.",
                 "Quote shipping for a 1.2 kg parcel to zone 1 for customer C-201."):
        hl("run", str(d), "--input-text", text, expect=(0, 1))
    hl("research", "init", str(d), "--name", "quotes", "--charter",
       str(d / "research.yaml"), "--approve")
    status = hl("research", "run", str(d), "--name", "quotes")["status"]
    stop = status["stop_reason"]["condition"]
    # A second program in concepts mode, left at its approval gate: the
    # Research tab's contract card and, once approved, its label questions.
    hl("research", "init", str(d), "--name", "concepts", "--charter",
       str(d / "research-concepts.yaml"), "--approve")
    waiting = hl("research", "run", str(d), "--name", "concepts")["status"]
    return (f"research 'quotes' stopped at {stop}; proposal left pending; "
            f"'concepts' waiting for {waiting['awaiting']} approval")


def delegation_lab(d: Path) -> str:
    peer = d / "peers" / "ledger-desk"
    hl("trust", str(peer))
    hl("registry", "add", str(peer))
    question = "What is the amount of invoice INV-1003?"
    hl("run", str(d), "--input-text", question)
    for invoice in ("INV-1001", "inv-1005", "INV-1009"):
        hl("run", str(peer), "--input-text", f"Amount of invoice {invoice}?")
    hl("run", str(d), "--input-text", question)
    return "referred while the peer was unmeasured, then delegated"


SCENARIOS = {"memory-lab": memory_lab, "routing-lab": routing_lab, "signal-lab": signal_lab,
             "research-lab": research_lab, "delegation-lab": delegation_lab}

# A task each demo answers offline, shown in its interface to copy or adapt.
EXAMPLES = {
    "memory-lab": "Investigate data/service.log and report the three facts.",
    "routing-lab": "Handle incident.txt",
    "signal-lab": "Look up invoice INV-1003 and report its amount.",
    "research-lab": "Quote shipping for a 2500 g parcel to zone 2 for customer C-100.",
    "delegation-lab": "What is the amount of invoice INV-1003?",
    "delegation-lab/peers/ledger-desk": "Amount of invoice INV-1001?",
}


def interfaces() -> None:
    """Give every showcase harness the page the copilot's create_interface writes.

    The page is a template filled from the spec, no model involved, so the Use
    tab opens a runnable page instead of the "ask the copilot" explanation.
    """
    import importlib.util

    loader = importlib.util.spec_from_file_location(
        "hiveloom_ui_server", Path(__file__).with_name("server.py"))
    server = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(server)
    ids = {entry["path"]: entry["id"] for entry in server._catalog([], [str(HARNESSES)]).values()}
    for relative, example in EXAMPLES.items():
        directory = HARNESSES / relative
        spec = server.load_spec(server.harness_path(directory))
        page = server._standalone_interface_html(
            harness_id=ids[str(directory.resolve())],
            harness_name=spec.name,
            title=spec.name.replace("-", " ").title(),
            description=spec.description,
            submit_label="Run",
            contract={"kind": "text", "label": "Task", "placeholder": example,
                      "help": f"For example: {example}"},
        )
        target = directory / "interfaces" / "default" / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="discard the showcase and reseed")
    args = parser.parse_args()

    if args.reset:
        shutil.rmtree(ROOT, ignore_errors=True)
    if DONE.is_file():
        interfaces()
        print(f"showcase already seeded at {ROOT} (--reset to reseed)")
        return 0
    # A half-seeded showcase from an interrupted run is not worth resuming.
    shutil.rmtree(ROOT, ignore_errors=True)

    _home()
    dirs = _copy()
    for directory in dirs.values():
        hl("trust", str(directory))
    summary = {}
    for name, scenario in SCENARIOS.items():
        print(f"  {name:<15}", end="", flush=True)
        try:
            summary[name] = scenario(dirs[name])
        except Failed as exc:
            print(f"failed\n{exc}", file=sys.stderr)
            return 1
        print(summary[name])
    interfaces()
    DONE.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"seeded {ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
