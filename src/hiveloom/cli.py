"""The ``hiveloom`` CLI — designed to be driven by an agent.

Design contract (build spec section 6):

* Every command supports ``--json`` with a stable output shape.
* Every mutating command validates the full spec after applying and rolls back
  on error, so a harness dir is never left invalid.
* Exit codes: 0 ok, 1 verify failed, 2 guardrail halt, 3 spec/validation error,
  4 runtime error.

Milestone M1 implements the explore (``schema``/``catalog``/``explain``/
``validate``) and construct (``init``/``set``/``add``/``remove``) commands.
``run``/``trace``/``stats``/``evolve``/``generate``/``package`` arrive later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from hiveloom import catalog as catalog_mod
from hiveloom import construct
from hiveloom.errors import ExitCode, HiveloomError, ProposalQueueError, SpecError
from hiveloom.spec import annotate
from hiveloom.spec.loader import validate_harness

_RUN_STATUS_EXIT = {
    "success": ExitCode.OK,
    "verify_failed": ExitCode.VERIFY_FAILED,
    "guardrail_halt": ExitCode.GUARDRAIL_HALT,
    "max_turns": ExitCode.RUNTIME_ERROR,
    "error": ExitCode.RUNTIME_ERROR,
}

app = typer.Typer(
    name="hiveloom",
    help="Generate, run, and evolve agent harnesses on the fly.",
    no_args_is_help=True,
    add_completion=False,
)
add_app = typer.Typer(help="Add a tool, validator, guardrail, hook, or skill to a harness.")
app.add_typer(add_app, name="add")
proposals_app = typer.Typer(help="Review, apply, or reject queued evolution proposals.")
app.add_typer(proposals_app, name="proposals")

_console = Console()
_err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _emit_json(payload: dict[str, Any]) -> None:
    _console.print_json(json.dumps(payload))


def _fail(message: str, json_output: bool, code: int) -> None:
    if json_output:
        _console.print_json(json.dumps({"ok": False, "error": message}))
    else:
        _err_console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code)


def _guard(json_output: bool):
    """Return a context manager that turns hiveloom errors into clean exits."""

    class _Guard:
        def __enter__(self) -> _Guard:
            return self

        def __exit__(self, exc_type, exc, _tb) -> bool:
            if exc is None:
                return False
            # Commands deliberately use typer.Exit for their documented
            # status; it is not an untranslated runtime failure.
            if isinstance(exc, typer.Exit):
                return False
            if isinstance(exc, SpecError):
                _fail(str(exc), json_output, ExitCode.SPEC_ERROR)
            if isinstance(exc, HiveloomError):
                _fail(str(exc), json_output, ExitCode.RUNTIME_ERROR)
            if isinstance(exc, (KeyError, ValueError)):
                _fail(str(exc).strip("'\""), json_output, ExitCode.SPEC_ERROR)
            # A command must never leak a traceback or accidentally use the
            # verify-failed exit code for an untranslated runtime failure.
            message = str(exc) or type(exc).__name__
            _fail(message, json_output, ExitCode.RUNTIME_ERROR)

    return _Guard()


# --------------------------------------------------------------------------- #
# Explore commands
# --------------------------------------------------------------------------- #
@app.command()
def schema(
    annotated: bool = typer.Option(
        False, "--annotated", help="Emit an annotated YAML template instead of JSON schema."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the JSON schema."),
) -> None:
    """Emit the harness contract.

    Default/``--json``: the JSON schema. ``--annotated``: a commented YAML
    template (a valid spec with placeholders). This is how an external LLM
    learns the contract. Next: run ``init`` then ``add``/``set``.
    """
    if annotated:
        # Raw write (no rich soft-wrapping, which would corrupt the YAML).
        typer.echo(annotate.annotated_template(), nl=False)
        return
    _emit_json(annotate.json_schema())


@app.command()
def catalog(
    kind: str = typer.Argument(
        ..., help="One of: tools, guardrails, validators, policies, compaction, hooks."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List catalog entries of ``kind`` (builtins + registered extensions).

    Use this before ``add`` to learn valid ``--builtin`` names.
    """
    from hiveloom import ext

    ext.ensure_environment_loaded()
    entries = catalog_mod.CATALOGS.get(kind)
    if entries is None:
        valid = ", ".join(sorted(catalog_mod.CATALOGS))
        _fail(f"unknown catalog kind '{kind}' (valid: {valid})", json_output, ExitCode.SPEC_ERROR)
        return

    if json_output:
        payload = {"ok": True, "kind": kind, "entries": [e.model_dump() for e in entries.values()]}
        _emit_json(payload)
        return

    table = Table(title=f"catalog: {kind}")
    table.add_column("name", style="bold cyan")
    table.add_column("description")
    table.add_column("tags", style="green")
    table.add_column("params", style="yellow")
    table.add_column("source", style="dim")
    for entry in entries.values():
        params = ", ".join(
            f"{p.name}:{p.type}{'*' if p.required else ''}" for p in entry.params
        ) or "-"
        table.add_row(
            entry.name, entry.description, ", ".join(entry.tags) or "-", params, entry.source
        )
    _console.print(table)


