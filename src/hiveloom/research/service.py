"""Research programs for an interface: payloads, a charter template, background jobs.

The workbench (and any other front end) drives programs through this module
rather than through the engine directly: it shapes what a screen shows, drafts
a starting charter from a harness, and runs a program in a background thread
so an HTTP request never waits minutes for an eval. The engine stays the only
thing that decides anything.
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hiveloom.spec.loader import harness_path, load_spec

from .charter import Charter
from .engine import Engine
from .execution import classify
from .program import Program, init_program, list_programs, program_root

#: What a started program may be told to do in one job.
UNTIL = ("unit", "round", "done")
_LEDGER_TAIL = 80
_DEFAULT_DIRECTOR = "openrouter/openai/gpt-5-mini"

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job_key(harness_dir: str | Path, name: str) -> str:
    return str(program_root(harness_dir, name).resolve())


def job(harness_dir: str | Path, name: str) -> dict[str, Any] | None:
    with _jobs_lock:
        current = _jobs.get(_job_key(harness_dir, name))
        return dict(current) if current else None


def start(harness_dir: str | Path, name: str, until: str = "round") -> dict[str, Any]:
    """Run a program in a background thread until ``until``; one job per program."""
    if until not in UNTIL:
        raise ValueError(f"until must be one of {', '.join(UNTIL)}")
    engine = Engine(Program(harness_dir, name))  # fail here, not in the thread
    if engine.program.load_state()["unit"] == "done":
        raise ValueError(f"research program '{name}' has finished")
    with engine.program.lock():  # busy elsewhere (a CLI run): say so now, not later
        pass
    key = _job_key(harness_dir, name)
    with _jobs_lock:
        if key in _jobs and _jobs[key]["running"]:
            raise ValueError(f"research program '{name}' is already running")
        record = {"running": True, "until": until, "started_at": _now(), "finished_at": None,
                  "error": None, "steps": []}
        _jobs[key] = record

    marker = _marker(harness_dir, name)
    marker.write_text(json.dumps({"until": until, "started_at": record["started_at"]}),
                      encoding="utf-8")

    def work() -> None:
        try:
            for step in engine.iter_run(until=until):
                with _jobs_lock:
                    record["steps"].append(step)
        except Exception as exc:  # noqa: BLE001 - reported to the interface
            with _jobs_lock:
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["detail"] = traceback.format_exc()[-4000:]
        finally:
            # A job that ends, however, clears its marker; one that survives means
            # the process died mid-run, and resume_interrupted picks it up.
            marker.unlink(missing_ok=True)
            with _jobs_lock:
                record["running"] = False
                record["finished_at"] = _now()

    threading.Thread(target=work, name=f"research-{name}", daemon=True).start()
    return dict(record)


def _marker(harness_dir: str | Path, name: str) -> Path:
    return program_root(harness_dir, name) / "job.json"


def resume_interrupted(harness_dirs: list[str | Path]) -> list[dict[str, Any]]:
    """Restart the jobs a previous workbench process was running when it stopped.

    A job's marker outlives it only when the process died mid-run (a restart, a
    crash, a closed laptop). Its program resumes where its files say — the
    interrupted eval continues rather than starting over — with the same scope.
    """
    resumed = []
    for harness_dir in harness_dirs:
        for row in list_programs(harness_dir):
            marker = _marker(harness_dir, row["name"])
            if not marker.exists():
                continue
            if row["status"] == "done":
                marker.unlink(missing_ok=True)
                continue
            until = json.loads(marker.read_text(encoding="utf-8")).get("until") or "done"
            try:
                start(harness_dir, row["name"], until)
            except Exception as exc:  # noqa: BLE001 - a program that cannot resume stays put
                resumed.append({"harness": str(harness_dir), "program": row["name"],
                                "resumed": False, "error": str(exc)})
                continue
            resumed.append({"harness": str(harness_dir), "program": row["name"],
                            "resumed": True, "until": until})
    return resumed


def progress(harness_dir: str | Path, name: str, state: dict[str, Any]) -> dict | None:
    """How far the eval now running has got: its purpose and cells done of total."""
    from hiveloom.eval_runner import load_eval_manifest

    current = job(harness_dir, name)
    if not current or not current["running"]:
        return None
    for purpose, eval_run_id in reversed(list((state.get("inflight") or {}).items())):
        try:
            manifest = load_eval_manifest(eval_run_id)
        except (OSError, ValueError, LookupError):
            continue
        done = sum(cell.status == "completed" for cell in manifest.cells)
        if manifest.status != "completed" or done < len(manifest.cells):
            return {"purpose": purpose, "unit": state["unit"], "completed": done,
                    "total": len(manifest.cells)}
    return {"purpose": None, "unit": state["unit"], "completed": 0, "total": 0}


def stop(harness_dir: str | Path, name: str, reason: str = "stopped from the workbench") -> None:
    Engine(Program(harness_dir, name)).request_stop(reason)


def programs(harness_dir: str | Path) -> list[dict[str, Any]]:
    rows = list_programs(harness_dir)
    for row in rows:
        current = job(harness_dir, row["name"])
        row["running"] = bool(current and current["running"])
    return rows


def detail(harness_dir: str | Path, name: str) -> dict[str, Any]:
    """Everything a program screen shows, in one payload."""
    program = Program(harness_dir, name)
    engine = Engine(program)
    state = program.load_state()
    report = program.root / "report.md"
    events = program.ledger.events()
    return {
        **engine.status(),
        "goal": program.charter.goal,
        "charter": program.charter.model_dump(mode="json"),
        "split": {"working": len(state["split"]["working"]),
                  "holdout": len(state["split"]["holdout"])},
        "hypotheses": [
            {k: h.get(k) for k in ("id", "round", "claim", "levers", "target", "expect", "by",
                                   "prior", "falsifier", "status")}
            for h in state["hypotheses"]
        ],
        "experiments": [
            {k: e.get(k) for k in ("id", "round", "hypothesis", "candidate", "base", "changes",
                                   "verdict", "kept", "stopped_early", "guard_ok",
                                   "measured_effect", "success_gain", "success",
                                   "target_measure", "summary")}
            for e in state["experiments"]
        ],
        "pending_experiments": [e["id"] for e in state["pending_experiments"]],
        "calibration": state["calibration"],
        "handoffs": state.get("handoffs", []),
        "report": report.read_text(encoding="utf-8") if report.exists() else None,
        "ledger_tail": [
            {"seq": e["seq"], "ts": e["ts"], "kind": e["kind"], "data": e["data"]}
            for e in events[-_LEDGER_TAIL:]
        ],
        "job": job(harness_dir, name),
        "progress": progress(harness_dir, name, state),
        "experiment_runs": experiment_runs(state),
        **concepts_detail(engine, state),
    }


def experiment_runs(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per experiment, each case's run on its base version beside its run on the candidate.

    Run ids open in the workbench's Trace view like any other run: this is how a
    verdict is read case by case.
    """
    from hiveloom.eval_runner import case_key_for, load_eval_manifest

    evals = {"c0": state.get("baseline_eval")}
    for experiment in state["experiments"]:
        evals[experiment["candidate"]] = experiment.get("eval_run_id")
    names = {case_key_for(cid): cid for cid in state["split"]["working"]}

    def cells(eval_run_id: str | None) -> dict[str, Any]:
        if not eval_run_id:
            return {}
        try:
            manifest = load_eval_manifest(eval_run_id)
        except (OSError, ValueError, LookupError):
            return {}
        return {cell.case_key: cell for cell in manifest.cells if cell.case_key in names}

    out: dict[str, list[dict[str, Any]]] = {}
    for experiment in state["experiments"]:
        before, after = cells(evals.get(experiment["base"])), cells(experiment.get("eval_run_id"))
        rows = []
        for key in sorted(set(before) | set(after), key=lambda k: names[k]):
            b, a = before.get(key), after.get(key)
            rows.append({
                "case": names[key],
                "before_run": b.run_id if b else None,
                "before_status": b.run_status if b else None,
                "after_run": a.run_id if a and a.status == "completed" else None,
                "after_status": a.run_status if a and a.status == "completed" else None,
            })
        out[experiment["id"]] = rows
    return out


