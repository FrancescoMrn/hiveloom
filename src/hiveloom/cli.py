"""The ``hiveloom`` CLI — designed to be driven by an agent.

Design contract (build spec section 6):

* Every command supports ``--json`` with a stable output shape.
* Every mutating command validates the full spec after applying and rolls back
  on error, so a harness dir is never left invalid.
* Exit codes: 0 ok, 1 verify failed, 2 guardrail halt, 3 spec/validation error,
  4 runtime error.

The same machine-readable and exit-code contracts apply across exploration,
construction, execution, evolution, packaging, and serving commands.
"""

from __future__ import annotations

import json
import os
import sys
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


@app.callback(invoke_without_command=True)
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        is_eager=True,
        help="Show the installed hiveloom version and exit.",
    ),
) -> None:
    """Generate, run, and evolve agent harnesses on the fly."""
    if version:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _version

        try:
            typer.echo(_version("hiveloom"))
        except PackageNotFoundError:  # a source tree with no installed dist
            typer.echo("unknown (hiveloom is not installed in this environment)")
        raise typer.Exit(ExitCode.OK)


add_app = typer.Typer(help="Add a tool, validator, guardrail, hook, or skill to a harness.")
app.add_typer(add_app, name="add")
proposals_app = typer.Typer(help="Review, apply, or reject queued evolution proposals.")
app.add_typer(proposals_app, name="proposals")
friction_app = typer.Typer(help="Query recovered retries and other indexed run friction.")
app.add_typer(friction_app, name="friction")
traces_app = typer.Typer(help="Manage raw trace files under a validated Hiveloom root.")
app.add_typer(traces_app, name="traces")
keys_app = typer.Typer(
    help="Ed25519 keys and bearer tokens for the (non-production) HTTP control plane."
)
app.add_typer(keys_app, name="keys")
mcp_app = typer.Typer(
    help="MCP integration: expose harnesses as MCP tools, or introspect declared servers."
)
app.add_typer(mcp_app, name="mcp")
registry_app = typer.Typer(
    help="The local harness registry: what `hiveloom mcp serve --registered` offers to agents."
)
app.add_typer(registry_app, name="registry")
metrics_app = typer.Typer(help="Record, import, and query numeric run metrics.")
app.add_typer(metrics_app, name="metrics")
eval_app = typer.Typer(help="Validate and run versioned local evaluations.")
app.add_typer(eval_app, name="eval")

_console = Console()
_err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _emit_json(payload: dict[str, Any]) -> None:
    # Machine-readable contract: plain bytes, never through rich (which
    # injects ANSI codes under FORCE_COLOR and breaks downstream json.loads).
    print(json.dumps(payload, indent=2))


def _fail(message: str, json_output: bool, code: int) -> None:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        _err_console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code)


