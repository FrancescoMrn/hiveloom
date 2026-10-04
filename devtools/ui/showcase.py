"""Seed a workbench showcase: the three demos, with real history to look at.

A checkout's `harnesses/` are clean folders — no runs, no forks, no proposals —
so every workbench tab opens empty on them. This copies the three demos and
drives each through a short scenario on a real model, so the Runs, Trace,
Versions, Improve and Research tabs, the fork rail and lineage all have
something to show — produced by the harness doing its actual job:

* ticket-triage — triages a support queue it reads from an MCP server, every
  open ticket checked against the system of record; one run is forked at its
  report turn and resumed, and a support lead's request ("urgent first")
  waits in Improve as a gated proposal to review and apply.
* ranked-retrieval — answers engineering questions from a knowledge base; an
  eval measures it, a measured evolution round tries a change and keeps it only
  if the eval confirms it, and a research program attacks the vocabulary gap
  and queues what it confirmed for review in Improve.
* log-forensics — investigates a 77 KB production log through a confined shell,
  the oversized output spilled and read back by handle; the second run recalls
  the first.

Each copy is first moved onto the showcase model with `set model` — the same
validated change the workbench makes — so the version graph opens on a
configured step rather than a lone first version.

    uv run python devtools/ui/showcase.py            # seed once; a no-op after
    uv run python devtools/ui/showcase.py --reset    # throw it away and reseed
    devtools/ui/dev.sh --showcase                    # seed if needed, then serve it

Real model calls: it needs `OPENROUTER_API_KEY` (from the environment, the
showcase's own `home/.env`, or `~/.hiveloom/.env`). On the default model
(`openrouter/deepseek/deepseek-v4.1-flash`) a full seed costs a few cents; every
demo keeps its own `max_cost_usd` guardrail and the research program its
charter budget, so nothing here can run away. `--model` picks another
OpenRouter model.

Everything lands in `devtools/ui/.hiveloom/showcase/` (gitignored): the copies
under `harnesses/`, and a `home/` of their own — Hive, trust store, registry —
so the seeded history never mixes with your own runs of the same harnesses,
which share their ids. A reset keeps the home's `.env` and the providers you
set up in Settings → Models, so the workbench keeps its keys.
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

DEMOS = ["ticket-triage", "ranked-retrieval", "log-forensics"]
DEFAULT_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"

# Survives --reset: what the person configured, not what the seed produced.
KEEP = [Path(".env"), Path("workbench") / "providers.json"]

# A task each demo answers, shown in its interface to copy or adapt.
EXAMPLES = {
    "ticket-triage": "Triage all currently open tickets.",
    "ranked-retrieval": (
        "Our service keeps running out of database connections whenever traffic spikes."
    ),
    "log-forensics": "Investigate data/service.log and report the three facts.",
}


TRIAGE_NOTE = (
    "Support leads read the report top-down during the morning stand-up: list urgent "
    "tickets first, then high, normal, low, so on-call sees what to act on at the top."
)


class Failed(RuntimeError):
    pass


def _openrouter_key() -> str:
    """The key, from wherever it already is; never printed."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    from dotenv import dotenv_values

    for env_file in (HOME / ".env", Path("~/.hiveloom/.env").expanduser()):
        if env_file.is_file():
            value = dotenv_values(env_file).get("OPENROUTER_API_KEY")
            if value:
                return value
    raise Failed(
        "the showcase runs real models and needs OPENROUTER_API_KEY — export it, or add "
        "OpenRouter in the workbench (Settings → Models) and reseed"
    )


def hl(*args: str, expect: tuple[int, ...] = (0, 1)) -> dict:
    """The checkout's CLI against the showcase home; JSON in, JSON out.

    Exit 1 (verification failed) is an outcome a showcase run may honestly
    have, not a seeding error.
    """
    env = {
        **os.environ,
        "HIVELOOM_HOME": str(HOME),
        "HIVELOOM_DB": str(HOME / "hive.db"),
        "OPENROUTER_API_KEY": _openrouter_key(),
    }
    env.pop("VIRTUAL_ENV", None)
    env.pop("HIVELOOM_TRUST", None)
    command = [str(Path(sys.executable).parent / "hiveloom"), *args, "--json"]
    proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1800)
    if proc.returncode not in expect:
        raise Failed(f"hiveloom {' '.join(args)} exited {proc.returncode}: "
                     f"{(proc.stdout or proc.stderr)[-600:]}")
    out = proc.stdout
    # Some commands print a progress line before the JSON document.
    return json.loads(out[out.find("{"):]) if "{" in out else {}