def concepts_detail(engine: Engine, state: dict[str, Any]) -> dict[str, Any]:
    """The contract, its sample cases, trust per criterion, and the questions."""
    if not engine.charter.concepts_mode:
        return {}
    contract = engine.contract(state)
    return {
        "contract": contract.model_dump(mode="json") if contract else None,
        "contract_version": state.get("contract_version"),
        "draft_contract": state.get("draft_contract") if state["unit"] == "approve" else None,
        "sample_cases": state.get("sample_cases") if state["unit"] == "approve" else None,
        "trust": state.get("trust", []),
        "question_list": [q.model_dump(mode="json") for q in engine.questions.all()],
        "working_cases": len(engine.cases.working()),
    }


def approve(harness_dir: str | Path, name: str, contract_text: str | None = None) -> dict:
    """The user's gate: approve the drafted contract, or an edited one (YAML)."""
    edited = None
    if contract_text is not None and contract_text.strip():
        try:
            edited = yaml.safe_load(contract_text)
        except yaml.YAMLError as exc:
            raise ValueError(f"the contract is not valid YAML: {exc}") from exc
    Engine(Program(harness_dir, name)).approve_contract(edited)
    return detail(harness_dir, name)


def answer(harness_dir: str | Path, name: str, question_id: str, value: str) -> dict:
    return Engine(Program(harness_dir, name)).answer(question_id, value)