def _optional_bool(value: str | None, option: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise SpecError(f"{option} must be true or false")


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
        ...,
        help=(
            "One of: tools, guardrails, validators, policies, compaction, hooks, "
            "datasets, scorers."
        ),
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
def models(
    name_or_action: str = typer.Argument(
        "", help="Provider to list, or 'probe' for a model capability probe."
    ),
    target: str = typer.Argument("", help="Harness path when the action is 'probe'."),
    model: str | None = typer.Option(None, "--model", help="Run-only model to probe."),
    probe_provider: str | None = typer.Option(
        None, "--provider", help="Run-only provider to probe."
    ),
    identity: str = typer.Option(
        "warn", "--identity", help="Identity policy: warn, exact, or alias."
    ),
    alias: list[str] = typer.Option([], "--alias", help="Accepted effective-model alias."),
    live: bool = typer.Option(
        False,
        "--live",
        help="Contact the provider for up to two possibly billed model calls.",
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore a valid cached probe."),
    require_compatible: bool = typer.Option(
        False,
        "--require-compatible",
        help="Return a validation error when identity is not accepted.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List model providers and their known models, pricing, and key status.

    Answers the two questions that block a first run: which provider name goes
    in ``model.provider``, and which environment variable holds its key. An
    ``open`` provider also accepts model ids not listed here (new releases,
    aggregator routes, whatever a local server is serving); a closed one does
    not, so a typo fails validation. Listing and declared probes are free.
    `models probe ... --live` explicitly opts into up to two possibly billed
    provider calls.
    """
    from hiveloom import ext

    if name_or_action == "probe":
        from hiveloom import trust as trust_mod
        from hiveloom.models.capabilities import (
            probe_model,
            probe_plan,
            require_compatible_probe,
        )
        from hiveloom.spec.loader import load_spec
        from hiveloom.spec.schema import ModelConfig

        with _guard(json_output):
            if not target:
                raise SpecError("models probe requires a harness path")
            if identity not in {"warn", "exact", "alias"}:
                raise SpecError("--identity must be warn, exact, or alias")
            harness_path = Path(target)
            yaml_path = harness_path / "harness.yaml" if harness_path.is_dir() else harness_path
            trust_mod.ensure_trusted(yaml_path.parent, _trust_prompt(json_output))
            spec = load_spec(yaml_path)
            resolved = ModelConfig(
                provider=probe_provider or spec.model.provider,
                id=model or spec.model.id,
                max_tokens=min(spec.model.max_tokens, 128),
                temperature=spec.model.temperature,
            )
            provider_instance = (
                ext.build_provider(resolved.provider, yaml_path.parent) if live else None
            )
            result = probe_model(
                resolved.provider,
                resolved.id,
                provider=provider_instance,
                live=live,
                policy=identity,
                aliases=alias,
                refresh=refresh,
            )
            if require_compatible:
                require_compatible_probe(result)
            payload = {
                "ok": True,
                "plan": probe_plan(live=live).model_dump(mode="json"),
                "probe": result.model_dump(mode="json"),
            }
            if json_output:
                _emit_json(payload)
            else:
                mode = "cached" if result.cached else "live" if result.live else "declared"
                _console.print(
                    f"[green]probe[/green] {resolved.provider}/{resolved.id} ({mode})\n"
                    f"identity: {result.identity.status} "
                    f"({'accepted' if result.identity.accepted else 'rejected'})\n"
                    f"{payload['plan']['note']}"
                )
        return

    if target:
        raise SpecError("a second argument is only valid for `hiveloom models probe`")
    if any((model, probe_provider, alias, live, refresh, require_compatible)) or identity != "warn":
        raise SpecError("probe options require `hiveloom models probe HARNESS`")

    provider = name_or_action

    entries = ext.providers()
    if provider:
        entries = [p for p in entries if p.name == provider]
        if not entries:
            raise SpecError(
                f"unknown model provider '{provider}' "
                f"(available: {', '.join(ext.provider_names())})"
            )

    payload = [
        {
            "name": p.name,
            "label": p.label,
            "api": p.api,
            "base_url": p.base_url,
            "api_key_env": p.api_key_env,
            # Whether the key is *present*, never its value — this output is
            # routinely piped into agents and issue reports.
            "api_key_set": bool(not p.api_key_env or os.environ.get(p.api_key_env)),
            "open_catalog": p.open_catalog,
            "source": p.source,
            "models": [m.model_dump() for m in ext.models_for_provider(p.name)],
        }
        for p in entries
    ]

    if json_output:
        _emit_json({"ok": True, "providers": payload})
        return

    table = Table(title="model providers")
    table.add_column("provider", style="bold cyan")
    table.add_column("key env")
    table.add_column("key", justify="center")
    table.add_column("catalog")
    table.add_column("models")
    for entry in payload:
        key_env = entry["api_key_env"] or "-"
        if not entry["api_key_env"]:
            key_state = "[dim]n/a[/dim]"
        elif entry["api_key_set"]:
            key_state = "[green]set[/green]"
        else:
            key_state = "[red]unset[/red]"
        listed = ", ".join(m["id"] for m in entry["models"]) or "-"
        table.add_row(
            entry["name"],
            key_env,
            key_state,
            "open" if entry["open_catalog"] else "fixed",
            listed,
        )
    _console.print(table)
    if provider and payload:
        entry = payload[0]
        _console.print(f"endpoint: {entry['base_url'] or 'native SDK'}")
        for model in entry["models"]:
            _console.print(
                f"  [cyan]{model['id']}[/cyan]  "
                f"${model['input_cost_per_mtok']}/${model['output_cost_per_mtok']} per Mtok"
            )
    _console.print(
        "[dim]Pricing is list price at release time, used for cost estimation and "
        "budget guardrails. Override in ~/.hiveloom/models.yaml.[/dim]"
    )


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
def guide(
    topic: str = typer.Argument(
        "agents", help="Topic to print: agents (default), all, or a skill name."
    ),
    list_topics: bool = typer.Option(False, "--list", help="List the topics instead."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Print the agent guidance that ships with the package.

    ``AGENTS.md`` and the lifecycle skills are packaged, so an agent that only
    ran ``pip install hiveloom`` can read the ground rules without a repository
    checkout. Free: this never touches the API.
    """
    from hiveloom import guide as guide_mod

    with _guard(json_output):
        if list_topics:
            topics = guide_mod.list_topics()
            if json_output:
                _emit_json({"ok": True, "topics": topics})
                return
            table = Table(title="guide topics")
            table.add_column("topic", style="bold cyan")
            table.add_column("covers")
            for entry in topics:
                table.add_row(entry["name"], entry["description"])
            _console.print(table)
            return

        text = guide_mod.read_topic(topic)
        if json_output:
            _emit_json({"ok": True, "topic": topic, "markdown": text})
            return
        # Raw markdown, not rendered: the reader is usually an agent piping it
        # into its own context, and rich would reflow and recolour the text.
        print(text)


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


@app.command()
def migrate(
    harness_dir: str = typer.Argument(".", help="Harness directory to migrate."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Atomically migrate legacy harness document fields.

    The current migration renames the document-format field from ``version``
    to ``schema_version``. Full validation runs before and after the write,
    with rollback on error. Harness behavior identity does not change.
    """
    from hiveloom.spec.migrate import migrate_harness

    with _guard(json_output):
        result = migrate_harness(
            harness_dir,
            approve_trust=_trust_prompt(json_output),
        )
        payload = {"ok": True, **result.model_dump(mode="json")}
        if json_output:
            _emit_json(payload)
        elif result.changed:
            _console.print(
                f"[green]migrated[/green] {result.from_field} -> {result.to_field} "
                f"(behavior {result.behavior_hash_after})"
            )
        else:
            _console.print(
                f"[green]already current[/green] — {result.to_field}: "
                f"{result.schema_version}"
            )


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
    ``hiveloom set system_prompt --file prompt.txt`` /
    ``hiveloom set model openai/gpt-4.1-mini``.

    ``set model provider/model-id`` is the way to switch labs: provider and id
    validate against each other, so they must change in one commit.
    """
    with _guard(json_output):
        if value is None and file is None:
            raise SpecError("provide a VALUE argument or --file")
        if path == "model" and file is None:
            spec = construct.set_model(directory, value)
            if json_output:
                _emit_json(
                    {"ok": True, "path": path,
                     "provider": spec.model.provider, "id": spec.model.id}
                )
            else:
                _console.print(
                    f"[green]set[/green] model {spec.model.provider}/{spec.model.id}"
                )
            return
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


@add_app.command("playbook")
def add_playbook_cmd(
    name: str = typer.Argument(..., help="Mode name; what switch_playbook takes."),
    description: str = typer.Option(
        ..., "--description", help="What this mode is for (selection guidance for the model)."
    ),
    prompt: str | None = typer.Option(
        None, "--prompt", help="Prompt fragment path (default: playbooks/<name>.md)."
    ),
    tools: str | None = typer.Option(
        None, "--tools", help="Comma-separated tool names active in this mode."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model id to execute with while this mode is active."
    ),
    model_provider: str | None = typer.Option(
        None, "--model-provider", help="Provider serving --model (default: the harness's)."
    ),
    on_enter: str | None = typer.Option(
        None, "--on-enter", help="Entry hook 'path.py:function' (scaffolded; frozen)."
    ),
    on_exit: str | None = typer.Option(
        None, "--on-exit", help="Exit hook 'path.py:function' (scaffolded; frozen)."
    ),
    entry: bool = typer.Option(False, "--entry", help="Start runs in this playbook."),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a playbook: a named mode the run can switch between.

    Scaffolds the prompt fragment (and any hooks) and lists the mode in the
    spec. Declaring any playbook auto-adds the ``switch_playbook`` tool.

    ``--tools`` narrows what the mode may use — that narrowing is what makes a
    mode a mode. ``--model`` gives it its own executor, so one harness can
    profile on a cheap model and decide on an expensive one. Both ``--model``
    and the hook options are frozen from evolution.
    """
    with _guard(json_output):
        construct.add_playbook(
            directory,
            name=name,
            description=description,
            prompt=prompt,
            tools=[t.strip() for t in tools.split(",") if t.strip()] if tools else None,
            model=model,
            model_provider=model_provider,
            on_enter=on_enter,
            on_exit=on_exit,
            entry=entry,
        )
        _added(json_output, "playbook", name)


@add_app.command("mcp-server")
def add_mcp_server_cmd(
    name: str = typer.Option(
        ..., "--name", help="Server name; becomes the mcp__<name>__<tool> prefix."
    ),
    stdio_command: str | None = typer.Option(
        None, "--stdio-command", help="Executable to launch (stdio transport)."
    ),
    stdio_arg: list[str] = typer.Option(
        [], "--stdio-arg", help="Argument for --stdio-command (repeatable)."
    ),
    env: list[str] = typer.Option(
        [], "--env", help="Literal env var as KEY=VALUE (repeatable, stdio only)."
    ),
    env_from_host: list[str] = typer.Option(
        [],
        "--env-from-host",
        help="Env var resolved from the host as TARGET=HOST_VAR (repeatable, stdio only).",
    ),
    cwd: str | None = typer.Option(
        None, "--cwd", help="Subprocess working dir, relative to the harness (stdio only)."
    ),
    url: str | None = typer.Option(
        None, "--url", help="Streamable HTTP endpoint (http transport)."
    ),
    header: list[str] = typer.Option(
        [], "--header", help="Literal HTTP header as 'Name: value' (repeatable, http only)."
    ),
    header_env: list[str] = typer.Option(
        [],
        "--header-env",
        help="HTTP header resolved from the host as Name=HOST_VAR (repeatable, http only).",
    ),
    tool: list[str] = typer.Option(
        [], "--tool", help="Allowlist a remote tool name (repeatable; omit to expose all)."
    ),
    deferred: bool = typer.Option(
        False, "--deferred", help="Register discovered tools inactive until search_tools."
    ),
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add an MCP server. Exactly one of --stdio-command / --url.

    Makes no live connection — a typo in the command or URL only surfaces at
    ``run``/``dry-run``/``mcp list-tools``.
    """
    with _guard(json_output):
        construct.add_mcp_server(
            directory,
            name=name,
            stdio_command=stdio_command,
            stdio_args=stdio_arg or None,
            stdio_env=_parse_kv_pairs(env, "--env") or None,
            stdio_env_from_host=_parse_kv_pairs(env_from_host, "--env-from-host") or None,
            stdio_cwd=cwd,
            url=url,
            headers=_parse_header_pairs(header) or None,
            header_env=_parse_kv_pairs(header_env, "--header-env") or None,
            tools=tool or None,
            deferred=deferred,
        )
        _added(json_output, "mcp-server", name)


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


@registry_app.command("add")
def registry_add_cmd(
    directory: Path = typer.Argument(..., help="Harness directory to register."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Register a harness so ``mcp serve --registered`` offers it to agents."""
    from hiveloom import registry as registry_mod

    with _guard(json_output):
        item = registry_mod.register(directory)
        if json_output:
            _emit_json({"ok": True, **item.model_dump()})
        else:
            _console.print(f"[green]registered[/green] {item.name} — {item.path}")


@registry_app.command("remove")
def registry_remove_cmd(
    target: str = typer.Argument(..., help="Harness directory path or harness name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Remove a harness from the registry (does not touch the harness itself)."""
    from hiveloom import registry as registry_mod

    with _guard(json_output):
        item = registry_mod.unregister(target)
        if json_output:
            _emit_json({"ok": True, **item.model_dump()})
        else:
            _console.print(f"[green]removed[/green] {item.path}")


@registry_app.command("list")
def registry_list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List registered harnesses and whether each currently validates."""
    from hiveloom import registry as registry_mod

    with _guard(json_output):
        items = registry_mod.registered()
        if json_output:
            _emit_json({"ok": True, "harnesses": [i.model_dump() for i in items]})
            return
        if not items:
            _console.print("registry is empty (add one with `hiveloom registry add <dir>`)")
            return
        table = Table(title="registered harnesses")
        table.add_column("name", style="bold cyan")
        table.add_column("path")
        table.add_column("status")
        for item in items:
            status = "[green]ok[/green]" if item.ok else f"[red]{item.error}[/red]"
            table.add_row(item.name or "-", item.path, status)
        _console.print(table)


@mcp_app.command("serve")
def mcp_serve_cmd(
    directories: list[Path] = typer.Argument(  # noqa: B008 - typer idiom
        None, help="Harness directories to expose (default: current directory)."
    ),
    registered: bool = typer.Option(
        False,
        "--registered",
        help="Serve every harness in the local registry (`hiveloom registry list`).",
    ),
    http: bool = typer.Option(
        False,
        "--http",
        help="Serve over streamable HTTP at /mcp instead of stdio. Auth via "
        "HIVELOOM_API_KEY (Bearer or X-API-Key); a non-loopback bind "
        "without the key is refused.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="HTTP bind host (with --http)."),
    port: int = typer.Option(8765, "--port", help="HTTP bind port (with --http)."),
) -> None:
    """Expose harnesses as MCP tools (one ``run_<name>`` tool each).

    The agent-facing front door: an MCP-capable agent mounts this server and
    can delegate a task to any listed harness, getting back a structured,
    validator-checked result. Non-interactive by design — stdout is the MCP
    protocol channel — so untrusted directories fail at startup instead of
    prompting; approve them first with ``hiveloom trust <dir>``.
    """
    from hiveloom import registry as registry_mod
    from hiveloom.serve.mcp import serve_http, serve_stdio

    with _guard(True):
        if registered:
            dirs, skipped = registry_mod.serveable()
            for entry in skipped:
                # stderr is safe: stdout carries the MCP protocol.
                print(
                    f"warning: skipping registered harness {entry['path']}: "
                    f"{entry['error']}",
                    file=sys.stderr,
                )
            if not dirs:
                raise SpecError(
                    "no serveable registered harnesses "
                    "(add one with `hiveloom registry add <dir>`)"
                )
        else:
            dirs = [Path(".")] if not directories else directories
        if http:
            serve_http(
                dirs, host=host, port=port, api_key=os.environ.get("HIVELOOM_API_KEY")
            )
        else:
            serve_stdio(dirs)


@mcp_app.command("list-tools")
def mcp_list_tools_cmd(
    directory: str = typer.Option(".", "--dir", "-d", help="Harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Connect to every declared MCP server, discover its tools, then disconnect.

    The dynamic analogue of ``catalog`` for server-defined tools: unlike
    builtins, an MCP server's tools aren't known until it's actually reached.
    A stdio server is arbitrary local exec, so this enforces the same trust
    gate as ``run``/``dry-run`` before connecting (reuses build_registry's
    bridge and closer — no second connection path).
    """
    from hiveloom import trust as trust_mod
    from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks
    from hiveloom.tools.registry import build_registry

    with _guard(json_output):
        yaml_path = harness_path(directory)
        base = yaml_path.parent
        trust_mod.ensure_trusted(base, _trust_prompt(json_output))
        spec = load_spec(yaml_path)
        resolve_hooks(spec, base)

        registry = build_registry(spec, base)
        try:
            tools = [
                registry.get(n) for n in registry.names() if "mcp" in registry.get(n).tags
            ]
        finally:
            registry.close()

        payload = [
            {
                "name": t.name,
                "description": t.description,
                "tags": t.tags,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]
        if json_output:
            _emit_json({"ok": True, "tools": payload})
            return
        if not payload:
            _console.print("no mcp tools (mcp_servers is empty)")
            return
        table = Table(title="mcp tools")
        table.add_column("name", style="bold cyan")
        table.add_column("description")
        table.add_column("tags", style="green")
        table.add_column("input", style="yellow")
        for t in payload:
            props = ", ".join((t["input_schema"] or {}).get("properties", {})) or "-"
            table.add_row(t["name"], t["description"], ", ".join(t["tags"]), props)
        _console.print(table)


@app.command()
def run(
    harness_dir: str = typer.Argument(".", help="Harness directory to run."),
    input_value: str = typer.Option(
        None,
        "--input",
        help="Legacy FILE-or-TEXT input heuristic (deprecated; use an explicit input flag).",
    ),
    input_text: str = typer.Option(
        None, "--input-text", help="Literal input text; never interpreted as a path."
    ),
    input_file: str = typer.Option(
        None, "--input-file", help="Read input from this file; missing files are errors."
    ),
    model: str = typer.Option(
        None, "--model", help="Override model id for this run without editing the harness."
    ),
    provider: str = typer.Option(
        None, "--provider", help="Override provider for this run without editing the harness."
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Use this caller-allocated run id."
    ),
    trace_dir: str = typer.Option(
        None, "--trace-dir", help="Write this run's trace under a durable directory."
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Resume a fork directory from the journal point it was created at.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Assemble the first model call without calling the model; MCP discovery does I/O.",
    ),
    stream: bool = typer.Option(
        False, "--stream", help="Stream every trace event to stdout as JSONL (result last)."
    ),
    approve: bool = typer.Option(
        False, "--approve", "-a", help="Trust the harness folder without prompting."
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help=(
            "Linked mode: pull the latest version before the run and push the "
            "traces after (requires `hiveloom cloud link`)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Run a harness on an input.

    ``--dry-run`` resolves hooks and prints the would-be first model call
    without calling the model API; declared MCP servers are still contacted
    for tool discovery. ``--stream`` emits each trace event as a JSON line
    while the run progresses — the embedding interface for other programs.
    ``--resume`` re-enters a directory made by ``hiveloom fork``: the parent
    run's conversation is seeded at the journal point the fork names, and no
    new task statement is added, so the run continues from that turn against
    whatever the fork's harness now says.
    Exit codes: 0 success, 1 verify failed, 2 guardrail halt, 4 runtime error.
    """
    from hiveloom import runner
    from hiveloom import trust as trust_mod

    with _guard(json_output):
        input_count = sum(
            value is not None for value in (input_value, input_text, input_file)
        )
        if (resume and input_count) or (not resume and input_count != 1):
            _fail(
                "pass exactly one of --input-text, --input-file, legacy --input, or --resume",
                json_output,
                ExitCode.SPEC_ERROR,
            )
            return
        if sync:
            from hiveloom import cloud as cloud_mod

            pulled = cloud_mod.pull(harness_dir)
            if pulled["changed"] and not (json_output or stream):
                _console.print(f"[green]pulled[/green] @ {pulled['version_hash']}")
        if approve:
            trust_mod.record_trust(harness_dir)
        if dry_run and resume:
            _fail(
                "--dry-run needs an input and cannot be used with --resume",
                json_output,
                ExitCode.SPEC_ERROR,
            )
            return
        literal_input = input_text is not None or input_file is not None
        resolved_input = input_text if input_text is not None else input_value
        if input_file is not None:
            from hiveloom.spec.loader import harness_path

            base = harness_path(harness_dir).parent
            direct = Path(input_file)
            candidates = [direct] if direct.is_absolute() else [direct, base / direct]
            selected = next((candidate for candidate in candidates if candidate.is_file()), None)
            if selected is None:
                raise SpecError(f"input file not found: {input_file}")
            resolved_input = selected.read_text(encoding="utf-8")
        if dry_run:
            info = runner.dry_run(
                harness_dir,
                resolved_input,
                literal_input=literal_input,
                model_override=model,
                provider_override=provider,
                approve_trust=_trust_prompt(json_output),
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

        if resume:
            from hiveloom import fork as fork_mod

            record = fork_mod.load_fork(harness_dir)
            if record is None:
                _fail(
                    f"{harness_dir} is not a fork directory (no {fork_mod.FORK_FILE}); "
                    "make one with `hiveloom fork <run_id> --at <seq>`",
                    json_output,
                    ExitCode.SPEC_ERROR,
                )
                return
            result = runner.run_harness(
                harness_dir,
                resume_messages=fork_mod.load_fork_context(harness_dir),
                lineage={
                    "parent_run_id": record.get("parent_run_id", ""),
                    "forked_at_seq": record.get("at_seq"),
                    "parent_line_hash": record.get("parent_line_hash", ""),
                },
                on_event=on_event,
                run_id=run_id,
                trace_dir=trace_dir,
                model_override=model,
                provider_override=provider,
                approve_trust=_trust_prompt(json_output or stream),
            )
        else:
            result = runner.run_harness(
                harness_dir,
                resolved_input,
                literal_input=literal_input,
                on_event=on_event,
                run_id=run_id,
                trace_dir=trace_dir,
                model_override=model,
                provider_override=provider,
                approve_trust=_trust_prompt(json_output or stream),
            )
        payload = runner.run_result_payload(result)
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
        if sync:
            from hiveloom import cloud as cloud_mod

            pushed = cloud_mod.push(harness_dir)
            if not (json_output or stream):
                _console.print(
                    f"[green]pushed[/green] {pushed['uploaded']} trace file(s) to the cloud"
                )
        raise typer.Exit(_RUN_STATUS_EXIT.get(result.status, ExitCode.RUNTIME_ERROR))


@app.command()
def serve(
    harness_dir: str = typer.Argument(".", help="Harness directory to serve."),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (0.0.0.0 in containers)."),
    port: int = typer.Option(8080, "--port", help="Bind port."),
    concurrency: int = typer.Option(
        1, "--concurrency", help="Concurrent runs allowed; extra requests get 429."
    ),
    approve: bool = typer.Option(
        False, "--approve", "-a", help="Trust the harness folder without prompting."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the startup line as JSON."),
) -> None:
    """Serve the harness over HTTP — the long-lived deployment interface.

    ``GET /healthz`` reports liveness; ``POST /runs`` runs the harness with
    either ``{"input": "..."}`` or ``{"messages": [...]}`` (the whole
    conversation, for a multi-turn caller). Add ``"stream": true`` for NDJSON
    trace events, final ``run_result`` line last — same format as
    ``run --stream``. Set
    ``HIVELOOM_API_KEY`` to require ``Authorization: Bearer`` / ``X-API-Key``
    on ``/runs``; run inputs are always treated as literal text, never file
    paths. Blocks until interrupted.
    """
    from hiveloom import serve as serve_mod
    from hiveloom import trust as trust_mod

    with _guard(json_output):
        if approve:
            trust_mod.record_trust(harness_dir)
        server = serve_mod.HarnessServer(
            harness_dir, host=host, port=port, concurrency=concurrency
        )
        info = {
            "ok": True,
            "name": server.harness_name,
            "version_hash": server.version_hash,
            "host": host,
            "port": server.server_address[1],
            "auth": bool(server.api_key),
            "concurrency": concurrency,
        }
        if json_output:
            _emit_json(info)
        else:
            auth = "API key required" if info["auth"] else "no API key (HIVELOOM_API_KEY unset)"
            _console.print(
                f"[green]serving[/green] {server.harness_name} "
                f"on http://{host}:{info['port']} — {auth}"
            )
        serve_mod.serve_forever(server)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ui(ctx: typer.Context) -> None:
    """Open the workbench: a local UI for building, running, and improving harnesses.

    A convenience wrapper around ``npx hiveloom-workbench``. The workbench is
    distributed on npm rather than with this package, because what it adds is a
    compiled web interface — something a harness running in production has no
    use for, and something this wheel should not carry for everyone who never
    opens a browser.

    It needs no separate install: ``npx`` fetches and runs it, and it brings its
    own API, which it starts against the interpreter that has hiveloom — this
    one. Every argument is passed straight through::

        hiveloom ui --port 8770 --scan-dir ./harnesses

    Blocks until interrupted.
    """
    import shutil

    npx = shutil.which("npx")
    if npx is None:
        _console.print(
            "[yellow]the workbench needs Node[/yellow] (npx was not found on PATH)\n"
            "\n"
            "  Install Node 20+, then:  [bold]npx hiveloom-workbench[/bold]\n"
            "\n"
            "The workbench ships on npm so this package stays what runs a harness."
        )
        raise typer.Exit(code=1)

    # Replaces this process rather than wrapping it: the workbench is long-lived
    # and interactive, and an extra Python process in the middle would only add
    # a layer for Ctrl-C to traverse.
    os.execv(npx, [npx, "--yes", "hiveloom-workbench", *ctx.args])


@app.command()
def trace(
    run_id: str = typer.Argument(..., help="Run id to display (e.g. run_abc123)."),
    directory: str | None = typer.Option(
        None, "--dir", "-d", help="Harness dir to ingest first if the run is unknown."
    ),
    materialize: int | None = typer.Option(
        None,
        "--materialize",
        "-m",
        help="Reconstruct the exact model request at this event seq and print it.",
    ),
    verify: bool = typer.Option(
        False, "--verify", help="Check the journal's append-only hash chain."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a run's trace: its summary and ordered events.

    Runs are ingested automatically by ``run``; pass ``--dir`` to ingest a
    harness's in-folder traces first (e.g. for a copied-back deployment).

    ``--materialize <seq>`` folds the journal back into the ``(system,
    messages, tools)`` triple that went to the model at that point — the
    conversation is recorded progressively, so a request is reconstructed
    rather than stored. ``--verify`` checks the hash chain instead: every
    event commits to the sha256 of the line before it, so an edited or
    removed line is reported with the position it broke at.
    """
    import json as _json

    from hiveloom import runner
    from hiveloom.logging.hive import Hive
    from hiveloom.logging.journal import state_at, verify_chain
    from hiveloom.logging.trace import payload_hash

    with _guard(json_output):
        with Hive() as hive:
            if directory is not None:
                runner.resolve_and_ingest(directory, hive)
            run = hive.get_run(run_id)
        if run is None:
            _fail(f"run '{run_id}' not found in the Hive", json_output, ExitCode.SPEC_ERROR)
            return

        events: list[dict[str, Any]] = []
        trace_path = run.get("trace_path")
        trace_file = Path(trace_path) if trace_path else None
        if trace_file is not None and trace_file.is_file():
            events = [
                _json.loads(line)
                for line in trace_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        if verify:
            if trace_file is None or not trace_file.is_file():
                detail = (
                    f"pruned at {run['trace_pruned_at']}"
                    if run.get("trace_pruned_at")
                    else "missing"
                )
                _fail(
                    f"trace file for '{run_id}' is {detail}",
                    json_output,
                    ExitCode.SPEC_ERROR,
                )
                return
            chain = verify_chain(trace_file)
            if json_output:
                _emit_json(
                    {
                        "ok": chain.ok,
                        "run_id": run_id,
                        "chained": chain.chained,
                        "checked": chain.checked,
                        "broken_at": chain.broken_at,
                        "reason": chain.reason,
                    }
                )
            else:
                colour = "green" if chain.ok and chain.chained else (
                    "yellow" if chain.ok else "red"
                )
                _console.print(f"[{colour}]{chain.summary()}[/{colour}]")
            if not chain.ok:
                raise typer.Exit(ExitCode.RUNTIME_ERROR)
            return

        if materialize is not None:
            target = next((e for e in events if e.get("seq") == materialize), None)
            if target is None:
                _fail(
                    f"no event with seq {materialize} in run '{run_id}'",
                    json_output,
                    ExitCode.SPEC_ERROR,
                )
                return
            # A model_call is emitted *after* the context events it consumed,
            # so its request is everything strictly before it.
            is_call = target.get("type") == "model_call"
            state = state_at(events, materialize, inclusive=not is_call)
            request = state.as_request()
            recorded = (target.get("payload") or {}).get("messages_hash")
            faithful = recorded is None or recorded == payload_hash(state.messages)
            if json_output:
                _emit_json(
                    {
                        "ok": True,
                        "run_id": run_id,
                        "seq": materialize,
                        "type": target.get("type"),
                        "faithful": faithful,
                        "request": request,
                    }
                )
                return
            if not faithful:
                _console.print(
                    "[yellow]warning:[/yellow] a context_assemble hook patched this "
                    "request without persisting it; the reconstruction below is the "
                    "conversation as journalled, not what went on the wire."
                )
            _console.print(_json.dumps(request, indent=2))
            return

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
        if run.get("trace_pruned_at"):
            _console.print(f"[dim]raw journal pruned at {run['trace_pruned_at']}[/dim]")
        for event in events:
            _console.print(f"  [dim]{event['seq']:>3}[/dim] {event['type']}")


@traces_app.command("prune")
def traces_prune(
    target: str = typer.Argument(..., help="Harness directory whose trace policy applies."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Plan and report deletions without changing files or the Hive."
    ),
    yes: bool = typer.Option(False, "--yes", help="Apply the configured retention policy."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Plan or apply explicit age, count, and byte limits for raw journals."""
    from hiveloom import trust
    from hiveloom.logging.hive import Hive
    from hiveloom.logging.retention import prune_trace_root
    from hiveloom.spec.loader import harness_path, load_spec

    with _guard(json_output):
        yaml_path = harness_path(target)
        if not yaml_path.exists():
            raise SpecError(f"no harness spec found at {yaml_path}")
        trust.ensure_trusted(yaml_path.parent)
        spec = load_spec(yaml_path)
        if spec.logging.retention is None:
            raise SpecError("logging.retention is not configured")
        if not dry_run and not yes:
            raise SpecError("pass --dry-run to inspect the plan or --yes to apply it")
        configured = Path(spec.logging.trace_dir).expanduser()
        trace_root = (
            configured.resolve()
            if configured.is_absolute()
            else (yaml_path.parent / configured).resolve()
        )
        if dry_run:
            plan = prune_trace_root(
                trace_root,
                spec.logging.retention,
                dry_run=True,
            )
        else:
            with Hive() as hive:
                # Raw evidence is not removed until every valid candidate has
                # an indexed record that can survive its journal.
                hive.ingest_dir(trace_root)
                plan = prune_trace_root(
                    trace_root,
                    spec.logging.retention,
                    hive=hive,
                )
        payload = {"ok": True, "dry_run": dry_run, **plan.to_dict()}
        if json_output:
            _emit_json(payload)
            return
        verb = "would prune" if dry_run else "pruned"
        _console.print(
            f"[bold]{verb} {payload['selected_runs']} trace(s)[/bold] "
            f"({payload['selected_bytes']} bytes) under {payload['root']}"
        )
        for item in payload["selected"]:
            _console.print(
                f"  {item['run_id']}  {item['size']} bytes  {', '.join(item['reasons'])}"
            )
        if not payload["limits_satisfied"]:
            _console.print(
                "[yellow]configured limits cannot be met while preserving protected files[/yellow]"
            )


@app.command()
def fork(
    run_id: str = typer.Argument(..., help="Parent run to fork (e.g. run_abc123)."),
    at: int | None = typer.Option(
        None, "--at", help="Journal seq of the model call to re-enter (default: the last)."
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Fork name inside the harness (default: <run_id>-<seq>)."
    ),
    directory: str | None = typer.Option(
        None, "--dir", "-d", help="Write the fork here instead, outside the harness."
    ),
    source: str | None = typer.Option(
        None, "--from", help="Harness folder to take files from (default: inferred)."
    ),
    list_points: bool = typer.Option(
        False, "--list", help="List the run's fork points and exit."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Replay the prefix on this model instead (an A/B)."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="Provider serving --model (default: the parent's)."
    ),
    allow_drift: bool = typer.Option(
        False,
        "--allow-drift",
        help="Fork even though a harness file has changed since the parent run.",
    ),
    ingest_dir: str | None = typer.Option(
        None, "--ingest", help="Harness dir to ingest first if the run is unknown."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Re-enter a finished run at one of its model calls.

    Materialises a new harness directory holding the harness that actually
    produced the parent run (reconstructed from its journal snapshot and
    checked against the working folder), a ``fork.yaml`` lineage record, and
    the folded conversation at that point. Edit the fork's ``harness.yaml``,
    then ``hiveloom run <dir> --resume`` to replay the identical prefix
    against the changed harness.

    Fork points are model calls: the state immediately before one is by
    construction a valid provider request, where an arbitrary seq can land
    mid-turn. ``--list`` shows them.

    ``--model`` makes the commonest edit at fork time — replay this exact
    prefix on a different model. It rewrites the fork's spec (through the same
    validated path as ``hiveloom set``), so the fork is a clean sample of a
    different harness version: identical prefix, one variable, and both arms
    keep their own fitness bucket. ``hiveloom lineage`` reads the result.

    The fork lands at ``<harness>/.hiveloom/forks/<name>``: an experiment on a
    harness belongs inside it, so a folder of harnesses stays a folder of
    harnesses and archiving one takes its experiments along. Forking a fork
    puts the new one beside it under the same original harness rather than
    nesting. ``--dir`` opts out for a one-off somewhere else — your own shell,
    your own choice — but the workbench has no such escape hatch.
    """
    from hiveloom import fork as fork_mod
    from hiveloom import runner
    from hiveloom.logging.hive import Hive
    from hiveloom.logging.journal import read_events

    with _guard(json_output):
        with Hive() as hive:
            if ingest_dir is not None:
                runner.resolve_and_ingest(ingest_dir, hive)
            run = hive.get_run(run_id)
        if run is None:
            _fail(f"run '{run_id}' not found in the Hive", json_output, ExitCode.SPEC_ERROR)
            return
        trace_path = run.get("trace_path")
        trace_file = Path(trace_path) if trace_path else None
        if trace_file is None or not trace_file.is_file():
            detail = (
                f"pruned at {run['trace_pruned_at']}"
                if run.get("trace_pruned_at")
                else "missing"
            )
            _fail(
                f"the journal for '{run_id}' is {detail}",
                json_output,
                ExitCode.SPEC_ERROR,
            )
            return

        if list_points:
            points = fork_mod.fork_points(read_events(trace_file))
            if json_output:
                _emit_json(
                    {
                        "ok": True,
                        "run_id": run_id,
                        "fork_points": [
                            {
                                "seq": p.seq,
                                "turn": p.turn,
                                "phase": p.phase,
                                "num_messages": p.num_messages,
                            }
                            for p in points
                        ],
                    }
                )
                return
            if not points:
                _console.print("[yellow]no fork points — this run made no model calls[/yellow]")
                return
            _console.print(f"[bold]{run_id}[/bold] — {len(points)} fork point(s)")
            for point in points:
                _console.print(f"  [dim]{point.seq:>3}[/dim] {point.label()}")
            return

        if directory is not None:
            target: str | Path = directory
        else:
            # Named after the parent run and the point in it, because that is
            # what distinguishes two forks of the same harness from each other.
            slug = name or f"{run_id.replace('run_', '')}-{at if at is not None else 'last'}"
            origin = (
                Path(source)
                if source is not None
                else fork_mod.find_harness_source(trace_file)
            )
            if origin is None:
                _fail(
                    f"cannot locate the harness folder that owns {trace_file}; "
                    "pass --from <harness dir>, or --dir to write the fork elsewhere",
                    json_output,
                    ExitCode.SPEC_ERROR,
                )
                return
            target = fork_mod.fork_target(origin, slug)
        result = fork_mod.create_fork(
            trace_file,
            target,
            at=at,
            source_dir=source,
            allow_drift=allow_drift,
            model=model,
            model_provider=provider,
        )
        if json_output:
            _emit_json(
                {
                    "ok": True,
                    "directory": str(result.directory),
                    "parent_run_id": result.parent_run_id,
                    "at_seq": result.at_seq,
                    "turn": result.turn,
                    "messages": result.messages,
                    "warnings": result.warnings,
                    "model_override": result.model_override,
                    "version_hash": result.version_hash,
                    "trust_inherited": result.trust_inherited,
                }
            )
            return
        _console.print(
            f"[green]forked[/green] {result.parent_run_id} @ seq {result.at_seq} "
            f"(turn {result.turn}, {result.messages} messages) -> {result.directory}"
        )
        if result.model_override:
            _console.print(
                f"  model: [magenta]{result.model_override['from']}[/magenta] -> "
                f"[magenta]{result.model_override['provider']}:"
                f"{result.model_override['model']}[/magenta]  "
                f"[dim](version {result.version_hash})[/dim]"
            )
        for warning in result.warnings:
            _console.print(f"[yellow]warning:[/yellow] {warning}")
        _console.print(
            f"[dim]edit {result.directory}/harness.yaml, then: "
            f"hiveloom run {result.directory} --resume[/dim]"
        )


@app.command()
def lineage(
    run_id: str = typer.Argument(..., help="Run to show the fork tree around."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a run's forks and the parent they diverged from.

    A fork shares its parent's journal up to the seq it re-entered at, so the
    two are comparable on that identical prefix — which is what makes a fork a
    controlled experiment rather than a second, unrelated run.
    """
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            tree = hive.lineage(run_id)
        if tree["run"] is None:
            _fail(f"run '{run_id}' not found in the Hive", json_output, ExitCode.SPEC_ERROR)
            return
        if json_output:
            _emit_json({"ok": True, **tree})
            return

        def _line(row: dict[str, Any], prefix: str = "") -> str:
            status = row.get("status", "?")
            colour = _status_colour(status)
            cost = row.get("cost_usd") or 0.0
            # The version hash and model path are the two things that can
            # differ between arms sharing a prefix; showing them is what makes
            # the comparison readable as an experiment.
            executed = row.get("model_path") or "(pre-1.0)"
            return (
                f"{prefix}[bold]{row['run_id']}[/bold] "
                f"[{colour}]{status}[/{colour}]  "
                f"turns={row.get('turns', 0)}  cost=${cost:.4f}  "
                f"[dim]{row.get('harness_version_hash', '')}[/dim] "
                f"[magenta]{executed}[/magenta]"
            )

        for ancestor in reversed(tree["ancestors"]):
            if ancestor.get("missing"):
                _console.print(f"[dim]{ancestor['run_id']} (not ingested)[/dim]")
                continue
            _console.print(_line(ancestor))
        run = tree["run"]
        if run.get("parent_run_id"):
            _console.print(
                f"[dim]  forked from {run['parent_run_id']} "
                f"at seq {run.get('forked_at_seq')}[/dim]"
            )
        _console.print(_line(run, prefix="> "))
        if not tree["forks"]:
            _console.print("[dim]no forks of this run[/dim]")
            return
        _console.print(
            f"[bold]{len(tree['forks'])} fork(s)[/bold] — identical prefix, one change each"
        )
        for child in tree["forks"]:
            _console.print(_line(child, prefix=f"  @seq {child.get('forked_at_seq')}  "))


@friction_app.command("list")
def friction_list(
    target: str = typer.Argument(..., help="Harness name, id, or harness directory."),
    category: str | None = typer.Option(None, "--category", help="Filter by category."),
    component: str | None = typer.Option(None, "--component", help="Filter by component."),
    recovered: str | None = typer.Option(
        None, "--recovered", help="Filter by recovery state: true or false."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Filter by requested, effective, or legacy model path."
    ),
    since: str | None = typer.Option(None, "--since", help="ISO timestamp lower bound."),
    until: str | None = typer.Option(None, "--until", help="ISO timestamp upper bound."),
    limit: int = typer.Option(100, "--limit", help="Maximum records to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List indexed run friction without opening raw journals."""
    from hiveloom import runner
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        if limit < 1 or limit > 1000:
            raise SpecError("--limit must be between 1 and 1000")
        recovered_value = _optional_bool(recovered, "--recovered")
        with Hive() as hive:
            key = runner.resolve_and_ingest(target, hive)
            records = hive.list_friction(
                key,
                category=category,
                component=component,
                recovered=recovered_value,
                model=model,
                since=since,
                until=until,
                limit=limit,
            )
        if json_output:
            _emit_json(
                {"ok": True, "harness_key": key, "count": len(records), "friction": records}
            )
            return
        if not records:
            _console.print("[dim]no indexed friction matched[/dim]")
            return
        table = Table(title=f"run friction for {key}")
        table.add_column("time", style="dim")
        table.add_column("run")
        table.add_column("category", style="yellow")
        table.add_column("component")
        table.add_column("recovered", justify="center")
        table.add_column("summary")
        for record in records:
            table.add_row(
                record.get("timestamp") or "",
                record["run_id"],
                record["category"],
                record.get("component") or "",
                "yes" if record["recovered"] else "no",
                record["summary"],
            )
        _console.print(table)


@app.command()
def stats(
    target: str = typer.Argument(..., help="Harness name or harness directory."),
    include_friction: bool = typer.Option(
        False, "--include-friction", help="Include indexed retries and recovered failures."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show Hive stats for a harness: success rate, cost, and turns per version.

    A harness directory is ingested on the fly (idempotent by run id) so stats
    reflect its in-folder traces even after being copied back from production.
    """
    from hiveloom import runner
    from hiveloom.logging.hive import Hive
    from hiveloom.spec.loader import load_spec

    with _guard(json_output):
        with Hive() as hive:
            key = runner.resolve_and_ingest(target, hive)
            # A directory target knows its display name; a bare-key target is
            # left for summary() to resolve from the run history.
            yaml_path = Path(target) / "harness.yaml" if Path(target).is_dir() else Path(target)
            display = (
                load_spec(yaml_path).name
                if yaml_path.name == "harness.yaml" and yaml_path.exists()
                else None
            )
            summary = hive.summary(key, display_name=display)
            recent = hive.recent_failures(key, 5)
            outcomes = hive.outcome_summary(key)
            friction = hive.friction_summary(key) if include_friction else None

        if json_output:
            payload = {"ok": True, **summary, "recent_failures": recent, "outcomes": outcomes}
            if friction is not None:
                payload["friction"] = friction
            _emit_json(payload)
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
            held_out = sum(v.get("swapped_runs", 0) for v in summary["versions"])
            if held_out:
                _console.print(
                    f"[yellow]{held_out} run(s) changed model mid-run and are "
                    "excluded above[/yellow] — they did not execute the harness as "
                    "declared. See the model-path table."
                )
        if summary.get("model_paths"):
            table = Table(title="per model path (runs that swapped)")
            table.add_column("version", style="cyan")
            table.add_column("model path", style="magenta")
            table.add_column("runs", justify="right")
            table.add_column("success", justify="right", style="green")
            table.add_column("avg cost", justify="right")
            for row in summary["model_paths"]:
                table.add_row(
                    row["version"],
                    row["model_path"],
                    str(row["runs"]),
                    f"{row['success_rate']:.0%}",
                    f"${row['avg_cost_usd']:.4f}",
                )
            _console.print(table)
        if summary["playbooks"]:
            table = Table(title="per playbook")
            table.add_column("playbook", style="cyan")
            table.add_column("runs", justify="right")
            table.add_column("success", justify="right", style="green")
            table.add_column("refused", justify="right", style="yellow")
            table.add_column("avg cost", justify="right")
            table.add_column("avg turns", justify="right")
            for p in summary["playbooks"]:
                table.add_row(
                    p["playbook"],
                    str(p["runs"]),
                    f"{p['success_rate']:.0%}",
                    str(p["refusals"]),
                    f"${p['avg_cost_usd']:.4f}",
                    f"{p['avg_turns']:.1f}",
                )
            _console.print(table)
        if outcomes["labelled_runs"]:
            _console.print(
                f"[bold]labelled outcomes[/bold] — {outcomes['labelled_runs']} labelled, "
                f"{outcomes['outcome_success_rate']:.0%} held up "
                f"({outcomes['failures']} rejected by the world)"
            )
        if friction is not None:
            _console.print(
                f"[bold]friction[/bold]: {friction['events']} event(s) across "
                f"{friction['runs']} run(s), {friction['recovered']} recovered"
            )
            if friction["categories"]:
                table = Table(title="friction by category")
                table.add_column("category", style="yellow")
                table.add_column("events", justify="right")
                table.add_column("runs", justify="right")
                table.add_column("recovered", justify="right", style="green")
                table.add_column("unrecovered", justify="right")
                for row in friction["categories"]:
                    table.add_row(
                        row["category"],
                        str(row["events"]),
                        str(row["runs"]),
                        str(row["recovered"]),
                        str(row["unrecovered"]),
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


def _metric_target(target: str, hive: Any) -> str:
    """Resolve a harness directory or already-indexed harness key."""
    from hiveloom import runner

    return runner.resolve_and_ingest(target, hive)


@eval_app.command("schema")
def eval_schema(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON schema."),
) -> None:
    """Emit the machine-readable eval document contract without loading code."""
    from hiveloom.evals import EvalSpec

    schema = EvalSpec.model_json_schema()
    if json_output:
        _emit_json({"ok": True, "schema": schema})
    else:
        _console.print_json(data=schema)


@eval_app.command("validate")
def eval_validate(
    path: str = typer.Argument(..., help="Path to an eval YAML document."),
    approve: bool = typer.Option(False, "--approve", help="Trust referenced local code."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Resolve a dataset and scorers without running a model or exposing cases."""
    from hiveloom.evals import validate_eval_spec

    with _guard(json_output):
        approval = (lambda _path: True) if approve else _trust_prompt(json_output)
        validated = validate_eval_spec(path, approve_trust=approval)
        payload = {
            "ok": True,
            "path": str(validated.path),
            "harness_path": str(validated.harness_path),
            "schema_version": validated.spec.schema_version,
            "case_count": validated.case_count,
            "repetitions": validated.spec.repetitions,
            "dataset": validated.spec.dataset.loader,
            "scorers": [scorer.name for scorer in validated.spec.scorers],
            "identity": validated.identity.model_dump(mode="json"),
        }
        if json_output:
            _emit_json(payload)
        else:
            _console.print(
                f"[green]valid[/green] {validated.case_count} case(s), "
                f"eval {validated.identity.eval_id[:12]}"
            )


def _eval_manifest_payload(manifest: Any) -> dict[str, Any]:
    from hiveloom.eval_runner import manifest_path as eval_manifest_path

    return {
        "ok": manifest.status == "completed",
        "eval_run_id": manifest.eval_run_id,
        "status": manifest.status,
        "summary": manifest.summary(),
        "manifest_path": str(eval_manifest_path(manifest.eval_run_id)),
        "manifest": manifest.model_dump(mode="json"),
    }


def _emit_eval_manifest(manifest: Any, json_output: bool) -> None:
    payload = _eval_manifest_payload(manifest)
    if json_output:
        _emit_json(payload)
    else:
        summary = payload["summary"]
        _console.print(
            f"[green]{manifest.eval_run_id}[/green] {manifest.status}: "
            f"{summary['completed']}/{summary['total']} completed"
        )
        _console.print(f"manifest: {payload['manifest_path']}")
    if manifest.status != "completed":
        raise typer.Exit(ExitCode.RUNTIME_ERROR)


@eval_app.command("run")
def eval_run_command(
    path: str = typer.Argument(..., help="Path to an eval YAML document."),
    model: str | None = typer.Option(None, "--model", help="Run-only model override."),
    provider: str | None = typer.Option(
        None, "--provider", help="Run-only provider override."
    ),
    repetitions: int | None = typer.Option(
        None, "--repetitions", min=1, max=10_000
    ),
    concurrency: int = typer.Option(1, "--concurrency", min=1, max=128),
    infrastructure_retries: int = typer.Option(
        0, "--infrastructure-retries", min=0, max=20
    ),
    approve: bool = typer.Option(False, "--approve", help="Trust referenced local code."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Run a model/case/repetition matrix with an atomic resumable manifest."""
    from hiveloom.eval_runner import run_eval

    with _guard(json_output):
        approval = (lambda _path: True) if approve else _trust_prompt(json_output)
        manifest = run_eval(
            path,
            model_override=model,
            provider_override=provider,
            repetitions=repetitions,
            concurrency=concurrency,
            infrastructure_retries=infrastructure_retries,
            approve_trust=approval,
        )
        _emit_eval_manifest(manifest, json_output)


@eval_app.command("resume")
def eval_resume_command(
    eval_run_id: str = typer.Argument(..., help="Eval run id from the manifest."),
    approve: bool = typer.Option(False, "--approve", help="Trust referenced local code."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Resume only unfinished cells after revalidating every content digest."""
    from hiveloom.eval_runner import resume_eval

    with _guard(json_output):
        approval = (lambda _path: True) if approve else _trust_prompt(json_output)
        manifest = resume_eval(eval_run_id, approve_trust=approval)
        _emit_eval_manifest(manifest, json_output)


@eval_app.command("status")
def eval_status_command(
    eval_run_id: str = typer.Argument(..., help="Eval run id from the manifest."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Read an eval checkpoint without loading evaluator or harness code."""
    from hiveloom.eval_runner import load_eval_manifest

    with _guard(json_output):
        manifest = load_eval_manifest(eval_run_id)
        payload = _eval_manifest_payload(manifest)
        payload["ok"] = True
        if json_output:
            _emit_json(payload)
        else:
            summary = payload["summary"]
            _console.print(
                f"[green]{manifest.eval_run_id}[/green] {manifest.status}: "
                f"{summary['completed']}/{summary['total']} completed"
            )


def _eval_output_format(format_name: str, json_output: bool) -> str:
    selected = "json" if json_output else format_name.lower()
    if selected not in {"json", "markdown"}:
        raise ValueError("eval report format must be 'json' or 'markdown'")
    return selected


@eval_app.command("report")
def eval_report_command(
    eval_run_id: str = typer.Argument(..., help="Eval run id to report."),
    format_name: str = typer.Option("json", "--format", help="json or markdown."),
    json_output: bool = typer.Option(False, "--json", help="Emit canonical JSON."),
) -> None:
    """Build a report from indexed eval state without reading raw traces."""
    from hiveloom.eval_reports import build_eval_report, render_report_markdown

    selected = _eval_output_format(format_name, json_output)
    with _guard(selected == "json"):
        report = build_eval_report(eval_run_id)
        if selected == "json":
            _emit_json({"ok": True, "report": report})
        else:
            _console.print(render_report_markdown(report), markup=False)


@eval_app.command("compare")
def eval_compare_command(
    baseline_id: str = typer.Argument(..., help="Baseline eval run id."),
    candidate_id: str = typer.Argument(..., help="Candidate eval run id."),
    format_name: str = typer.Option("json", "--format", help="json or markdown."),
    json_output: bool = typer.Option(False, "--json", help="Emit canonical JSON."),
) -> None:
    """Compare matching case/repetition cells and label unmatched cells."""
    from hiveloom.eval_reports import compare_evals, render_comparison_markdown

    selected = _eval_output_format(format_name, json_output)
    with _guard(selected == "json"):
        comparison = compare_evals(baseline_id, candidate_id)
        if selected == "json":
            _emit_json({"ok": True, "comparison": comparison})
        else:
            _console.print(render_comparison_markdown(comparison), markup=False)


@metrics_app.command("schema")
def metrics_schema(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON schema."),
) -> None:
    """Emit the machine-readable RunMetric ingestion contract."""
    from hiveloom.metrics import RunMetric

    schema = RunMetric.model_json_schema()
    if json_output:
        _emit_json({"ok": True, "schema": schema})
    else:
        _console.print_json(data=schema)


@metrics_app.command("record")
def metrics_record(
    target: str = typer.Argument(..., help="Harness name, id, or directory."),
    run_id: str = typer.Option(..., "--run-id", help="Indexed run receiving the metric."),
    name: str = typer.Option(..., "--name", help="User-defined metric name."),
    value: float = typer.Option(..., "--value", help="Finite numeric value."),
    direction: str = typer.Option(..., "--direction", help="maximize or minimize."),
    unit: str = typer.Option(..., "--unit", help="Metric unit, for example ratio or usd."),
    source: str = typer.Option(..., "--source", help="Scorer/evaluator identity."),
    scope: str = typer.Option("run", "--scope", help="case, run, or eval."),
    metadata: str = typer.Option("{}", "--metadata", help="JSON object with bounded metadata."),
    idempotency_key: str | None = typer.Option(
        None, "--idempotency-key", help="Optional caller-owned deduplication key."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Attach one validated numeric metric to an indexed run."""
    from hiveloom.logging.hive import Hive
    from hiveloom.metrics import RunMetric, record_run_metrics

    with _guard(json_output):
        parsed_metadata = json.loads(metadata)
        if not isinstance(parsed_metadata, dict):
            raise ValueError("--metadata must be a JSON object")
        metric = RunMetric(
            run_id=run_id,
            name=name,
            value=value,
            direction=direction,
            unit=unit,
            source=source,
            scope=scope,
            metadata=parsed_metadata,
            idempotency_key=idempotency_key,
        )
        with Hive() as hive:
            harness_key = _metric_target(target, hive)
            receipt = record_run_metrics(hive, harness_key, [metric])
        payload = {
            "ok": True,
            "harness_key": harness_key,
            **receipt,
            "idempotency_key": metric.resolved_idempotency_key(),
        }
        if json_output:
            _emit_json(payload)
        else:
            _console.print(
                f"[green]recorded[/green] {metric.name}={metric.value:g} {metric.unit} "
                f"for {metric.run_id} ({receipt['duplicates']} duplicate)"
            )


@metrics_app.command("import")
def metrics_import(
    target: str = typer.Argument(..., help="Harness name, id, or directory."),
    path: str = typer.Argument(..., help="NDJSON file containing RunMetric objects."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Transactionally import metrics after validating every NDJSON row."""
    from hiveloom.logging.hive import Hive
    from hiveloom.metrics import load_metrics_ndjson, record_run_metrics

    with _guard(json_output):
        metrics = load_metrics_ndjson(path)
        with Hive() as hive:
            harness_key = _metric_target(target, hive)
            receipt = record_run_metrics(hive, harness_key, metrics)
        payload = {"ok": True, "harness_key": harness_key, **receipt}
        if json_output:
            _emit_json(payload)
        else:
            _console.print(
                f"[green]imported[/green] {receipt['inserted']} metric(s), "
                f"{receipt['duplicates']} duplicate(s)"
            )


@metrics_app.command("list")
def metrics_list(
    target: str = typer.Argument(..., help="Harness name, id, or directory."),
    run_id: str | None = typer.Option(None, "--run-id"),
    name: str | None = typer.Option(None, "--name"),
    source: str | None = typer.Option(None, "--source"),
    scope: str | None = typer.Option(None, "--scope"),
    model: str | None = typer.Option(None, "--model"),
    since: str | None = typer.Option(None, "--since", help="Run finish time, ISO 8601."),
    until: str | None = typer.Option(None, "--until", help="Run finish time, ISO 8601."),
    limit: int = typer.Option(1000, "--limit", min=1, max=100_000),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List metrics and scope-safe aggregates with explicit missing counts."""
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        if scope is not None and scope not in {"case", "run", "eval"}:
            raise ValueError("--scope must be case, run, or eval")
        with Hive() as hive:
            harness_key = _metric_target(target, hive)
            filters = {
                "run_id": run_id,
                "name": name,
                "source": source,
                "scope": scope,
                "model": model,
                "since": since,
                "until": until,
            }
            metrics = hive.list_metrics(harness_key, limit=limit, **filters)
            aggregates = hive.metric_aggregates(harness_key, **filters)
        payload = {
            "ok": True,
            "harness_key": harness_key,
            "metrics": metrics,
            "aggregates": aggregates,
        }
        if json_output:
            _emit_json(payload)
            return
        table = Table(title=f"metrics: {harness_key}")
        for column in ("run", "name", "value", "unit", "scope", "source", "model"):
            table.add_column(column)
        for metric in metrics:
            table.add_row(
                metric["run_id"],
                metric["name"],
                f"{metric['value']:g}",
                metric["unit"],
                metric["scope"],
                metric["source"],
                metric["model"] or "-",
            )
        _console.print(table)
        for aggregate in aggregates:
            _console.print(
                f"[cyan]{aggregate['name']}[/cyan] ({aggregate['scope']}, "
                f"{aggregate['source']}): mean={aggregate['mean']:g}, "
                f"n={aggregate['sample_count']}, "
                f"missing={aggregate['missing_value_count']}"
            )


@app.command()
def outcome(
    run_id: str = typer.Argument(..., help="Run id to label."),
    result: str = typer.Argument(..., help="'success' or 'failure'."),
    source: str = typer.Option(
        "external", "--source", help="Who or what produced this judgement."
    ),
    detail: str = typer.Option("", "--detail", help="Why, in one line."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Attach a real-world outcome to a completed run.

    Validators grade a run while it happens; some signals only arrive later
    and from somewhere else — a human confirmed or dismissed the proposal, the
    extracted record turned out wrong. Recording that here feeds it back into
    `stats` and `evolve` as the reward signal, without rewriting what the run
    itself did.
    """
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            recorded = hive.record_outcome(
                run_id, result, source=source, detail=detail
            )
        if json_output:
            _emit_json({"ok": True, **recorded})
            return
        _console.print(
            f"recorded [bold]{recorded['outcome']}[/bold] for {run_id} "
            f"(source: {recorded['source']})"
        )


def _status_colour(status: str) -> str:
    return "green" if status == "success" else "yellow"


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
    a validate/repair loop. Needs credentials for the configured provider when
    that provider requires them.
    """
    from hiveloom.generate import generate_harness

    with _guard(json_output):
        spec = generate_harness(task, output, model_id=model_id, blueprint=blueprint)
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
    from_parent: bool = typer.Option(
        False,
        "--from-parent",
        help="Analyse the parent run's version instead (a fork with no runs yet).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Analyze Hive failures and propose a gated harness mutation.

    Guardrails/model/logging.redact can never be changed. YAML changes within the
    mutable set auto-apply with ``--yes``; regenerated code always needs y/n
    approval. Recorded in the Hive under a new version hash. With ``--propose``,
    the gated proposal is queued (see ``hiveloom proposals``) instead of applied;
    a human reviews and applies it later via ``proposals apply``.

    ``--from-parent`` is for a fresh fork: analysis is normally scoped to the
    version on disk, which a fork has no runs for, so the failures that
    motivated the fork are invisible at exactly the moment there is most to
    say. It reads the parent version out of ``fork.yaml`` and drafts against
    those failures, applying the result to the fork's own spec.
    """
    from hiveloom import evolve as evolve_mod
    from hiveloom import runner
    from hiveloom import trust as trust_mod
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.generate.llm import build_strong_model
    from hiveloom.logging.hive import Hive
    from hiveloom.spec.loader import harness_path, load_spec

    with _guard(json_output):
        trust_mod.ensure_trusted(harness_dir, _trust_prompt(json_output))
        base = harness_path(harness_dir).parent  # the harness dir, given a dir or a yaml path
        # Load first so harness-declared extensions have registered any provider
        # named by --model. Resolving the strong model before the spec made an
        # otherwise runnable local provider look unknown and fell back to Claude.
        spec = load_spec(harness_dir)
        model = build_strong_model(model_id, base)
        with Hive() as hive:
            name = runner.resolve_and_ingest(harness_dir, hive)
            # Scoped to one version — see analyze().
            version = _analysis_version(harness_dir, spec, base, from_parent=from_parent)
            report = evolve_mod.analyze(hive, name, version=version)
            if report.is_empty():
                reason = _nothing_to_evolve_reason(
                    hive, name, version, from_parent=from_parent
                )
                if json_output:
                    _emit_json({"ok": True, "changed": False, "reason": reason})
                else:
                    _console.print(f"[green]nothing to evolve[/green] — {reason}")
                return

            if propose:
                record = proposals_mod.create_proposal(
                    hive,
                    spec,
                    harness_dir,
                    report,
                    model,
                    trigger="fork" if from_parent else "manual",
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


# --------------------------------------------------------------------------- #
# Proposals queue
# --------------------------------------------------------------------------- #
def _analysis_version(
    harness_dir: str, spec: Any, base: Path, *, from_parent: bool
) -> str:
    """Which harness version's failures drive the proposal.

    Normally the spec on disk: evolving against failures from a version that
    has already been changed drafts a fix for a bug that may be gone.

    A fork is the exception that scoping cannot see. It exists *because* its
    parent failed, and until it is resumed it has no runs of its own — so the
    default reports nothing to evolve at exactly the moment there is most to
    say. ``--from-parent`` reads the parent version out of the fork's lineage
    record, so the proposal is drafted against the failures that motivated the
    fork and applied to the fork's own spec.
    """
    if not from_parent:
        from hiveloom.logging.trace import spec_version_hash

        return spec_version_hash(spec, base)

    from hiveloom import fork as fork_mod

    return fork_mod.parent_version_hash(harness_dir)


def _nothing_to_evolve_reason(
    hive: Any, name: str, version: str, *, from_parent: bool = False
) -> str:
    """Why the report is empty — the cases need different next steps.

    Analysis is scoped to one spec version, so a harness edited since its last
    failing run has failures on record that deliberately do not count.
    Reporting that as "no recorded failures" would send the user looking for a
    logging bug instead of re-running the harness.

    Under ``--from-parent`` the scoped version is the parent's, so "re-run it"
    is the wrong advice: the runs that would matter already happened, and an
    empty report means the fork is pointed somewhere with nothing on record.
    """
    stale = hive.failure_count(name)
    if from_parent:
        if stale:
            return (
                f"no failures recorded for the parent version {version} "
                f"({stale} on other versions of '{name}') — the parent run may "
                "have succeeded, or its journal was never ingested"
            )
        return f"no failures recorded for '{name}' at any version"
    if stale:
        return (
            f"no failures recorded for the current harness version "
            f"({stale} on earlier versions) — re-run the harness to collect fresh ones"
        )
    return "no recorded failures"


def _emit_proposal_created(record: Any, json_output: bool) -> None:
    if json_output:
        from hiveloom.evolve.proposals import proposal_payload

        _emit_json({"ok": True, **proposal_payload(record)})
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
    from hiveloom.evolve.evolver import CodeChange

    def approve(change: CodeChange) -> bool:
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
            _emit_json(
                {"ok": True, "proposals": [proposals_mod.proposal_payload(r) for r in records]}
            )
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
        ..., help="Harness name or directory."
    ),
    proposal_id: str = typer.Argument(..., help="Proposal id (e.g. prop_abc123)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a queued proposal: its rationale, gate result, and any apply result."""
    from hiveloom import runner
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            name = runner.resolve_and_ingest(harness_dir, hive)
            record = proposals_mod.get_proposal(hive, proposal_id)
        if record is None:
            raise ProposalQueueError(f"no proposal with id '{proposal_id}'")
        if record.harness_name != name:
            raise ProposalQueueError(
                f"proposal '{proposal_id}' belongs to harness '{record.harness_name}', "
                f"not '{name}'"
            )

        if json_output:
            _emit_json({"ok": True, **proposals_mod.proposal_payload(record)})
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

    approved_files = (
        {path for item in approve_code_arg.split(",") if (path := item.strip())}
        if approve_code_arg
        else None
    )
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
        ..., help="Harness name or directory."
    ),
    proposal_id: str = typer.Argument(..., help="Proposal id to reject."),
    reason: str = typer.Option("", "--reason", help="Why this proposal is being rejected."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Reject a queued proposal. Never touches harness.yaml."""
    from hiveloom import runner
    from hiveloom.evolve import proposals as proposals_mod
    from hiveloom.logging.hive import Hive

    with _guard(json_output):
        with Hive() as hive:
            name = runner.resolve_and_ingest(harness_dir, hive)
            record = proposals_mod.get_proposal(hive, proposal_id)
            if record is None:
                raise ProposalQueueError(f"no proposal with id '{proposal_id}'")
            if record.harness_name != name:
                raise ProposalQueueError(
                    f"proposal '{proposal_id}' belongs to harness '{record.harness_name}', "
                    f"not '{name}'"
                )
            proposals_mod.reject_proposal(hive, proposal_id, reason)
        if json_output:
            _emit_json({"ok": True, "proposal_id": proposal_id, "status": "rejected"})
        else:
            _console.print(f"[yellow]rejected[/yellow] proposal {proposal_id}")


@app.command()
def package(
    harness_dir: str = typer.Argument(..., help="Harness directory to package."),
    docker: bool = typer.Option(False, "--docker", help="Also emit a Dockerfile."),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Docker image serves HTTP (`hiveloom serve` on :8080) instead of one-shot run.",
    ),
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
            serve=serve,
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


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _serve_approve_callback():
    """Interactive trust prompt for `serve`, or ``None`` when stdin isn't a
    TTY — refuse to start rather than hang waiting for input that will never
    come (a piped/CI/systemd invocation).
    """
    if not sys.stdin.isatty():
        return None
    return _trust_prompt(json_output=False)


@app.command("control-plane")
def control_plane(
    harness_dir: str = typer.Argument(..., help="Harness directory to serve."),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8420, "--port", help="Bind port."),
    max_concurrent_runs: int = typer.Option(
        1, "--max-concurrent-runs", help="How many runs may execute at once."
    ),
    max_queued_runs: int = typer.Option(
        4, "--max-queued-runs", help="How many more runs may wait before /run returns 503."
    ),
    authorized_keys: str | None = typer.Option(
        None, "--authorized-keys", help="Override the authorized-keys store path."
    ),
    approve: bool = typer.Option(
        False, "--approve", "-a", help="Trust the harness folder without prompting."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the startup line as JSON."
    ),
) -> None:
    """Serve a harness's CLI surface over HTTP to bearer-authorized members.

    Non-production by design: no TLS, no replay cache, one harness per
    process. See ``docs/control-plane.md`` for the endpoint table, the
    ``hiveloom keys`` custody model, and the full limitations list. Trust is
    checked once here, before the socket binds — never per request.
    """
    from hiveloom import trust as trust_mod
    from hiveloom.serve.app import create_app

    with _guard(json_output):
        if approve:
            trust_mod.record_trust(harness_dir)
        trust_mod.ensure_trusted(harness_dir, _serve_approve_callback())

        if host not in _LOOPBACK_HOSTS:
            _err_console.print(
                f"[red]warning:[/red] binding to '{host}' — there is no TLS, so bearer "
                "tokens travel in cleartext over the network. Put a reverse proxy or "
                "SSH tunnel in front, or bind to 127.0.0.1 instead."
            )

        import uvicorn

        asgi_app = create_app(
            harness_dir,
            keys_path=authorized_keys,
            max_concurrent_runs=max_concurrent_runs,
            max_queued_runs=max_queued_runs,
        )
        info = {
            "ok": True,
            "service": "control-plane",
            "harness_dir": str(Path(harness_dir).resolve()),
            "host": host,
            "port": port,
            "max_concurrent_runs": max_concurrent_runs,
            "max_queued_runs": max_queued_runs,
        }
        if json_output:
            _emit_json(info)
        else:
            _console.print(
                f"[green]serving control plane[/green] for {harness_dir} "
                f"on http://{host}:{port}"
            )
        uvicorn.run(asgi_app, host=host, port=port)


# --------------------------------------------------------------------------- #
# Keys: ed25519 identity + bearer tokens for the (non-production) control plane
# --------------------------------------------------------------------------- #
# Custody model: a member runs `generate` (and later `sign`) on THEIR OWN
# machine — the private key never leaves it. The operator runs `authorize` on
# the deploy box using the member's public key. See docs/control-plane.md for
# the full limitations list (no TLS, no replay cache, no revocation propagation).
@keys_app.command("generate")
def keys_generate_cmd(
    name: str = typer.Argument(..., help="Key name; written as <name>.pem."),
    out_dir: str = typer.Option(
        "~/.hiveloom/keys", "--out-dir", help="Directory to write the private key into."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Generate an ed25519 keypair on THIS machine; the private key never leaves it.

    Writes ``<out-dir>/<name>.pem`` (mode 0600, refusing to overwrite) and
    prints the public key — send that to the harness operator for
    ``keys authorize`` — and its key_id.
    """
    from hiveloom.serve import keys as keys_mod

    with _guard(json_output):
        directory = Path(out_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        key_path = directory / f"{name}.pem"
        private_pem, public_key = keys_mod.generate_keypair()
        try:
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise SpecError(f"{key_path} already exists; refusing to overwrite") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_pem)
        key_id = keys_mod.key_id_for(public_key)
        if json_output:
            _emit_json(
                {
                    "ok": True,
                    "private_key_path": str(key_path),
                    "public_key": public_key,
                    "key_id": key_id,
                }
            )
        else:
            _console.print(f"[green]generated[/green] {key_path} (0600)")
            _console.print(f"public key: {public_key}")
            _console.print(f"key_id: {key_id}")


@keys_app.command("authorize")
def keys_authorize_cmd(
    name: str = typer.Argument(..., help="Human-readable name for this key (e.g. a person)."),
    public_key: str = typer.Argument(..., help="The public key printed by `keys generate`."),
    harness: str = typer.Option(..., "--harness", help="Harness directory to authorize into."),
    scope: list[str] = typer.Option(
        ...,
        "--scope",
        help="Repeatable; scopes this key may use (required — e.g. --scope run). "
        "Pass --scope '*' only for a fully-trusted admin key; there is no default, "
        "so a key is never granted broad scope by omission.",
    ),
    authorized_keys: str | None = typer.Option(
        None, "--authorized-keys", help="Override the store path ($HIVELOOM_AUTHORIZED_KEYS)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Authorize a member's public key for a harness (run on the deploy box).

    Idempotent on key_id: re-authorizing an already-known public key
    un-revokes it and replaces its scopes.
    """
    from hiveloom.serve import auth as auth_mod

    with _guard(json_output):
        path = auth_mod.authorized_keys_path(harness, override=authorized_keys)
        row = auth_mod.authorize_key(path, name=name, public_key_b64=public_key, scopes=scope)
        if json_output:
            _emit_json({"ok": True, **row})
        else:
            _console.print(
                f"[green]authorized[/green] {row['key_id']} ({name}) scopes={row['scopes']}"
            )


@keys_app.command("revoke")
def keys_revoke_cmd(
    key_id: str = typer.Argument(..., help="Key id to revoke (see `keys list`)."),
    harness: str = typer.Option(..., "--harness", help="Harness directory."),
    authorized_keys: str | None = typer.Option(
        None, "--authorized-keys", help="Override the store path ($HIVELOOM_AUTHORIZED_KEYS)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Revoke a key. The row is kept (not deleted) as an audit trail."""
    from hiveloom.serve import auth as auth_mod

    with _guard(json_output):
        path = auth_mod.authorized_keys_path(harness, override=authorized_keys)
        auth_mod.revoke_key(path, key_id)
        if json_output:
            _emit_json({"ok": True, "key_id": key_id, "revoked": True})
        else:
            _console.print(f"[yellow]revoked[/yellow] {key_id}")


@keys_app.command("list")
def keys_list_cmd(
    harness: str = typer.Option(..., "--harness", help="Harness directory."),
    authorized_keys: str | None = typer.Option(
        None, "--authorized-keys", help="Override the store path ($HIVELOOM_AUTHORIZED_KEYS)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List authorized keys for a harness (revoked rows are kept and shown)."""
    from hiveloom.serve import auth as auth_mod

    with _guard(json_output):
        path = auth_mod.authorized_keys_path(harness, override=authorized_keys)
        rows = auth_mod.list_keys(path)
        if json_output:
            _emit_json({"ok": True, "keys": rows})
            return
        if not rows:
            _console.print("[green]no authorized keys[/green]")
            return
        table = Table()
        table.add_column("key_id")
        table.add_column("name")
        table.add_column("scopes")
        table.add_column("revoked")
        table.add_column("added_at")
        for row in rows:
            table.add_row(
                row["key_id"],
                row["name"],
                ", ".join(row["scopes"]),
                str(row["revoked"]),
                row["added_at"],
            )
        _console.print(table)


@keys_app.command("sign")
def keys_sign_cmd(
    key: str = typer.Option(..., "--key", help="Path to the private key PEM from `keys generate`."),
    subject: str | None = typer.Option(
        None, "--subject", help="Subject claim (defaults to the key file's stem)."
    ),
    scope: str = typer.Option("*", "--scope", help="Scope this token requests."),
    ttl: int = typer.Option(900, "--ttl", help="Token lifetime in seconds (default 900 = 15 min)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Mint a bearer token on YOUR OWN machine, using your private key.

    Prints the token (or ``{"token": ...}`` with ``--json``). The 900s
    default TTL is short by design — there is no revocation-propagation or
    replay cache, so mint a fresh token whenever you need one.
    """
    from hiveloom.serve import keys as keys_mod

    with _guard(json_output):
        key_path = Path(key)
        private_pem = key_path.read_text(encoding="utf-8")
        public_key = keys_mod.public_key_b64_for(private_pem)
        key_id = keys_mod.key_id_for(public_key)
        token = keys_mod.sign_token(
            private_pem, key_id=key_id, subject=subject or key_path.stem, scope=scope,
            ttl_seconds=ttl,
        )
        if json_output:
            _emit_json({"ok": True, "token": token, "key_id": key_id})
        else:
            _console.print(token)


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


def _parse_kv_pairs(values: list[str], flag: str) -> dict[str, str]:
    """Parse repeated ``KEY=VALUE`` option values into a dict."""
    result: dict[str, str] = {}
    for raw in values:
        key, sep, value = raw.partition("=")
        if not sep or not key:
            raise SpecError(f"{flag} expects KEY=VALUE (got {raw!r})")
        result[key] = value
    return result


def _parse_header_pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated ``Name: value`` option values into a dict."""
    result: dict[str, str] = {}
    for raw in values:
        name, sep, value = raw.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            raise SpecError(f"--header expects 'Name: value' (got {raw!r})")
        result[name] = value
    return result
# --------------------------------------------------------------------------- #
# cloud — linked mode against a hiveloom-cloud harness
# --------------------------------------------------------------------------- #
cloud_app = typer.Typer(
    help="Pair a local folder with a hiveloom-cloud harness and keep the two in sync."
)
app.add_typer(cloud_app, name="cloud")


@cloud_app.command("link")
def cloud_link(
    url: str = typer.Argument(
        ...,
        help=(
            "The server origin, e.g. https://app.example.com — hiveloom-cloud or "
            "any server implementing docs/sync-protocol.md."
        ),
    ),
    token: str = typer.Argument(..., help="The harness's link token (hl_link_…) from the web UI."),
    directory: str | None = typer.Option(
        None, "--dir", help="Target directory (defaults to the harness slug)."
    ),
    allow_insecure_http: bool = typer.Option(
        False,
        "--allow-insecure-http",
        help="Allow sending the link token over plain HTTP to a non-local host.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Pair a directory with a remote harness and pull it."""
    from hiveloom import cloud as cloud_mod

    with _guard(json_output):
        result = cloud_mod.link_harness(
            url, token, directory, allow_insecure_http=allow_insecure_http
        )
        if json_output:
            _emit_json({"ok": True, **result})
        else:
            _console.print(
                f"[green]linked[/green] {result['dir']} → {result['slug']} "
                f"@ {result['version_hash']}"
            )


@cloud_app.command("pull")
def cloud_pull(
    harness_dir: str = typer.Argument(".", help="Linked harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Fetch the latest harness version when the remote hash moved."""
    from hiveloom import cloud as cloud_mod

    with _guard(json_output):
        result = cloud_mod.pull(harness_dir)
        if json_output:
            _emit_json({"ok": True, **result})
        elif result["changed"]:
            _console.print(f"[green]pulled[/green] @ {result['version_hash']}")
        else:
            _console.print(f"already up to date @ {result['version_hash']}")


@cloud_app.command("push")
def cloud_push(
    harness_dir: str = typer.Argument(".", help="Linked harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Upload local run traces so the web harness can evolve from them."""
    from hiveloom import cloud as cloud_mod

    with _guard(json_output):
        result = cloud_mod.push(harness_dir)
        if json_output:
            _emit_json({"ok": True, **result})
        else:
            _console.print(
                f"[green]pushed[/green] {result['uploaded']} trace file(s), "
                f"{result['run_count']} run(s)"
            )


@cloud_app.command("sync")
def cloud_sync(
    harness_dir: str = typer.Argument(".", help="Linked harness directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Push local traces, then pull the latest version."""
    from hiveloom import cloud as cloud_mod

    with _guard(json_output):
        result = cloud_mod.sync(harness_dir)
        if json_output:
            _emit_json({"ok": True, **result})
        else:
            _console.print(
                f"[green]synced[/green] pushed {result['uploaded']} trace file(s); "
                + (
                    f"pulled @ {result['version_hash']}"
                    if result["changed"]
                    else f"already up to date @ {result['version_hash']}"
                )
            )


if __name__ == "__main__":
    app()
