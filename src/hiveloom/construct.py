"""Incremental harness construction — the library behind the construct CLI.

Both ``hiveloom init/set/add/remove`` and ``hiveloom generate`` drive these
functions, so there is one code path for building a harness. Every
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
import re
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args, get_origin

import yaml
from pydantic import BaseModel

from hiveloom import trust
from hiveloom.catalog import CATALOGS
from hiveloom.errors import HiveloomError, SpecError
from hiveloom.spec.loader import (
    HARNESS_FILENAME,
    atomic_write_text,
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
        atomic_write_text(harness_path(directory), dump_spec(spec))
        _log_construction(directory, command, args, "ok")
        return spec
    except Exception as exc:  # noqa: BLE001 - rollback is part of the construct contract
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            _log_construction(directory, command, args, "error", str(exc))
        except Exception:  # noqa: BLE001 - never hide the original construction failure
            pass
        if isinstance(exc, HiveloomError):
            raise
        raise HiveloomError(f"could not {command}: {type(exc).__name__}: {exc}") from exc


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

_PLAYBOOK_HOOK_STUB = '''"""Auto-scaffolded hiveloom playbook hook — fill in the behaviour.

Runs when a playbook is entered or left. The payload carries
``playbook``, ``from``/``to``, ``reason``, and ``run_context``.

Return None to just observe, or a dict:
  {{"context": "..."}}                 inject a note into the conversation
  {{"block": True, "reason": "..."}}   refuse the entry/exit (a boundary gate)

Must not raise: a raising hook is traced as hook_error and skipped.
"""


def {func}(event):
    """TODO: implement the playbook gate/side effect."""
    return None
'''

_STUBS = {
    "tool": _TOOL_STUB,
    "validator": _VALIDATOR_STUB,
    "guardrail": _GUARDRAIL_STUB,
    "hook": _EVENT_HOOK_STUB,
    "playbook_hook": _PLAYBOOK_HOOK_STUB,
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
    if code_ref.count(":") != 1 or code_ref.startswith(":") or code_ref.endswith(":"):
        raise SpecError(
            "code hook must be 'relative/path.py:function_name' "
            f"(got {code_ref!r})"
        )
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
# `.venv/` because a harness now declares its deps in pyproject.toml, so
# `uv sync` inside the folder is the natural way to install them.
_GITIGNORE = ".env\n.venv/\n.hiveloom/\n__pycache__/\n"

# Dependencies are declared the way every other Python project declares them,
# in a PEP 621 ``[project]`` table, rather than in a requirements.txt that no
# resolver treats as authoritative. The harness folder is not a package and is
# never built: ``[tool.uv] package = false`` says so, so ``uv sync`` installs
# the pins and stops there.
_REQUIRES_PYTHON = ">=3.11"

_PYPROJECT_TEMPLATE = """\
# What this harness needs to run. The folder is the harness; this file pins
# the runtime that executes it, plus anything its tools, validators and
# extensions import.
#
#     uv sync                        # or: pip install hiveloom=={version}
[project]
name = "{project_name}"
version = "0.1.0"
description = "{description}"
requires-python = "{requires_python}"
dependencies = [
    "hiveloom=={version}",
]

# Not a library: nothing here is built or installed, only the pins resolved.
[tool.uv]
package = false
"""

_README_TEMPLATE = """# {name}

{task}

## What this is

A self-contained **hiveloom** harness. The folder is the harness; the runtime
(`pip install hiveloom`) executes it.

## Run