def create(harness_dir: str | Path, name: str, charter_text: str) -> dict[str, Any]:
    """Start a program from charter YAML; the harness must already be trusted."""
    try:
        raw = yaml.safe_load(charter_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"the charter is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the charter must be a YAML mapping")
    program = init_program(harness_dir, name, Charter.model_validate(raw))
    return detail(program.base, program.name)


def charter_templates(harness_dir: str | Path) -> dict[str, str]:
    """Every research*.yaml charter in the harness folder, or one drafted from its spec."""
    base = harness_path(harness_dir).parent
    # research.yaml, the harness's canonical charter, first; any variants after it.
    paths = sorted(base.glob("research*.yaml"), key=lambda p: (p.name != "research.yaml", p.name))
    found = {path.name: path.read_text(encoding="utf-8") for path in paths if path.is_file()}
    return found or {"research.yaml": charter_template(base)}


def charter_template(harness_dir: str | Path) -> str:
    """The harness's own research.yaml, or a draft charter built from its spec."""
    base = harness_path(harness_dir).parent
    existing = base / "research.yaml"
    if existing.is_file():
        return existing.read_text(encoding="utf-8")
    spec = load_spec(base)
    preferred = ["system_prompt", "loop.max_turns", "memory.entries"]
    levers = [lever for lever in preferred
              if any(lever == m or lever.startswith(m + ".") for m in spec.evolution.mutable)]
    tools = {}
    for entry in classify(spec):
        if entry.kind == "mcp":
            continue
        if entry.default is None:
            tools[entry.name] = "replay"
    evals = sorted(p.name for p in base.glob("*.yaml") if p.name.startswith("eval"))
    lines = [
        "# A research charter: what the program is for and what it may touch.",
        "goal: <what better means for this harness, in one sentence>",
        f"eval: {evals[0] if evals else 'eval.yaml'}   # an eval in this folder with 'harness: .'",
        "holdout: 0.25            # sealed cases, read once at the end",
        "levers:",
        *[f"  - {lever}" for lever in levers or ["system_prompt"]],
        "budget:",
        "  usd: 1.0",
        "  rounds: 4",
        "stop:",
        "  no_progress_rounds: 2",
    ]
    if tools or spec.mcp_servers:
        lines += ["execution:", "  tools:"]
        lines += [f"    {tool}: {mode}   # allow | replay | deny: review this"
                  for tool, mode in tools.items()] or ["    {}"]
        if spec.mcp_servers:
            lines += ["  mcp:"] + [f"    {server.name}: replay   # allow | replay | deny"
                                   for server in spec.mcp_servers]
    lines += ["models:", f"  director: {_DEFAULT_DIRECTOR}", ""]
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Autonomous evolve: `hiveloom evolve --research`
# --------------------------------------------------------------------------- #
#: What an automatic charter lets the director change when evolution allows it.
AUTO_LEVERS = ("system_prompt", "loop.max_turns", "memory.entries")


def director_selector(model: str | None) -> str:
    """A provider/model selector for the director; a bare id is a Claude model."""
    from hiveloom.generate.llm import DEFAULT_STRONG_MODEL

    model = (model or "").strip() or DEFAULT_STRONG_MODEL
    return model if "/" in model else f"claude/{model}"


