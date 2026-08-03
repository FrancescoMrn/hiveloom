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
add_app = typer.Typer(help="Add a tool, validator, guardrail, hook, or skill to a harness.")
app.add_typer(add_app, name="add")
proposals_app = typer.Typer(help="Review, apply, or reject queued evolution proposals.")
app.add_typer(proposals_app, name="proposals")
keys_app = typer.Typer(
    help="Ed25519 keys and bearer tokens for the (non-production) HTTP control plane."
)
app.add_typer(keys_app, name="keys")
mcp_app = typer.Typer(help="Introspect the MCP servers declared by a harness.")
app.add_typer(mcp_app, name="mcp")

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
def models(
    provider: str = typer.Argument("", help="Show only this provider's models."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List model providers and their known models, pricing, and key status.

    Answers the two questions that block a first run: which provider name goes
    in ``model.provider``, and which environment variable holds its key. An
    ``open`` provider also accepts model ids not listed here (new releases,
    aggregator routes, whatever a local server is serving); a closed one does
    not, so a typo fails validation. Free: this never touches the API.
    """
    from hiveloom import ext

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
    input_value: str = typer.Option(..., "--input", help="Input FILE path or literal TEXT."),
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
    Exit codes: 0 success, 1 verify failed, 2 guardrail halt, 4 runtime error.
    """
    from hiveloom import runner
    from hiveloom import trust as trust_mod

    with _guard(json_output):
        if sync:
            from hiveloom import cloud as cloud_mod

            pulled = cloud_mod.pull(harness_dir)
            if pulled["changed"] and not (json_output or stream):
                _console.print(f"[green]pulled[/green] @ {pulled['version_hash']}")
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

    ``GET /healthz`` reports liveness; ``POST /runs`` with ``{"input": "..."}``
    runs the harness (add ``"stream": true`` for NDJSON trace events, final
    ``run_result`` line last — same format as ``run --stream``). Set
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
    from hiveloom.generate.llm import build_strong_model
    from hiveloom.logging.hive import Hive
    from hiveloom.logging.trace import spec_version_hash
    from hiveloom.spec.loader import harness_path, load_spec

    with _guard(json_output):
        trust_mod.ensure_trusted(harness_dir, _trust_prompt(json_output))
        base = harness_path(harness_dir).parent  # the harness dir, given a dir or a yaml path
        model = build_strong_model(model_id, base)
        with Hive() as hive:
            name = runner.resolve_and_ingest(harness_dir, hive)
            spec = load_spec(harness_dir)
            # Scoped to the current version — see analyze().
            report = evolve_mod.analyze(hive, name, version=spec_version_hash(spec, base))
            if report.is_empty():
                reason = _nothing_to_evolve_reason(hive, name)
                if json_output:
                    _emit_json({"ok": True, "changed": False, "reason": reason})
                else:
                    _console.print(f"[green]nothing to evolve[/green] — {reason}")
                return

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


# --------------------------------------------------------------------------- #
# Proposals queue
# --------------------------------------------------------------------------- #
def _nothing_to_evolve_reason(hive: Any, name: str) -> str:
    """Why the report is empty — the two cases need different next steps.

    Analysis is scoped to the current spec version, so a harness edited since
    its last failing run has failures on record that deliberately do not count.
    Reporting that as "no recorded failures" would send the user looking for a
    logging bug instead of re-running the harness.
    """
    stale = hive.failure_count(name)
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
    url: str = typer.Argument(..., help="The hiveloom-cloud origin, e.g. https://app.example.com"),
    token: str = typer.Argument(..., help="The harness's link token (hl_link_…) from the web UI."),
    directory: str | None = typer.Option(
        None, "--dir", help="Target directory (defaults to the harness slug)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Pair a directory with a remote harness and pull it."""
    from hiveloom import cloud as cloud_mod

    with _guard(json_output):
        result = cloud_mod.link_harness(url, token, directory)
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