```bash
uv sync                # install the pinned runtime (pyproject.toml)
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
    target_existed = target.exists()
    created_dirs: list[Path] = []
    originals: dict[Path, bytes | None] = {}

    def make_dir(path: Path) -> None:
        if not path.exists():
            path.mkdir(parents=True)
            created_dirs.append(path)

    def write(path: Path, content: str) -> None:
        if path not in originals:
            originals[path] = path.read_bytes() if path.exists() else None
        atomic_write_text(path, content)

    try:
        make_dir(target)
        for sub in ("tools", "validators", "schemas"):
            make_dir(target / sub)
        make_dir(target / ".hiveloom")
        make_dir(target / TRACE_SUBDIR)

        spec = HarnessSpec(
            name=name,
            # The identity the Hive keys evidence on — assigned once, here,
            # so renaming or copying the folder never splits (or merges) a
            # harness's run history with a same-named neighbour's.
            id=f"hl-{uuid.uuid4().hex[:12]}",
            description=task,
            system_prompt=(
                f"You are an agent that performs the following task:\n{task}\n\n"
                "Use the available tools, and stop when the work is complete and verified."
            ),
        )
        # Structural + hook validation of the freshly built spec (defensive).
        spec = spec_from_dict(spec.model_dump(mode="json"), source=str(target))
        write(harness_path(target), dump_spec(spec))
        write(target / ".gitignore", _GITIGNORE)
        write(target / ".env.example", "ANTHROPIC_API_KEY=\n")
        write(
            target / "pyproject.toml",
            _PYPROJECT_TEMPLATE.format(
                project_name=_project_name(name),
                description=_toml_line(task),
                requires_python=_REQUIRES_PYTHON,
                version=_pkg_version(),
            ),
        )
        write(target / "README.md", _README_TEMPLATE.format(name=name, task=task))

        # Trust is convenience metadata, not a requirement for a valid local harness.
        try:
            trust.record_trust(target)
        except Exception:  # noqa: BLE001 - e.g. an unwritable HIVELOOM_HOME
            pass
        _log_construction(target, "init", {"name": name, "task": task}, "ok")
        return spec
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        if not target_existed:
            shutil.rmtree(target, ignore_errors=True)
        else:
            for path in reversed(created_dirs):
                try:
                    path.rmdir()
                except OSError:
                    pass
        raise


def _pkg_version() -> str:
    from hiveloom import __version__

    return __version__


def _project_name(name: str) -> str:
    """Normalise a harness name into a valid PEP 508 distribution name.

    Harness names are free text; project names are not. Anything outside
    ``[a-z0-9._-]`` folds to a hyphen, and a name that survives as empty falls
    back to ``harness`` rather than producing a pyproject nothing can parse.
    """
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return slug or "harness"


def _toml_line(text: str) -> str:
    """Fold arbitrary text into one escaped TOML basic-string line."""
    line = " ".join(text.split())
    if len(line) > 200:
        line = line[:197].rstrip() + "..."
    return line.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------- #
# Dotted/indexed path walking (shared by set/remove)
# --------------------------------------------------------------------------- #
def _resolve_index(items: list[Any], segment: str, path: str) -> int:
    """Resolve a numeric path segment to an existing index into ``items``.

    Existing index only — there is no "append via set" — so an out-of-range
    (or non-numeric) segment raises a :class:`SpecError` naming the list's
    actual length rather than silently falling through to dict handling.
    """
    if not segment.isdigit():
        raise SpecError(f"cannot resolve '{path}': '{segment}' is not a list index")
    index = int(segment)
    if index >= len(items):
        noun = "item" if len(items) == 1 else "items"
        raise SpecError(
            f"cannot resolve '{path}': index {index} out of range "
            f"(list has {len(items)} {noun})"
        )
    return index


def _walk_settable(raw: dict[str, Any], path: str) -> tuple[Any, Any]:
    """Return ``(parent, key)`` for ``path``'s final segment, creating along the way.

    A dotted segment auto-vivifies a missing/wrong-type dict key (unchanged
    from before), while a numeric segment indexes into an existing list —
    never creating one, per :func:`_resolve_index`. ``key`` is a dict key
    (``str``) when ``parent`` is a dict, or a list index (``int``) when
    ``parent`` is a list; either way ``parent[key] = value`` sets it.
    """
    parts = path.split(".")
    cursor: Any = raw
    for segment in parts[:-1]:
        if isinstance(cursor, list):
            cursor = cursor[_resolve_index(cursor, segment, path)]
            continue
        if not isinstance(cursor, dict):
            raise SpecError(f"cannot set '{path}': parent is not a mapping")
        existing = cursor.get(segment)
        if not isinstance(existing, (dict, list)):
            existing = {}
            cursor[segment] = existing
        cursor = existing
    last = parts[-1]
    if isinstance(cursor, list):
        return cursor, _resolve_index(cursor, last, path)
    if not isinstance(cursor, dict):
        raise SpecError(f"cannot set '{path}': parent is not a mapping")
    return cursor, last


def _walk_removable(raw: dict[str, Any], path: str) -> tuple[Any, Any] | None:
    """Return ``(parent, key)`` for ``path``'s final segment, or ``None`` if not found.

    Mirrors :func:`_walk_settable` but never creates anything: a missing dict
    key along the way means the path simply doesn't exist (``remove`` falls
    back to its "nothing found" error), matching the pre-existing behaviour of
    dotted object paths. A numeric segment still resolves via
    :func:`_resolve_index`, so an out-of-range list index raises the same
    clear error it does for ``set`` rather than being swallowed as "not found".
    """
    parts = path.split(".")
    cursor: Any = raw
    for segment in parts[:-1]:
        if isinstance(cursor, list):
            cursor = cursor[_resolve_index(cursor, segment, path)]
            continue
        if not isinstance(cursor, dict) or segment not in cursor:
            return None
        cursor = cursor[segment]
    last = parts[-1]
    if isinstance(cursor, list):
        return cursor, _resolve_index(cursor, last, path)
    if not isinstance(cursor, dict):
        return None
    return cursor, last


def _unwrap_annotation(annotation: Any) -> Any:
    """Strip ``Annotated[...]``/``Optional[...]`` noise down to a bare type.

    Same idea as ``spec.annotate._unwrap``, kept local so ``set_field`` doesn't
    reach into that module's private helpers. A discriminated union (more than
    one non-``None`` branch, e.g. ``ToolRef``) is returned as-is — callers that
    only care about a plain scalar type treat that as "not resolvable".
    """
    while hasattr(annotation, "__metadata__"):  # Annotated[T, ...]
        annotation = annotation.__origin__
    origin = get_origin(annotation)
    if origin is not None and (
        origin.__name__ == "UnionType" or str(origin) == "typing.Union"
    ):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _unwrap_annotation(args[0])
    return annotation


def _leaf_field_is_str(path: str) -> bool:
    """True if the dotted/indexed spec ``path`` resolves to a plain ``str`` field.

    Walks ``HarnessSpec``'s pydantic model the same way ``_walk_settable``
    walks the raw YAML dict — descending into nested models and, for a
    numeric segment, into a list field's item type. Anything this can't
    confidently resolve (an unknown segment, a discriminated union like
    ``tools.0`` or ``mcp_servers.0``, a catalog-defined builtin parameter)
    returns ``False``, which keeps the existing YAML-parsing behavior for
    those paths unchanged.
    """
    parts = [p for p in path.split(".") if p]
    if not parts:
        return False
    cls: Any = HarnessSpec
    for segment in parts:
        cls = _unwrap_annotation(cls)
        if get_origin(cls) is list:
            args = get_args(cls)
            if not segment.isdigit() or not args:
                return False
            cls = args[0]
            continue
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            return False
        fields = cls.model_fields
        if segment not in fields:
            return False
        cls = fields[segment].annotation
    return _unwrap_annotation(cls) is str


_STRING_COERCION_HINT = (
    "hint: the value is parsed as YAML, so text with YAML-significant "
    "characters (e.g. ': ', '- ', or a leading '{'/'[') can be misread as a "
    "mapping/list. Pass it via --file to use it verbatim instead."
)


# --------------------------------------------------------------------------- #
# set
# --------------------------------------------------------------------------- #
def set_field(
    directory: str | Path,
    path: str,
    value: str | None = None,
    file: str | Path | None = None,
) -> HarnessSpec:
    """Set a scalar/object field by dotted (and optionally indexed) path.

    Examples: ``loop.max_turns``, ``guardrails.0.value`` (read-modify-write of
    an existing list item; an out-of-range index is a clear ``SpecError``).

    ``value`` is normally parsed as a YAML scalar (so ``"30"`` becomes
    ``30``) — except when ``path`` resolves to a plain ``str`` field in the
    schema (e.g. ``system_prompt``), where the raw CLI text is kept verbatim
    so YAML-significant characters (``": "``, ...) can't turn it into a
    mapping. Use ``file`` to load the value from a text file either way.
    """
    directory = Path(directory)
    parsed_from_yaml = False
    if file is not None:
        parsed: Any = Path(file).read_text(encoding="utf-8")
    elif value is not None:
        if _leaf_field_is_str(path):
            parsed = value
        else:
            parsed = yaml.safe_load(value)
            parsed_from_yaml = True
    else:
        raise SpecError("set requires either a value or a --file")

    raw = load_raw(directory)
    parent, key = _walk_settable(raw, path)
    parent[key] = parsed

    try:
        return _commit(
            directory,
            raw,
            [],
            "set",
            {"path": path, "value": parsed if file is None else f"<{file}>"},
        )
    except SpecError as exc:
        if parsed_from_yaml and "valid string" in str(exc):
            raise SpecError(f"{exc}\n{_STRING_COERCION_HINT}") from exc
        raise


def set_model(directory: str | Path, selector: str) -> HarnessSpec:
    """Switch provider and model id together, from a ``provider/model-id`` selector.

    Both fields must move in one commit. Setting them one at a time can never
    work: `model.provider` and `model.id` validate against each other, so
    whichever is written first leaves the spec briefly inconsistent and the
    change is rolled back. This is the only supported way to move a harness
    between labs.

    The selector matches ``generate --model`` / ``evolve --model``, and splits
    on the FIRST ``/`` only — aggregator ids contain slashes of their own
    (``openrouter/deepseek/deepseek-r1``).
    """
    directory = Path(directory)
    provider, _, model_id = selector.partition("/")
    if not provider or not model_id:
        raise SpecError(
            f"expected 'provider/model-id' (got {selector!r}); "
            "run `hiveloom models` to list providers"
        )

    raw = load_raw(directory)
    model = raw.get("model")
    if not isinstance(model, dict):
        model = {}
        raw["model"] = model
    model["provider"] = provider
    model["id"] = model_id
    return _commit(
        directory, raw, [], "set_model", {"provider": provider, "id": model_id}
    )


def set_value(directory: str | Path, path: str, value: Any) -> HarnessSpec:
    """Set a dotted (and optionally indexed) field to an already-typed value.

    No YAML parsing — used by the generator and evolver, which supply native
    JSON values. See :func:`set_field` for the path syntax.
    """
    directory = Path(directory)
    raw = load_raw(directory)
    parent, key = _walk_settable(raw, path)
    parent[key] = value
    return _commit(directory, raw, [], "set", {"path": path})


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #
def add_tool(
    directory: str | Path,
    builtin: str | None = None,
    code: str | None = None,
    description: str | None = None,
    *,
    hosts: list[str] | None = None,
    **params: Any,
) -> HarnessSpec:
    """Add a tool. Exactly one of ``builtin`` / ``code`` must be given.

    ``params`` are the builtin's inline catalog parameters (``commands`` for
    ``shell``, ``limit`` for ``recall_runs``, …) and are validated against the
    catalog by the commit below. ``hosts`` is the pre-existing spelling of
    ``http_get``'s parameter and is kept for callers that already pass it.
    """
    directory = Path(directory)
    entry, created = _make_ref(
        directory, "tool", builtin, code, description, require_description=True
    )
    if hosts and builtin != "http_get":
        raise SpecError("hosts may only be declared for the http_get builtin")
    if builtin == "http_get" and hosts:
        entry["hosts"] = list(hosts)
    supplied = {k: v for k, v in params.items() if v is not None}
    if supplied and builtin is None:
        raise SpecError("parameters may only be declared for a builtin tool")
    entry.update(supplied)
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


def find_guardrails(directory: str | Path, builtin: str) -> list[dict[str, Any]]:
    """Return the raw guardrail entries in the spec that reference ``builtin``."""
    raw = load_raw(Path(directory))
    return [
        g
        for g in raw.get("guardrails") or []
        if isinstance(g, dict) and g.get("builtin") == builtin
    ]


def _apply_guardrail(entries: list[Any], entry: dict[str, Any]) -> None:
    """Add ``entry`` to ``entries`` in place, replacing what it supersedes.

    Singleton guardrails (per the catalog) allow only one meaningful entry, so a
    new one replaces any existing entries of that name — otherwise `init`'s
    default ``max_cost_usd`` would linger next to the one the caller just asked
    for. Guardrails that compose as a list (e.g. ``regex_output_filter``, one per
    pattern) only collapse exact duplicates, which are always no-ops.

    Superseded entries are collapsed to one, so adding to a spec that already
    carries duplicates cleans it up rather than adding to the pile.
    """
    catalog_entry = CATALOGS["guardrails"].get(entry["builtin"])
    singleton = catalog_entry is not None and catalog_entry.singleton
    superseded = [
        index
        for index, existing in enumerate(entries)
        if isinstance(existing, dict)
        and existing.get("builtin") == entry["builtin"]
        and (singleton or existing == entry)
    ]
    if not superseded:
        entries.append(entry)
        return
    entries[superseded[0]] = entry  # keep the original position
    for index in reversed(superseded[1:]):
        del entries[index]


def add_guardrail(
    directory: str | Path,
    builtin: str,
    value: Any = None,
    **params: Any,
) -> HarnessSpec:
    """Add a builtin guardrail, replacing an existing entry of the same name.

    Guardrails are code-hookable but usually builtin. See :func:`_apply_guardrail`
    for when adding replaces rather than appends.
    """
    directory = Path(directory)
    entry: dict[str, Any] = {"builtin": builtin}
    if value is not None:
        entry["value"] = value
    entry.update({k: v for k, v in params.items() if v is not None})
    raw = load_raw(directory)
    _apply_guardrail(raw.setdefault("guardrails", []), entry)
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


_MEMORY_SLUG_RE = re.compile(r"[^a-z0-9]+")


def memory_slug(title: str) -> str:
    """Derive a memory entry id from its title.

    Deterministic so the same lesson added twice collides on the id instead of
    quietly becoming two entries; the caller can always pass an explicit id.
    """
    slug = _MEMORY_SLUG_RE.sub("-", title.casefold()).strip("-")[:64].strip("-")
    if not slug or not slug[0].isalnum():
        raise SpecError(
            f"could not derive a memory id from title {title!r}; pass an explicit id "
            "(a-z, 0-9, dashes)"
        )
    return slug


def add_memory_entry(
    directory: str | Path,
    *,
    kind: str,
    title: str,
    content: str,
    source: str | None = None,
    evidence: str | None = None,
    entry_id: str | None = None,
) -> HarnessSpec:
    """Append a durable lesson to ``memory.entries``.

    The operator-side write path. It goes through :func:`_commit` like every
    other construction command, so the entry count, the entry length, and the
    rendered prompt budget are all enforced by the schema before anything is
    written — and an over-budget entry leaves ``harness.yaml`` untouched.
    """
    directory = Path(directory)
    raw = load_raw(directory)
    memory = raw.setdefault("memory", {})
    if not isinstance(memory, dict):
        raise SpecError("memory section is not a mapping; fix harness.yaml first")
    entries = memory.setdefault("entries", [])
    if not isinstance(entries, list):
        raise SpecError("memory.entries is not a list; fix harness.yaml first")

    new_id = entry_id or memory_slug(title)
    if any(isinstance(e, dict) and e.get("id") == new_id for e in entries):
        raise SpecError(
            f"memory entry '{new_id}' already exists; forget it first or use another id"
        )
    entry: dict[str, Any] = {
        "id": new_id,
        "kind": kind,
        "title": title,
        "content": content,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if source:
        entry["source"] = source
    if evidence:
        entry["evidence"] = evidence
    entries.append(entry)
    return _commit(directory, raw, [], "add_memory", {"id": new_id, "kind": kind})


def forget_memory_entry(directory: str | Path, entry_id: str) -> HarnessSpec:
    """Remove one durable lesson by id, validating and rolling back like any edit.

    Deliberately not part of :func:`remove_item`: memory entries key on ``id``
    rather than a builtin/code/name ref, and a bare `hiveloom remove <name>`
    must never reach into what the harness has learned.
    """
    directory = Path(directory)
    raw = load_raw(directory)
    memory = raw.get("memory")
    entries = memory.get("entries") if isinstance(memory, dict) else None
    kept = (
        [e for e in entries if not (isinstance(e, dict) and e.get("id") == entry_id)]
        if isinstance(entries, list)
        else []
    )
    if not isinstance(entries, list) or len(kept) == len(entries):
        raise SpecError(f"no memory entry with id '{entry_id}'")
    memory["entries"] = kept
    return _commit(directory, raw, [], "forget_memory", {"id": entry_id})


_PLAYBOOK_PROMPT_STUB = """# {name}

TODO: write the guidance that applies while the harness is in this playbook.
It is appended to the system prompt on entry, so keep it about *how to work in
this mode* — what to establish first, what to refuse, when to hand off to
another playbook.
"""


def add_playbook(
    directory: str | Path,
    *,
    name: str,
    description: str,
    prompt: str | None = None,
    tools: list[str] | None = None,
    validators: list[dict[str, Any]] | None = None,
    model: str | None = None,
    model_provider: str | None = None,
    on_enter: str | None = None,
    on_exit: str | None = None,
    entry: bool = False,
) -> HarnessSpec:
    """Add a playbook: scaffold its prompt (and any hooks) and list it in the spec.

    ``prompt`` defaults to ``playbooks/<name>.md``; pass an explicit path to
    reuse one. Hook refs are scaffolded like every other code hook, so a
    generated plan and a hand-typed CLI call land the same way.

    ``model``/``model_provider`` give the mode its own executor — profile on a
    cheap model, decide on an expensive one, inside one harness and one
    conversation. Both are frozen from evolution, like the harness-level model.
    """
    directory = Path(directory)
    created: list[Path] = []

    prompt_ref = prompt or f"playbooks/{name}.md"
    prompt_file = directory / prompt_ref
    if not prompt_file.exists():
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(
            _PLAYBOOK_PROMPT_STUB.format(name=name), encoding="utf-8"
        )
        created.append(prompt_file)

    for hook_ref in (on_enter, on_exit):
        if hook_ref:
            scaffolded = _scaffold_hook(
                directory, hook_ref, "playbook_hook", f"{name} playbook hook"
            )
            if scaffolded is not None:
                created.append(scaffolded)

    entry_dict: dict[str, Any] = {
        "name": name,
        "description": description,
        "prompt": prompt_ref,
    }
    if tools is not None:
        entry_dict["tools"] = list(tools)
    if validators:
        entry_dict["validators"] = [dict(v) for v in validators]
        for ref in validators:
            code_ref = ref.get("code")
            if code_ref:
                scaffolded = _scaffold_hook(
                    directory, code_ref, "validator", f"{name} playbook validator"
                )
                if scaffolded is not None:
                    created.append(scaffolded)
    if model:
        entry_dict["model"] = model
    if model_provider:
        entry_dict["model_provider"] = model_provider
    if on_enter:
        entry_dict["on_enter"] = on_enter
    if on_exit:
        entry_dict["on_exit"] = on_exit
    if entry:
        entry_dict["entry"] = True

    raw = load_raw(directory)
    playbooks = raw.setdefault("playbooks", [])
    if any(p.get("name") == name for p in playbooks):
        raise SpecError(f"playbook '{name}' is already listed in the spec")
    playbooks.append(entry_dict)
    return _commit(directory, raw, created, "add_playbook", {"name": name})


def add_mcp_server(
    directory: str | Path,
    *,
    name: str,
    stdio_command: str | None = None,
    stdio_args: list[str] | None = None,
    stdio_env: dict[str, str] | None = None,
    stdio_env_from_host: dict[str, str] | None = None,
    stdio_cwd: str | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    header_env: dict[str, str] | None = None,
    tools: list[str] | None = None,
    deferred: bool = False,
    timeout_seconds: float | None = None,
) -> HarnessSpec:
    """Add an MCP server (a stdio subprocess or a Streamable HTTP endpoint).

    Exactly one of ``stdio_command`` / ``url`` must be given.

    Unlike ``add tool --code``, there is no local file to import and validate
    here, so this makes NO live connection — a typo in the command or URL only
    surfaces later, at ``run``/``dry-run`` (which discover eagerly) or
    ``hiveloom mcp list-tools``.

    ``timeout_seconds`` covers connect/initialize and each tool call (default
    30s on the schema; ``> 0`` and ``<= 600``, validated there) — raise it for
    a slower peer harness/tool round trip.
    """
    if (stdio_command is None) == (url is None):
        raise SpecError("add mcp-server requires exactly one of --stdio-command or --url")

    directory = Path(directory)
    entry: dict[str, Any] = {"name": name}
    if stdio_command is not None:
        entry["transport"] = "stdio"
        entry["command"] = stdio_command
        if stdio_args:
            entry["args"] = list(stdio_args)
        if stdio_env:
            entry["env"] = dict(stdio_env)
        if stdio_env_from_host:
            entry["env_from_host_env"] = dict(stdio_env_from_host)
        if stdio_cwd is not None:
            entry["cwd"] = stdio_cwd
    else:
        entry["transport"] = "http"
        entry["url"] = url
        if headers:
            entry["headers"] = dict(headers)
        if header_env:
            entry["header_env"] = dict(header_env)
    if tools:
        entry["tools"] = list(tools)
    if deferred:
        entry["deferred"] = True
    if timeout_seconds is not None:
        entry["timeout_seconds"] = timeout_seconds

    raw = load_raw(directory)
    raw.setdefault("mcp_servers", []).append(entry)
    return _commit(
        directory, raw, [], "add_mcp_server", {"name": name, "transport": entry["transport"]}
    )


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
    "mcp_servers": ("mcp_servers",),
}


def remove_item(directory: str | Path, target: str) -> HarnessSpec:
    """Remove a tool/guardrail/validator by identifier, or delete a field path.

    ``target`` matches a builtin name or a ``path.py:function`` code ref in any
    of the list sections; failing that, it is treated as a dotted (optionally
    indexed, e.g. ``guardrails.0``) field path to delete (which reverts an
    object field to its default, or drops one item from a list).
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


def _iter_list_sections(raw: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], str]]:
    """Yield ``(dotted_root, container, key)`` per list section present in ``raw``."""
    for keys in _LIST_SECTIONS.values():
        container: Any = raw
        for key in keys[:-1]:
            container = container.get(key) if isinstance(container, dict) else None
            if container is None:
                break
        if not isinstance(container, dict):
            continue
        if isinstance(container.get(keys[-1]), list):
            yield ".".join(keys), container, keys[-1]


def matching_roots(raw: dict[str, Any], target: str) -> set[str]:
    """The spec roots :func:`remove_item` would remove a ``target`` entry from.

    Callers that gate removal on a spec path (the HTTP control plane refuses
    frozen roots) need to know what a bare name resolves to. Sharing
    ``_iter_list_sections`` with the removal itself means a new list section is
    covered the moment it is added to ``_LIST_SECTIONS`` — a caller cannot
    drift out of step with what removal actually touches.
    """
    return {
        root
        for root, container, key in _iter_list_sections(raw)
        if any(_ref_matches(item, target) for item in container[key])
    }


def _remove_from_lists(raw: dict[str, Any], target: str) -> bool:
    removed = False
    for _root, container, key in _iter_list_sections(raw):
        items = container[key]
        kept = [it for it in items if not _ref_matches(it, target)]
        if len(kept) != len(items):
            container[key] = kept
            removed = True
    return removed


def _ref_matches(item: Any, target: str) -> bool:
    if isinstance(item, str):  # skills are plain names
        return item == target
    if not isinstance(item, dict):
        return False
    # mcp_servers entries key on `name` rather than `builtin`/`code`; no other
    # list section uses that key, so checking it here is unambiguous.
    return (
        item.get("builtin") == target
        or item.get("code") == target
        or item.get("name") == target
    )


def _delete_path(raw: dict[str, Any], path: str) -> bool:
    resolved = _walk_removable(raw, path)
    if resolved is None:
        return False
    parent, key = resolved
    if isinstance(parent, list):
        del parent[key]
        return True
    if key in parent:
        del parent[key]
        return True
    return False