def auto_charter(harness_dir: str | Path, *, eval_file: str | None = None,
                 director: str | None = None, budget: float | None = None,
                 rounds: int | None = None, tools: dict[str, str] | None = None) -> dict:
    """A charter for an unattended program, built from the harness itself."""
    base = harness_path(harness_dir).parent
    spec = load_spec(base)
    levers = [lever for lever in AUTO_LEVERS
              if any(lever == m or lever.startswith(m + ".") for m in spec.evolution.mutable)
              and not any(lever == f or lever.startswith(f + ".") for f in spec.evolution.frozen)]
    if not levers:
        raise ValueError("none of system_prompt, loop.max_turns or memory.entries is in this "
                         "harness's evolution.mutable; write a research.yaml naming the levers")
    charter: dict[str, Any] = {
        "goal": (spec.description or f"Improve {spec.name}").strip(),
        "eval": eval_file or "eval.yaml",
        "levers": levers,
        "budget": {"usd": float(budget) if budget else 1.0,
                   **({"rounds": int(rounds)} if rounds else {})},
        "stop": {"no_progress_rounds": 2},
        "models": {"director": director_selector(director)},
    }
    if tools:
        charter["execution"] = {"tools": dict(tools)}
    return charter


def charter_source(base: Path, *, charter_path: str | Path | None = None,
                   eval_file: str | None = None, director: str | None = None,
                   budget: float | None = None, rounds: int | None = None,
                   tools: dict[str, str] | None = None) -> Any:
    """The charter a new program starts from, with the caller's overrides on top.

    An explicit charter file wins; else the harness's research.yaml; else one built
    from the harness. Overrides (director, budget, rounds, tools, eval) apply on top
    of whichever it is, so asking for a bigger budget never discards the tool
    classifications the harness's own charter made.
    """
    path = Path(charter_path) if charter_path is not None else base / "research.yaml"
    if charter_path is not None or path.is_file():
        charter = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        return auto_charter(base, eval_file=eval_file, director=director, budget=budget,
                            rounds=rounds, tools=tools)
    if director:
        charter.setdefault("models", {})["director"] = director_selector(director)
    if budget:
        charter.setdefault("budget", {})["usd"] = float(budget)
    if rounds:
        charter.setdefault("budget", {})["rounds"] = int(rounds)
    if tools:
        charter.setdefault("execution", {}).setdefault("tools", {}).update(tools)
    if eval_file:
        charter["eval"] = eval_file
    return charter


def autonomous(harness_dir: str | Path, *, program: str | None = None,
               charter_path: str | Path | None = None, eval_file: str | None = None,
               director: str | None = None, budget: float | None = None,
               rounds: int | None = None, tools: dict[str, str] | None = None,
               apply: bool = False, approve_trust=None, until: str = "done") -> dict[str, Any]:
    """Run (or resume) an unattended research program on a harness, to a stop.

    The program never touches the live harness; its kept changes are queued as
    one proposal. With ``apply``, that proposal is applied at the end when its
    evidence is confirmed or supported — the same trust ``evolve --yes`` puts in
    a measured change — and left pending otherwise.
    """
    from datetime import datetime

    from hiveloom.errors import SpecError

    base = harness_path(harness_dir).parent
    name = program or f"evolve-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if program_root(base, name).exists():
        resumed = True
        if any([charter_path, eval_file, director, budget, rounds, tools]):
            raise SpecError(f"program '{name}' exists; resuming it uses its own charter "
                            "(drop the charter options, or pick a new --program)")
    else:
        resumed = False
        source = charter_source(base, charter_path=charter_path, eval_file=eval_file,
                                director=director, budget=budget, rounds=rounds, tools=tools)
        try:
            init_program(base, name, source, approve_trust=approve_trust)
        except SpecError as exc:
            if isinstance(source, dict) and "execution.tools." in str(exc):
                raise SpecError(f"{exc}\n  (with evolve --research, classify each with "
                                "--tool NAME=allow|replay|deny)") from exc
            raise
    engine = Engine(Program(base, name))
    if engine.program.load_state()["unit"] == "done":
        raise SpecError(f"program '{name}' has already finished; see its report, or start a "
                        "new one without --program")
    steps = engine.run(until=until)
    status = engine.status()
    applied = None
    promotion = status.get("promotion")
    if apply and promotion and promotion["status"] == "pending" \
            and promotion["strength"] in ("confirmed", "supported"):
        from hiveloom.evolve import proposals as proposals_mod
        from hiveloom.logging.hive import Hive

        with Hive() as hive:
            result = proposals_mod.apply_proposal_by_id(hive, base, promotion["proposal_id"])
        applied = {"changed": result.changed, "new_version_hash": result.new_version_hash}
        engine.program.ledger.append("promotion_applied", proposal_id=promotion["proposal_id"],
                                     new_version_hash=result.new_version_hash)
    report = engine.program.root / "report.md"
    return {
        "program": name,
        "resumed": resumed,
        "steps": [step.get("unit") for step in steps],
        "status": status,
        "applied": applied,
        "report": str(report) if report.exists() else None,
    }


