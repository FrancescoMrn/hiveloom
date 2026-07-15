"""Incremental harness construction — the library behind the construct CLI.

Both ``hiveloom init/set/add/remove`` and (in M4) ``hiveloom generate`` drive
these functions, so there is one code path for building a harness. Every
mutating function:

* applies the change to the raw YAML dict,
* scaffolds stub files for new code hooks (with correct signatures),
* re-validates the full spec (structural + code-hook resolution),
* rolls back — removing scaffolded files, never writing — on any error,
* appends a ``construction_event`` to the harness's in-folder trace dir.

A harness directory is therefore never left in an invalid state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hiveloom import trust
from hiveloom.errors import SpecError
from hiveloom.spec.loader import (
    HARNESS_FILENAME,
    dump_spec,
    harness_path,
    load_raw,
    resolve_hooks,
    spec_from_dict,
)
from hiveloom.spec.schema import HarnessSpec

TRACE_SUBDIR = Path(".hiveloom") / "traces"
CONSTRUCTION_LOG = "construction.jsonl"


# --------------------------------------------------------------------------- #
# Construction event logging
# --------------------------------------------------------------------------- #
def _log_construction(
    directory: Path,
    command: str,
    args: dict[str, Any],
    outcome: str,
    error: str | None = None,
) -> None:
    trace_dir = directory / TRACE_SUBDIR
    trace_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "construction_event",
        "timestamp": datetime.now(UTC).isoformat(),
        "command": command,
        "args": args,
        "outcome": outcome,
    }
    if error is not None:
        event["error"] = error
    with (trace_dir / CONSTRUCTION_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


# --------------------------------------------------------------------------- #
# Commit helper (validate + roll back on error)
# --------------------------------------------------------------------------- #
def _commit(
    directory: Path,
    raw: dict[str, Any],
    created: list[Path],
    command: str,
    args: dict[str, Any],
) -> HarnessSpec:
    try:
        # Trust gate first: validation imports hook/extension code.
        trust.ensure_trusted(directory)
        spec = spec_from_dict(raw, source=str(harness_path(directory)), base_dir=directory)
        resolve_hooks(spec, directory)
    except SpecError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        _log_construction(directory, command, args, "error", str(exc))
        raise
    harness_path(directory).write_text(dump_spec(spec), encoding="utf-8")
    _log_construction(directory, command, args, "ok")
    return spec


# --------------------------------------------------------------------------- #
# Stub scaffolding
# --------------------------------------------------------------------------- #
_TOOL_STUB = '''"""Auto-scaffolded hiveloom tool — fill in the implementation."""

from hiveloom.tools import tool


@tool(description={desc!r}, tags=[])
def {func}(query: str) -> str:
    """TODO: implement this tool. Adjust the parameters and return type."""
    raise NotImplementedError("tool {func} is not implemented yet")
'''

_VALIDATOR_STUB = '''"""Auto-scaffolded hiveloom validator — fill in the checks."""


def {func}(run_output, run_context):
    """TODO: validate the run output.

    Return an object/dict with ``passed: bool`` and actionable ``feedback: str``
    (the feedback is injected into the model's context on retry).
    """
    return {{"passed": True, "feedback": "TODO: implement validation."}}
'''

_GUARDRAIL_STUB = '''"""Auto-scaffolded hiveloom guardrail — fill in the policy."""


def {func}(context):
    """TODO: inspect ``context`` and return an Allow/Block/Halt decision."""
    return {{"decision": "allow"}}
'''

_EVENT_HOOK_STUB = '''"""Auto-scaffolded hiveloom event hook — fill in the handler.

See hiveloom.events for the payload of each event and what a returned dict
may change (block/patch a tool call, patch a result, replace the context,
cancel or supply a compaction summary). Return None to just observe.
"""


def {func}(event):
    """TODO: handle the event payload. Must not raise."""
    return None
'''

_STUBS = {
    "tool": _TOOL_STUB,
    "validator": _VALIDATOR_STUB,
    "guardrail": _GUARDRAIL_STUB,
    "hook": _EVENT_HOOK_STUB,
}

_SKILL_STUB = """---
name: {name}
description: {description}
---

# {name}

TODO: write the skill instructions the model should follow when this skill
matches the task. Keep the description above short — it is always in the
system prompt; this file is read on demand.
"""


def _scaffold_hook(directory: Path, code_ref: str, kind: str, description: str) -> Path | None:
    """Create a stub file for ``path.py:function`` if the file does not exist.

    Returns the created path (for rollback) or ``None`` if the file already
    existed.
    """
    rel_path, func_name = code_ref.rsplit(":", 1)
    file_path = directory / rel_path
    if file_path.exists():
        return None
    file_path.parent.mkdir(parents=True, exist_ok=True)
    template = _STUBS[kind]
    file_path.write_text(
        template.format(func=func_name, desc=description or f"TODO: describe {func_name}"),
        encoding="utf-8",
    )
    return file_path


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
_GITIGNORE = ".env\n.hiveloom/\n__pycache__/\n"

_README_TEMPLATE = """# {name}

{task}

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom`) executes it.

## Run

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
hiveloom run . --input path/to/input.txt
```

Traces are written to `.hiveloom/traces/` and travel with the harness.
"""


def init_harness(directory: str | Path, name: str, task: str) -> HarnessSpec:
    """Create a minimal, valid harness directory skeleton and return its spec."""
    target = Path(directory)
    if (target / HARNESS_FILENAME).exists():
        raise SpecError(f"a harness already exists at {target / HARNESS_FILENAME}")
    target.mkdir(parents=True, exist_ok=True)
    for sub in ("tools", "validators", "schemas"):
        (target / sub).mkdir(exist_ok=True)
    (target / TRACE_SUBDIR).mkdir(parents=True, exist_ok=True)

    spec = HarnessSpec(
        name=name,
        description=task,
        system_prompt=(
            f"You are an agent that performs the following task:\n{task}\n\n"
            "Use the available tools, and stop when the work is complete and verified."
        ),
    )
    # Structural + hook validation of the freshly built spec (defensive).
    spec = spec_from_dict(spec.model_dump(mode="json"), source=str(target))
    harness_path(target).write_text(dump_spec(spec), encoding="utf-8")

    (target / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (target / ".env.example").write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    (target / "requirements.txt").write_text(f"hiveloom=={_pkg_version()}\n", encoding="utf-8")
    (target / "README.md").write_text(
        _README_TEMPLATE.format(name=name, task=task), encoding="utf-8"
    )
    trust.record_trust(target)  # built on this machine -> trusted
    _log_construction(target, "init", {"name": name, "task": task}, "ok")
    return spec


def _pkg_version() -> str:
    from hiveloom import __version__

    return __version__


# --------------------------------------------------------------------------- #
# set
# --------------------------------------------------------------------------- #
def set_field(
    directory: str | Path,
    path: str,
    value: str | None = None,
    file: str | Path | None = None,
) -> HarnessSpec:
    """Set a scalar/object field by dotted path (e.g. ``loop.max_turns``).

    ``value`` is parsed as a YAML scalar (so ``"30"`` becomes ``30``). Use
    ``file`` to load the value verbatim from a text file (e.g. a system prompt).
    """
    directory = Path(directory)
    if file is not None:
        parsed: Any = Path(file).read_text(encoding="utf-8")
    elif value is not None:
        parsed = yaml.safe_load(value)
    else:
        raise SpecError("set requires either a value or a --file")

    raw = load_raw(directory)
    parts = path.split(".")
    cursor: Any = raw
    for segment in parts[:-1]:
        if segment not in cursor or not isinstance(cursor[segment], dict):
            cursor[segment] = {}
        cursor = cursor[segment]
    if not isinstance(cursor, dict):
        raise SpecError(f"cannot set '{path}': parent is not a mapping")
    cursor[parts[-1]] = parsed

    return _commit(
        directory, raw, [], "set", {"path": path, "value": parsed if file is None else f"<{file}>"}
    )


def set_value(directory: str | Path, path: str, value: Any) -> HarnessSpec:
    """Set a dotted field to an already-typed value (no YAML parsing).

    Used by the generator and evolver, which supply native JSON values.
    """
    directory = Path(directory)
    raw = load_raw(directory)
    parts = path.split(".")
    cursor: Any = raw
    for segment in parts[:-1]:
        if segment not in cursor or not isinstance(cursor[segment], dict):
            cursor[segment] = {}
        cursor = cursor[segment]
    if not isinstance(cursor, dict):
        raise SpecError(f"cannot set '{path}': parent is not a mapping")
    cursor[parts[-1]] = value
    return _commit(directory, raw, [], "set", {"path": path})


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #
def add_tool(
    directory: str | Path,
    builtin: str | None = None,
    code: str | None = None,
    description: str | None = None,
) -> HarnessSpec:
    """Add a tool. Exactly one of ``builtin`` / ``code`` must be given."""
    directory = Path(directory)
    entry, created = _make_ref(
        directory, "tool", builtin, code, description, require_description=True
    )
    raw = load_raw(directory)
    raw.setdefault("tools", []).append(entry)
    return _commit(directory, raw, created, "add_tool", {"builtin": builtin, "code": code})


def add_validator(
    directory: str | Path,
    builtin: str | None = None,
    code: str | None = None,
    description: str | None = None,
    **params: Any,
) -> HarnessSpec:
    """Add a verifier. Exactly one of ``builtin`` / ``code`` must be given."""
    directory = Path(directory)
    entry, created = _make_ref(directory, "validator", builtin, code, description)
    if builtin is not None:
        entry.update({k: v for k, v in params.items() if v is not None})
    raw = load_raw(directory)
    raw.setdefault("verify", {}).setdefault("validators", []).append(entry)
    return _commit(
        directory, raw, created, "add_validator", {"builtin": builtin, "code": code}
    )


def add_guardrail(
    directory: str | Path,
    builtin: str,
    value: Any = None,
    **params: Any,
) -> HarnessSpec:
    """Add a builtin guardrail (guardrails are code-hookable but usually builtin)."""
    directory = Path(directory)
    entry: dict[str, Any] = {"builtin": builtin}
    if value is not None:
        entry["value"] = value
    entry.update({k: v for k, v in params.items() if v is not None})
    raw = load_raw(directory)
    raw.setdefault("guardrails", []).append(entry)
    return _commit(directory, raw, [], "add_guardrail", {"builtin": builtin, "value": value})


def add_hook(
    directory: str | Path,
    on: str,
    builtin: str | None = None,
    code: str | None = None,
    description: str | None = None,
    **params: Any,
) -> HarnessSpec:
    """Attach an event hook. Exactly one of ``builtin`` / ``code`` must be given."""
    directory = Path(directory)
    entry, created = _make_ref(directory, "hook", builtin, code, description)
    entry["event"] = on
    if builtin is not None:
        entry.update({k: v for k, v in params.items() if v is not None})
    raw = load_raw(directory)
    raw.setdefault("hooks", []).append(entry)
    return _commit(
        directory, raw, created, "add_hook", {"event": on, "builtin": builtin, "code": code}
    )


def add_skill(directory: str | Path, name: str, description: str) -> HarnessSpec:
    """Add a skill: scaffold ``skills/<name>/SKILL.md`` and list it in the spec."""
    from hiveloom.skills import skill_path

    directory = Path(directory)
    created: list[Path] = []
    skill_file = directory / skill_path(name)
    if not skill_file.exists():
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(
            _SKILL_STUB.format(name=name, description=description), encoding="utf-8"
        )
        created.append(skill_file)
    raw = load_raw(directory)
    skills = raw.setdefault("skills", [])
    if name in skills:
        raise SpecError(f"skill '{name}' is already listed in the spec")
    skills.append(name)
    return _commit(directory, raw, created, "add_skill", {"name": name})


def _make_ref(
    directory: Path,
    kind: str,
    builtin: str | None,
    code: str | None,
    description: str | None,
    require_description: bool = False,
) -> tuple[dict[str, Any], list[Path]]:
    if (builtin is None) == (code is None):
        raise SpecError(f"add {kind} requires exactly one of --builtin or --code")
    created: list[Path] = []
    if builtin is not None:
        return {"builtin": builtin}, created
    entry: dict[str, Any] = {"code": code}
    if description is not None:
        entry["description"] = description
    elif require_description:
        raise SpecError(f"add {kind} --code requires --description")
    scaffolded = _scaffold_hook(directory, code, kind, description or "")
    if scaffolded is not None:
        created.append(scaffolded)
    return entry, created


# --------------------------------------------------------------------------- #
# remove
# --------------------------------------------------------------------------- #
_LIST_SECTIONS = {
    "tools": ("tools",),
    "guardrails": ("guardrails",),
    "validators": ("verify", "validators"),
    "hooks": ("hooks",),
    "skills": ("skills",),
}


def remove_item(directory: str | Path, target: str) -> HarnessSpec:
    """Remove a tool/guardrail/validator by identifier, or delete a field path.

    ``target`` matches a builtin name or a ``path.py:function`` code ref in any
    of the list sections; failing that, it is treated as a dotted field path to
    delete (which reverts the field to its default).
    """
    directory = Path(directory)
    raw = load_raw(directory)

    removed = _remove_from_lists(raw, target)
    if not removed:
        if not _delete_path(raw, target):
            raise SpecError(
                f"nothing named or located at '{target}' to remove"
            )
    return _commit(directory, raw, [], "remove", {"target": target})


def _remove_from_lists(raw: dict[str, Any], target: str) -> bool:
    removed = False
    for keys in _LIST_SECTIONS.values():
        container: Any = raw
        for key in keys[:-1]:
            container = container.get(key) if isinstance(container, dict) else None
            if container is None:
                break
        if not isinstance(container, dict):
            continue
        items = container.get(keys[-1])
        if not isinstance(items, list):
            continue
        kept = [it for it in items if not _ref_matches(it, target)]
        if len(kept) != len(items):
            container[keys[-1]] = kept
            removed = True
    return removed


def _ref_matches(item: Any, target: str) -> bool:
    if isinstance(item, str):  # skills are plain names
        return item == target
    if not isinstance(item, dict):
        return False
    return item.get("builtin") == target or item.get("code") == target


def _delete_path(raw: dict[str, Any], path: str) -> bool:
    parts = path.split(".")
    cursor: Any = raw
    for segment in parts[:-1]:
        if not isinstance(cursor, dict) or segment not in cursor:
            return False
        cursor = cursor[segment]
    if isinstance(cursor, dict) and parts[-1] in cursor:
        del cursor[parts[-1]]
        return True
    return False