@app.command()
def explain(
    path: str = typer.Argument(..., help="Dotted spec path, e.g. context.compaction."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show field-level docs for a spec path (type, default, description, choices)."""
    with _guard(json_output):
        info = annotate.explain(path)
        if json_output:
            _emit_json({"ok": True, **info})
            return
        _console.print(f"[bold cyan]{info['path']}[/bold cyan]  ([yellow]{info['type']}[/yellow])")
        if info["description"]:
            _console.print(info["description"])
        _console.print(f"required: {info['required']}   default: {info['default']!r}")
        if "choices" in info:
            _console.print(f"choices: {info['choices']}")
        if "fields" in info:
            _console.print("fields:")
            for name, desc in info["fields"].items():
                _console.print(f"  - [cyan]{name}[/cyan]: {desc}")


@app.command()
def extensions(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List loaded extensions: packs, user extensions, providers, and load errors.

    Sources are installed packages with a ``hiveloom.extensions`` entry point,
    ``~/.hiveloom/extensions/*.py``, and ``~/.hiveloom/models.yaml``. A broken
    extension never crashes the CLI — its error is reported here.
    """
    from hiveloom import ext

    info = ext.status()
    if json_output:
        _emit_json({"ok": True, **info})
        return

    _console.print(f"providers: {', '.join(info['providers'])}")
    if info["sources"]:
        table = Table(title="loaded extensions")
        table.add_column("source", style="bold cyan")
        table.add_column("registered")
        for src in info["sources"]:
            regs = ", ".join(f"{r['kind']}:{r['name']}" for r in src["registered"]) or "-"
            table.add_row(src["source"], regs)
        _console.print(table)
    else:
        _console.print("no extensions loaded (builtins only)")
    for err in info["errors"]:
        _err_console.print(f"[red]load error[/red] {err['source']}: {err['error']}")


@app.command()
def validate(
    harness_dir: str = typer.Argument(".", help="Harness directory to validate."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Full validation: spec structure + code-hook import & signatures.

    Run this after constructing a harness. Exit 3 on any problem.
    """
    from hiveloom import trust as trust_mod

    with _guard(json_output):
        trust_mod.ensure_trusted(harness_dir, _trust_prompt(json_output))
        spec = validate_harness(harness_dir)
        if json_output:
            _emit_json({"ok": True, "name": spec.name, "message": "harness is valid"})
        else:
            _console.print(f"[green]valid[/green] — {spec.name}")


# --------------------------------------------------------------------------- #
# Construct commands
# --------------------------------------------------------------------------- #
@app.command()
def init(
    directory: str = typer.Argument(..., help="Directory to create the harness in."),
    name: str = typer.Option(..., "--name", help="Harness name."),
    task: str = typer.Option(..., "--task", help="One-line task description."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Create a minimal, valid harness skeleton.

    Next: inspect ``catalog`` and ``schema --annotated``, then ``add``/``set``.
    """
    with _guard(json_output):
        spec = construct.init_harness(directory, name=name, task=task)
        if json_output:
            _emit_json({"ok": True, "directory": directory, "name": spec.name})
        else:
            _console.print(f"[green]initialized[/green] harness '{spec.name}' in {directory}")


@app.command("set")
def set_cmd(
    path: str = typer.Argument(..., help="Dotted field path, e.g. loop.max_turns."),
    value: str | None = typer.Argument(None, help="Value (parsed as a YAML scalar)."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    file: str | None = typer.Option(None, "--file", help="Read the value from this file."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Set a scalar/object field. Validated and rolled back on error.

    Examples: ``hiveloom set loop.max_turns 30`` /
    ``hiveloom set system_prompt --file prompt.txt``.
    """
    with _guard(json_output):
        if value is None and file is None:
            raise SpecError("provide a VALUE argument or --file")
        construct.set_field(directory, path, value=value, file=file)
        if json_output:
            _emit_json({"ok": True, "path": path})
        else:
            _console.print(f"[green]set[/green] {path}")


@add_app.command("tool")
def add_tool_cmd(
    builtin: str | None = typer.Option(None, "--builtin", help="Builtin tool name."),
    code: str | None = typer.Option(None, "--code", help="Code hook path.py:function."),
    description: str | None = typer.Option(None, "--description", help="Tool description."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a tool. ``--code`` scaffolds a stub file if it does not exist."""
    with _guard(json_output):
        construct.add_tool(directory, builtin=builtin, code=code, description=description)
        _added(json_output, "tool", builtin or code)


@add_app.command("validator")
def add_validator_cmd(
    builtin: str | None = typer.Option(None, "--builtin", help="Builtin validator name."),
    code: str | None = typer.Option(None, "--code", help="Code hook path.py:function."),
    description: str | None = typer.Option(None, "--description", help="Optional note."),
    schema_file: str | None = typer.Option(None, "--schema-file", help="For output_schema."),
    pattern: str | None = typer.Option(None, "--pattern", help="For regex_match."),
    path: str | None = typer.Option(None, "--path", help="For file_exists."),
    command: str | None = typer.Option(None, "--command", help="For command_succeeds."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a verifier. ``--code`` scaffolds a stub file if it does not exist."""
    with _guard(json_output):
        construct.add_validator(
            directory,
            builtin=builtin,
            code=code,
            description=description,
            schema_file=schema_file,
            pattern=pattern,
            path=path,
            command=command,
        )
        _added(json_output, "validator", builtin or code)


@add_app.command("guardrail")
def add_guardrail_cmd(
    builtin: str = typer.Option(..., "--builtin", help="Builtin guardrail name."),
    value: str | None = typer.Option(None, "--value", help="Value (YAML scalar)."),
    pattern: str | None = typer.Option(None, "--pattern", help="For regex_output_filter."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a builtin guardrail. Guardrails are frozen from evolution by design."""
    import yaml

    with _guard(json_output):
        parsed_value = yaml.safe_load(value) if value is not None else None
        before = construct.find_guardrails(directory, builtin)
        construct.add_guardrail(directory, builtin=builtin, value=parsed_value, pattern=pattern)
        after = construct.find_guardrails(directory, builtin)
        if len(after) > len(before):
            _added(json_output, "guardrail", builtin)
        else:
            _replaced(json_output, "guardrail", builtin, before, after[0])


@add_app.command("hook")
def add_hook_cmd(
    on: str = typer.Option(..., "--on", help="Lifecycle event (see hiveloom.events.EVENTS)."),
    builtin: str | None = typer.Option(None, "--builtin", help="Registered hook name."),
    code: str | None = typer.Option(None, "--code", help="Code hook path.py:function."),
    description: str | None = typer.Option(None, "--description", help="Optional note."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Attach an event hook. ``--code`` scaffolds a stub file if it does not exist."""
    with _guard(json_output):
        construct.add_hook(directory, on=on, builtin=builtin, code=code, description=description)
        _added(json_output, "hook", f"{on}:{builtin or code}")


@add_app.command("skill")
def add_skill_cmd(
    name: str = typer.Argument(..., help="Skill name (becomes skills/<name>/SKILL.md)."),
    description: str = typer.Option(..., "--description", help="One-line skill description."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a skill: scaffolds SKILL.md and lists it in the spec.

    Only the name + description enter the system prompt; the model reads the
    full skill on demand (pair with the file_read tool).
    """
    with _guard(json_output):
        construct.add_skill(directory, name=name, description=description)
        _added(json_output, "skill", name)


@app.command()
def remove(
    target: str = typer.Argument(..., help="Builtin name, code ref, or dotted field path."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Remove a tool/guardrail/validator by identifier, or delete a field path."""
    with _guard(json_output):
        construct.remove_item(directory, target)
        if json_output:
            _emit_json({"ok": True, "removed": target})
        else:
            _console.print(f"[green]removed[/green] {target}")


def _trust_prompt(json_output: bool):
    """An interactive trust-approval callback, or None in non-interactive modes."""
    if json_output:
        return None

    def approve(path: str) -> bool:
        _console.print(
            f"[yellow]untrusted harness[/yellow] {path}\n"
            "Its code hooks will run with your permissions."
        )
        return typer.confirm("Trust this harness folder?", default=False)

    return approve


@app.command()
def trust(
    harness_dir: str = typer.Argument(..., help="Harness directory to trust."),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke trust instead."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Trust (or revoke trust in) a harness folder.

    Harnesses built on this machine are trusted automatically; foreign folders
    (unzipped artifacts, clones) need trust before their code hooks may run.
    CI alternative: HIVELOOM_TRUST=always|never.
    """
    from hiveloom import trust as trust_mod

    with _guard(json_output):
        if revoke:
            removed = trust_mod.revoke_trust(harness_dir)
            verb = "revoked" if removed else "was not trusted"
        else:
            trust_mod.record_trust(harness_dir)
            verb = "trusted"
        if json_output:
            _emit_json({"ok": True, "directory": harness_dir, "action": verb})
        else:
            _console.print(f"[green]{verb}[/green] {harness_dir}")


@app.command()
def run(
    harness_dir: str = typer.Argument(".", help="Harness directory to run."),
    input_value: str = typer.Option(..., "--input", help="Input FILE path or literal TEXT."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Assemble the first model call and print it; no API use."
    ),
    stream: bool = typer.Option(
        False, "--stream", help="Stream every trace event to stdout as JSONL (result last)."
    ),
    approve: bool = typer.Option(
        False, "--approve", "-a", help="Trust the harness folder without prompting."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Run a harness on an input.

    ``--dry-run`` resolves hooks and prints the would-be first model call without
    touching the API. ``--stream`` emits each trace event as a JSON line while
    the run progresses — the embedding interface for other programs. Exit codes:
    0 success, 1 verify failed, 2 guardrail halt, 4 runtime error.
    """
    from hiveloom import runner
    from hiveloom import trust as trust_mod

    with _guard(json_output):
        if approve:
            trust_mod.record_trust(harness_dir)
        if dry_run:
            info = runner.dry_run(
                harness_dir, input_value, approve_trust=_trust_prompt(json_output)
            )
            if json_output:
                _emit_json({"ok": True, "dry_run": True, **info})
            else:
                _console.print(f"[bold]dry run[/bold] — {info['name']} ({info['model']})")
                _console.print(f"system:\n{info['system']}")
                _console.print(f"tools: {[t['name'] for t in info['tools']]}")
                _console.print(f"first message: {info['messages'][0]['content']}")
            return

        on_event = None
        if stream:
            def on_event(event) -> None:
                typer.echo(event.model_dump_json())

        result = runner.run_harness(
            harness_dir,
            input_value,
            on_event=on_event,
            approve_trust=_trust_prompt(json_output or stream),
        )
        payload = {
            "ok": result.status == "success",
            "status": result.status,
            "output": result.output,
            "turns": result.turns,
            "cost_usd": result.cost_usd,
            "duration_seconds": result.duration_seconds,
            "run_id": result.run_id,
            "trace_path": result.trace_path,
            "reason": result.reason,
        }
        if stream:
            typer.echo(json.dumps({"type": "run_result", **payload}))
        elif json_output:
            _emit_json(payload)
        else:
            colour = "green" if result.status == "success" else "yellow"
            _console.print(f"[{colour}]{result.status}[/{colour}] — {result.run_id}")
            if result.output:
                _console.print(result.output)
            if result.reason:
                _console.print(f"reason: {result.reason}")
        raise typer.Exit(_RUN_STATUS_EXIT.get(result.status, ExitCode.RUNTIME_ERROR))


@app.command()
def trace(
    run_id: str = typer.Argument(..., help="Run id to display (e.g. run_abc123)."),
    directory: str | None = typer.Option(
        None, "--dir", "-d", help="Harness dir to ingest first if the run is unknown."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a run's trace: its summary and ordered events.

    Runs are ingested automatically by ``run``; pass ``--dir`` to ingest a
    harness's in-folder traces first (e.g. for a copied-back deployment).
    """
    import json as _json

    from hiveloom import runner
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            if directory is not None:
                runner.resolve_and_ingest(directory, hive)
            run = hive.get_run(run_id)
        if run is None:
            _fail(f"run '{run_id}' not found in the Hive", json_output, ExitCode.SPEC_ERROR)
            return

        events: list[dict[str, Any]] = []
        trace_file = Path(run.get("trace_path", ""))
        if trace_file.exists():
            events = [
                _json.loads(line)
                for line in trace_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        if json_output:
            _emit_json({"ok": True, "run": run, "events": events})
            return
        _console.print(
            f"[bold]{run['run_id']}[/bold] — {run['harness_name']} "
            f"@ {run['harness_version_hash']}  [{_status_colour(run['status'])}]"
            f"{run['status']}[/{_status_colour(run['status'])}]"
        )
        _console.print(
            f"turns={run['turns']}  cost=${run['cost_usd']:.4f}  "
            f"duration={run['duration_seconds']:.2f}s"
        )
        if run.get("reason"):
            _console.print(f"reason: {run['reason']}")
        for event in events:
            _console.print(f"  [dim]{event['seq']:>3}[/dim] {event['type']}")


@app.command()
def stats(
    target: str = typer.Argument(..., help="Harness name or harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show Hive stats for a harness: success rate, cost, and turns per version.

    A harness directory is ingested on the fly (idempotent by run id) so stats
    reflect its in-folder traces even after being copied back from production.
    """
    from hiveloom import runner
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            name = runner.resolve_and_ingest(target, hive)
            summary = hive.summary(name)
            recent = hive.recent_failures(name, 5)

        if json_output:
            _emit_json({"ok": True, **summary, "recent_failures": recent})
            return

        _console.print(
            f"[bold]{summary['harness_name']}[/bold] — {summary['total_runs']} runs, "
            f"success rate {summary['success_rate']:.0%}, "
            f"avg cost ${summary['avg_cost_usd']:.4f}, avg turns {summary['avg_turns']:.1f}"
        )
        if summary["versions"]:
            table = Table(title="per version hash")
            table.add_column("version", style="cyan")
            table.add_column("runs", justify="right")
            table.add_column("success", justify="right", style="green")
            table.add_column("avg cost", justify="right")
            table.add_column("avg turns", justify="right")
            for v in summary["versions"]:
                table.add_row(
                    v["version"],
                    str(v["runs"]),
                    f"{v['success_rate']:.0%}",
                    f"${v['avg_cost_usd']:.4f}",
                    f"{v['avg_turns']:.1f}",
                )
            _console.print(table)
        sigs = summary["failure_signatures"]
        if sigs["verdicts"]:
            _console.print("[yellow]top failure verdicts:[/yellow]")
            for v in sigs["verdicts"]:
                _console.print(f"  {v['count']}× {v['feedback']}")
        if sigs["guardrails"]:
            _console.print("[yellow]top guardrail triggers:[/yellow]")
            for g in sigs["guardrails"]:
                _console.print(f"  {g['count']}× {g['guardrail']} ({g['kind']})")


def _status_colour(status: str) -> str:
    return "green" if status == "success" else "yellow"


def _strong_model(model_id: str | None, base: Path | None = None):
    """Build the strong generation/evolution model.

    ``--model provider/model-id`` (e.g. ``ollama/qwen3:32b``) routes through the
    provider registry when the prefix names a registered provider; anything else
    is a Claude model id.
    """
    import os

    from hiveloom import ext
    from hiveloom.generate.llm import (
        DEFAULT_STRONG_MODEL,
        ClaudeStrongModel,
        ProviderStrongModel,
    )

    if model_id and "/" in model_id:
        prefix, rest = model_id.split("/", 1)
        if prefix in ext.provider_names():
            return ProviderStrongModel(ext.build_provider(prefix, base), rest)

    if base is not None and (base / ".env").exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(base / ".env")
        except ImportError:  # pragma: no cover
            pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SpecError("ANTHROPIC_API_KEY is not set (needed for generate/evolve).")
    return ClaudeStrongModel(model_id or DEFAULT_STRONG_MODEL)


@app.command()
def generate(
    task: str = typer.Argument(..., help="Task description to build a harness for."),
    output: str = typer.Option(..., "--output", "-o", help="Directory to create the harness in."),
    model_id: str | None = typer.Option(
        None, "--model", help="Strong model id (or provider/model-id) for generation."
    ),
    blueprint: str | None = typer.Option(
        None,
        "--blueprint",
        help="Named house-style prompt fragment (~/.hiveloom/blueprints/<name>.md or a pack).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Generate a harness for a task (a strong model drives the construction API).

    Sugar over the construct commands: explore → init → add/set → validate, with
    a validate/repair loop. Needs ANTHROPIC_API_KEY.
    """
    from hiveloom.generate.generator import generate as run_generate

    with _guard(json_output):
        model = _strong_model(model_id)
        spec = run_generate(task, output, model, blueprint=blueprint)
        if json_output:
            _emit_json({"ok": True, "directory": output, "name": spec.name})
        else:
            _console.print(f"[green]generated[/green] harness '{spec.name}' in {output}")


@app.command()
def evolve(
    harness_dir: str = typer.Argument(..., help="Harness directory to evolve."),
    yes: bool = typer.Option(False, "--yes", help="Auto-apply YAML changes (never code)."),
    model_id: str | None = typer.Option(None, "--model", help="Strong model id for proposals."),
    propose: bool = typer.Option(
        False,
        "--propose",
        help="Queue the gated proposal for later review instead of applying it. "
        "--yes is ignored.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Analyze Hive failures and propose a gated harness mutation.

    Guardrails/model/logging.redact can never be changed. YAML changes within the
    mutable set auto-apply with ``--yes``; regenerated code always needs y/n
    approval. Recorded in the Hive under a new version hash. With ``--propose``,
    the gated proposal is queued (see ``hiveloom proposals``) instead of applied;
    a human reviews and applies it later via ``proposals apply``.
    """
    from hiveloom import evolve as evolve_mod
    from hiveloom import runner
    from hiveloom import trust as trust_mod
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        trust_mod.ensure_trusted(harness_dir, _trust_prompt(json_output))
        base = Path(harness_dir)
        model = _strong_model(model_id, base if base.is_dir() else base.parent)
        with Hive() as hive:
            name = runner.resolve_and_ingest(harness_dir, hive)
            report = evolve_mod.analyze(hive, name)
            if report.is_empty():
                if json_output:
                    _emit_json(
                        {"ok": True, "changed": False, "reason": "no failures to learn from"}
                    )
                else:
                    _console.print("[green]nothing to evolve[/green] — no recorded failures")
                return

            spec = load_spec_for(harness_dir)
            if propose:
                record = proposals_mod.create_proposal(
                    hive, spec, harness_dir, report, model, trigger="manual"
                )
                _emit_proposal_created(record, json_output)
                return

            proposal = evolve_mod.propose(spec, report, model)
            yaml_diff = evolve_mod.preview_yaml_changes(harness_dir, proposal)

            if yaml_diff and not json_output:
                _console.print("[yellow]proposed YAML diff[/yellow]")
                _console.print(yaml_diff)
            apply_yaml = _confirm_apply_yaml(yes, json_output)
            result = evolve_mod.apply_proposal(
                harness_dir,
                proposal,
                hive=hive,
                approve_code=_make_approve_code(base, json_output=json_output),
                apply_yaml=apply_yaml,
            )

        if json_output:
            _emit_json({"ok": True, **result.model_dump()})
        else:
            if result.changed:
                _console.print(
                    f"[green]evolved[/green] to #{result.counter} "
                    f"({result.old_version_hash} -> {result.new_version_hash}): {result.rationale}"
                )
            else:
                _console.print("[yellow]no changes applied[/yellow]")
            _print_apply_leftovers(result)


def load_spec_for(harness_dir: str):
    from hiveloom.spec.loader import load_spec

    return load_spec(harness_dir)


# --------------------------------------------------------------------------- #
# Proposals queue
# --------------------------------------------------------------------------- #
def _proposal_payload(record: Any) -> dict[str, Any]:
    """Expand a ProposalRecord's JSON-text columns into nested objects for output."""
    payload = record.model_dump()
    payload["proposal"] = record.proposal.model_dump()
    payload["gate"] = record.gate.model_dump()
    payload["apply_result"] = record.apply_result
    return payload


def _emit_proposal_created(record: Any, json_output: bool) -> None:
    if json_output:
        _emit_json({"ok": True, **_proposal_payload(record)})
        return
    gate = record.gate
    _console.print(
        f"[green]queued[/green] proposal {record.id} — "
        f"{len(gate.accepted)} accepted, {len(gate.rejected)} rejected, "
        f"{len(gate.code_changes)} code change(s) pending review"
    )


def _confirm_apply_yaml(yes: bool, json_output: bool) -> bool:
    """Whether to apply gated YAML changes: ``--yes``, or an interactive y/n."""
    return yes or (
        not json_output and typer.confirm("Apply the proposed YAML changes?", default=False)
    )


def _print_apply_leftovers(result: Any) -> None:
    """Print an ApplyResult's rejected paths and any code changes still awaiting approval."""
    for rej in result.rejected:
        _console.print(f"  [red]rejected[/red] {rej['path']}: {rej['reason']}")
    for pending in result.pending_code:
        _console.print(f"  [dim]pending code approval[/dim] {pending}")


def _make_approve_code(
    harness_dir: Path, *, json_output: bool, allowlist: set[str] | None = None
) -> Any:
    """Build a code-change approval callback: auto-approve ``allowlist`` paths,
    else interactive y/n (never in ``--json`` mode) — the same flow ``evolve``
    and ``proposals apply`` both use.
    """
    from hiveloom.evolve import resolve_code_change_path

    def approve(change: Any) -> bool:
        if change.file in (allowlist or ()):
            return True
        if json_output:
            return False
        resolved = resolve_code_change_path(harness_dir, change.file)
        _console.print(f"[yellow]code change[/yellow] {resolved}: {change.rationale}")
        _console.print(change.source)
        return typer.confirm(f"Apply regenerated code to {resolved}?", default=False)

    return approve


@proposals_app.command("list")
def proposals_list_cmd(
    harness_dir: str = typer.Argument(..., help="Harness name or directory."),
    status: str | None = typer.Option(
        None, "--status", help="Filter by status: pending|applied|rejected."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List queued proposals for a harness, newest first."""
    from hiveloom import runner
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            name = runner.resolve_and_ingest(harness_dir, hive)
            records = proposals_mod.list_proposals(hive, harness_name=name, status=status)
        if json_output:
            _emit_json({"ok": True, "proposals": [_proposal_payload(r) for r in records]})
            return
        if not records:
            _console.print("[green]no proposals[/green]")
            return
        table = Table()
        table.add_column("id")
        table.add_column("status")
        table.add_column("trigger")
        table.add_column("rationale")
        table.add_column("created_at")
        for r in records:
            table.add_row(r.id, r.status, r.trigger, r.rationale, r.created_at)
        _console.print(table)


@proposals_app.command("show")
def proposals_show_cmd(
    harness_dir: str = typer.Argument(
        ..., help="Harness dir (kept for CLI-shape parity; ids are looked up directly)."
    ),
    proposal_id: str = typer.Argument(..., help="Proposal id (e.g. prop_abc123)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a queued proposal: its rationale, gate result, and any apply result."""
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            record = proposals_mod.get_proposal(hive, proposal_id)
        if record is None:
            raise ProposalQueueError(f"no proposal with id '{proposal_id}'")

        if json_output:
            _emit_json({"ok": True, **_proposal_payload(record)})
            return

        gate = record.gate
        _console.print(
            f"[bold]{record.id}[/bold] — {record.harness_name} @ {record.spec_version_hash} "
            f"[{record.status}] (trigger={record.trigger})"
        )
        _console.print(f"rationale: {record.rationale}")
        _console.print(
            f"gate: {len(gate.accepted)} accepted, {len(gate.rejected)} rejected, "
            f"{len(gate.code_changes)} code change(s)"
        )
        for rej in gate.rejected:
            _console.print(f"  [red]rejected[/red] {rej['path']}: {rej['reason']}")
        for change in gate.code_changes:
            _console.print(f"  [yellow]code change[/yellow] {change.file}: {change.rationale}")
        apply_result = record.apply_result
        if apply_result is not None:
            _console.print(f"resolved_at: {record.resolved_at}")
            _console.print(json.dumps(apply_result, indent=2))


@proposals_app.command("apply")
def proposals_apply_cmd(
    harness_dir: str = typer.Argument(..., help="Harness directory to apply into."),
    proposal_id: str = typer.Argument(..., help="Proposal id to apply."),
    yes: bool = typer.Option(False, "--yes", help="Auto-apply YAML changes (never code)."),
    approve_code_arg: str | None = typer.Option(
        None,
        "--approve-code",
        help="Comma-separated file paths to approve for code changes.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Apply a queued proposal.

    Re-derives the harness's version hash first; if it no longer matches what
    the proposal was drafted against, it fails without touching disk (the
    harness changed — regenerate). Review with ``proposals show`` first: YAML
    changes apply with ``--yes`` or interactive confirmation, same as
    ``evolve`` (asked only after the trust/existence/staleness checks above
    pass); code changes need per-file ``--approve-code`` or interactive y/n,
    fed from the proposal's stored gate result rather than a fresh propose.
    """
    from hiveloom import trust as trust_mod
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    approved_files = set(approve_code_arg.split(",")) if approve_code_arg else None
    approve = _make_approve_code(
        Path(harness_dir), json_output=json_output, allowlist=approved_files
    )

    with _guard(json_output):
        trust_mod.ensure_trusted(harness_dir, _trust_prompt(json_output))
        with Hive() as hive:
            result = proposals_mod.apply_proposal_by_id(
                hive,
                harness_dir,
                proposal_id,
                approve_code=approve,
                confirm_apply_yaml=lambda: _confirm_apply_yaml(yes, json_output),
            )
        if json_output:
            _emit_json({"ok": True, "proposal_id": proposal_id, **result.model_dump()})
        else:
            if result.changed:
                _console.print(
                    f"[green]applied[/green] proposal {proposal_id} — now #{result.counter} "
                    f"({result.old_version_hash} -> {result.new_version_hash})"
                )
            else:
                _console.print(f"[yellow]no changes applied[/yellow] for proposal {proposal_id}")
            _print_apply_leftovers(result)


@proposals_app.command("reject")
def proposals_reject_cmd(
    harness_dir: str = typer.Argument(
        ..., help="Harness directory (kept for CLI-shape parity; reject never touches it)."
    ),
    proposal_id: str = typer.Argument(..., help="Proposal id to reject."),
    reason: str = typer.Option("", "--reason", help="Why this proposal is being rejected."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Reject a queued proposal. Never touches harness.yaml."""
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            proposals_mod.reject_proposal(hive, proposal_id, reason)
        if json_output:
            _emit_json({"ok": True, "proposal_id": proposal_id, "status": "rejected"})
        else:
            _console.print(f"[yellow]rejected[/yellow] proposal {proposal_id}")


@app.command()
def package(
    harness_dir: str = typer.Argument(..., help="Harness directory to package."),
    docker: bool = typer.Option(False, "--docker", help="Also emit a Dockerfile."),
    runtime_wheel: str | None = typer.Option(
        None,
        "--runtime-wheel",
        help=(
            "Embed this hiveloom .whl in a Docker artifact "
            "(pre-release/private-index deployment)."
        ),
    ),
    output: str | None = typer.Option(None, "--output", "-o", help="Where to write the zip."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Package a harness into a portable <name>-<version_hash>.zip artifact.

    Validates the harness first. Excludes secrets (.env) and local run memory
    (.hiveloom/). ``--docker`` also emits a Dockerfile for a runnable container.
    """
    from hiveloom import trust as trust_mod
    from hiveloom.package import package_harness

    with _guard(json_output):
        trust_mod.ensure_trusted(harness_dir, _trust_prompt(json_output))
        result = package_harness(
            harness_dir,
            docker=docker,
            output_dir=output,
            runtime_wheel=runtime_wheel,
        )
        if json_output:
            _emit_json({"ok": True, **result})
        else:
            _console.print(
                f"[green]packaged[/green] {result['name']} @ {result['version_hash']} "
                f"→ {result['zip_path']} ({result['files']} files)"
            )
            if result["dockerfile"]:
                _console.print("  wrote Dockerfile")


def _added(json_output: bool, kind: str, ident: str | None) -> None:
    if json_output:
        _emit_json({"ok": True, "added": kind, "ref": ident})
    else:
        _console.print(f"[green]added[/green] {kind} {ident}")


def _replaced(
    json_output: bool,
    kind: str,
    ident: str | None,
    before: list[dict[str, Any]],
    after: dict[str, Any],
) -> None:
    """Report an add that superseded ``before`` (one or more existing entries)."""
    if json_output:
        _emit_json({"ok": True, "replaced": kind, "ref": ident, "before": before, "after": after})
    elif before == [after]:
        _console.print(f"[green]unchanged[/green] {kind} {ident} — already present")
    else:
        was = " and ".join(_terse(entry) for entry in before)
        _console.print(f"[green]replaced[/green] {kind} {ident} — was {was}")


def _terse(entry: dict[str, Any]) -> str:
    """Render a guardrail entry's params (everything but its name) for a message."""
    params = {k: v for k, v in entry.items() if k != "builtin"}
    return ", ".join(f"{k}={v}" for k, v in params.items()) or "no params"


if __name__ == "__main__":
    app()