def launch(harness_dir: str | Path, *, program: str | None = None,
           director: str | None = None, budget: float | None = None,
           rounds: int | None = None, tools: dict[str, str] | None = None,
           charter_path: str | Path | None = None) -> dict[str, Any]:
    """Create a program (from research.yaml, a charter, or the harness) and run it in
    the background — for interfaces that must answer at once (the copilot, the UI)."""
    from datetime import datetime

    base = harness_path(harness_dir).parent
    name = program or f"evolve-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if not program_root(base, name).exists():
        init_program(base, name, charter_source(base, charter_path=charter_path,
                                                director=director, budget=budget,
                                                rounds=rounds, tools=tools))
    started = start(base, name, "done")
    return {"program": name, "job": started, **detail(base, name)}


def overview(harness_dir: str | Path) -> dict[str, Any]:
    """What needs the user now, across a harness's programs: gates, questions, results."""
    rows = programs(harness_dir)
    active = [row for row in rows if row["status"] != "done"]
    focus = (active or rows)[-1]["name"] if rows else None
    summary: dict[str, Any] = {"programs": rows, "focus": focus}
    if focus:
        d = detail(harness_dir, focus)
        summary["program"] = {
            key: d.get(key) for key in (
                "name", "status", "unit", "round", "goal", "stop_reason", "confirmation",
                "promotion", "budget", "awaiting", "blocked_reason", "trust", "job")
        }
        summary["program"]["experiments"] = [
            {k: e.get(k) for k in ("id", "verdict", "kept", "stopped_early", "success")}
            for e in d["experiments"]
        ]
        if d.get("draft_contract"):
            summary["program"]["contract_to_approve"] = {
                "criteria": d["draft_contract"]["criteria"],
                "sample_cases": d.get("sample_cases"),
            }
        summary["program"]["open_questions"] = [
            {k: q.get(k) for k in ("id", "kind", "text", "criterion", "options", "request",
                                   "output", "judges")}
            for q in d.get("question_list") or [] if q["status"] == "open"
        ]
    return summary


def attention(harness_dir: str | Path) -> dict[str, int]:
    """What a harness's programs need from the person, cheaply (no engine is built)."""
    import json

    from .questions import Question

    counts = {"running": 0, "awaiting": 0, "questions": 0, "blocked": 0}
    for row in programs(harness_dir):
        counts["running"] += int(bool(row.get("running")))
        counts["awaiting"] += int(row.get("status") == "awaiting_user")
        counts["blocked"] += int(row.get("status") == "blocked")
        if row.get("status") == "done":
            continue
        path = program_root(harness_dir, row["name"]) / "questions.jsonl"
        if path.exists():
            latest: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    question = Question.model_validate(json.loads(line))
                    latest[question.id] = question.status
            counts["questions"] += sum(status == "open" for status in latest.values())
    return counts


def charter_form(harness_dir: str | Path) -> dict[str, Any]:
    """What a charter form offers for this harness, so nobody writes YAML to start."""
    base = harness_path(harness_dir).parent
    spec = load_spec(base)
    frozen = list(spec.evolution.frozen)
    levers = []
    for path in spec.evolution.mutable:
        if path == "delegation" or any(path == f or path.startswith(f + ".") for f in frozen):
            continue
        levers.append({"path": path, "default": path in AUTO_LEVERS})
    evals = []
    for path in sorted(base.glob("eval*.yaml")):
        try:
            harness = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("harness")
        except yaml.YAMLError:
            continue
        if harness in (".", "./"):
            evals.append(path.name)
    tools = [{"name": c.name, "kind": c.kind, "tags": c.tags, "default": c.default}
             for c in classify(spec)]
    existing = base / "research.yaml"
    director = None
    if existing.is_file():
        director = ((yaml.safe_load(existing.read_text(encoding="utf-8")) or {})
                    .get("models") or {}).get("director")
    return {
        "goal": (spec.description or "").strip(),
        "evals": evals,
        "levers": levers,
        "tools": tools,
        "director": director or director_selector(None),
        "defaults": {"budget": 1.0, "rounds": 4, "holdout": 0.25},
    }