def _home(kept: dict[Path, bytes]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    for relative, body in kept.items():
        target = HOME / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if relative.name == ".env":
            target.chmod(0o600)
    user = Path(os.environ.get("HIVELOOM_HOME", "~/.hiveloom")).expanduser()
    for name in (".env", "models.yaml", "extensions"):
        if (user / name).exists() and not (HOME / name).exists():
            (HOME / name).symlink_to(user / name)


def _copy() -> dict[str, Path]:
    dirs = {}
    for name in DEMOS:
        target = HARNESSES / name
        shutil.copytree(REPO / "harnesses" / name, target,
                        ignore=shutil.ignore_patterns(".hiveloom", "__pycache__", "out", ".env"))
        dirs[name] = target
    return dirs


def _run(d: Path, task: str) -> dict:
    return hl("run", str(d), "--input-text", task)


def ticket_triage(d: Path, model: str) -> str:
    hl("set", "model", model, "--dir", str(d))
    first = _run(d, "Triage all currently open tickets.")
    second = _run(d, "Triage all currently open tickets.")
    # Fork at the last model call — the report turn — and resume it: the same
    # reads replayed, the report written again on the fork.
    points = hl("fork", second["run_id"], "--list")["fork_points"]
    fork = hl("fork", second["run_id"], "--at", str(points[-1]["seq"]), "--name", "report-retake")
    resumed = hl("run", fork["directory"], "--resume")
    # Every run passed, so there are no failures to learn from — changes also
    # come from people. An operator's finding becomes a gated proposal that
    # waits in Improve for someone to review and apply.
    proposal = hl("evolve", str(d), "--propose", "--model", model, "--note", TRIAGE_NOTE)
    return (f"2 triage runs ({first['status']}, {second['status']}), "
            f"fork 'report-retake' resumed ({resumed.get('status')}), "
            f"proposal {proposal.get('status', '?')} in Improve")


def ranked_retrieval(d: Path, model: str) -> str:
    hl("set", "model", model, "--dir", str(d))
    _run(d, "Rank up to three records about PostgreSQL query performance.")
    _run(d, EXAMPLES["ranked-retrieval"])
    hl("eval", "validate", str(d / "eval.yaml"), "--approve")
    baseline = hl("eval", "run", str(d / "eval.yaml"))
    evolved = hl("evolve", str(d), "--experiment", str(d / "eval.yaml"), "--rounds", "1",
                 "--yes", "--model", model)
    hl("research", "init", str(d), "--name", "vocabulary", "--charter",
       str(d / "research.yaml"), "--approve")
    research = hl("research", "run", str(d), "--name", "vocabulary")
    status = hl("research", "status", str(d), "--name", "vocabulary")
    promotion = (status.get("promotion") or {}).get("status") or "nothing to promote"
    del research  # its outcome is read back through `status`, below
    tested = len(status.get("experiments") or [])
    return (f"2 queries, eval {baseline.get('eval_run_id', '?')}, "
            f"experiment kept {evolved.get('kept', 0)}, research {status.get('status')} "
            f"after {tested} experiment(s) ({promotion})")


def log_forensics(d: Path, model: str) -> str:
    hl("set", "model", model, "--dir", str(d))
    task = EXAMPLES["log-forensics"]
    first = _run(d, task)
    # The second run of the same version finds the first through recall_runs.
    second = _run(d, task)
    return f"2 investigations ({first['status']}, {second['status']})"


SCENARIOS = {"ticket-triage": ticket_triage, "ranked-retrieval": ranked_retrieval,
             "log-forensics": log_forensics}


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
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"provider/model-id the demos run on (default {DEFAULT_MODEL})")
    args = parser.parse_args()

    kept = {
        relative: (HOME / relative).read_bytes()
        for relative in KEEP
        if (HOME / relative).is_file() and not (HOME / relative).is_symlink()
    }
    if args.reset:
        shutil.rmtree(ROOT, ignore_errors=True)
    if DONE.is_file():
        interfaces()
        print(f"showcase already seeded at {ROOT} (--reset to reseed)")
        return 0
    # A half-seeded showcase from an interrupted run is not worth resuming.
    shutil.rmtree(ROOT, ignore_errors=True)

    _home(kept)
    try:
        _openrouter_key()
    except Failed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    dirs = _copy()
    for directory in dirs.values():
        hl("trust", str(directory))
    summary = {}
    for name, scenario in SCENARIOS.items():
        print(f"  {name:<17}", end="", flush=True)
        try:
            summary[name] = scenario(dirs[name], args.model)
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
