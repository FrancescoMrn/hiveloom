"""The local harness registry: which harnesses this machine serves to agents.

``~/.hiveloom/registry.yaml`` (relocatable via ``$HIVELOOM_HOME``) holds the
list of harness directories the user has registered. It is the discovery
layer under ``hiveloom mcp serve --registered``: register a harness once and
every MCP-mounted agent session sees it, without repeating paths in agent
config. Entries are stored as resolved absolute paths; names, descriptions,
and health are derived from the spec at read time, so a renamed harness never
leaves a stale registry entry behind.

Registration is deliberately *not* trust: ``mcp serve`` still runs the same
per-directory trust gate as ``run``. Registering only says "offer this".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from hiveloom import paths
from hiveloom.errors import SpecError
from hiveloom.spec.loader import harness_path, load_spec


class RegisteredHarness(BaseModel):
    """One registry entry, resolved against the spec on disk."""

    path: str
    name: str = ""
    description: str = ""
    ok: bool = True
    error: str = ""


def registry_path() -> Path:
    return paths.hiveloom_home() / "registry.yaml"


def _load_paths() -> list[str]:
    path = registry_path()
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SpecError(f"malformed registry file {path}: {exc}") from exc
    entries = data.get("harnesses", [])
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise SpecError(f"malformed registry file {path}: 'harnesses' must be a list of paths")
    return entries


def _save_paths(entries: list[str]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"harnesses": entries}, sort_keys=False), encoding="utf-8"
    )


def register(directory: str | Path) -> RegisteredHarness:
    """Add a harness directory. Validates the spec now so a typo fails loudly."""
    base = harness_path(directory).parent.resolve()
    spec = load_spec(base / "harness.yaml")
    entries = _load_paths()
    if str(base) not in entries:
        entries.append(str(base))
        _save_paths(entries)
    return RegisteredHarness(path=str(base), name=spec.name, description=spec.description)


def unregister(target: str | Path) -> RegisteredHarness:
    """Remove an entry by directory path or by harness name."""
    entries = _load_paths()
    resolved = str(Path(target).expanduser().resolve())
    for entry in entries:
        if entry == resolved:
            _save_paths([e for e in entries if e != entry])
            return RegisteredHarness(path=entry)
    for item in registered():
        if item.name == str(target):
            _save_paths([e for e in entries if e != item.path])
            return item
    raise SpecError(f"'{target}' is not a registered harness path or name")


def registered() -> list[RegisteredHarness]:
    """Every entry with its live spec state; broken entries carry the error."""
    items: list[RegisteredHarness] = []
    for entry in _load_paths():
        try:
            spec = load_spec(harness_path(entry))
            items.append(
                RegisteredHarness(path=entry, name=spec.name, description=spec.description)
            )
        except Exception as exc:  # noqa: BLE001 - a broken entry must not hide the rest
            items.append(
                RegisteredHarness(
                    path=entry, ok=False, error=f"{type(exc).__name__}: {exc}"
                )
            )
    return items


def serveable() -> tuple[list[str], list[dict[str, Any]]]:
    """Registered paths fit to serve, plus ``{path, error}`` for the skipped."""
    good: list[str] = []
    skipped: list[dict[str, Any]] = []
    for item in registered():
        if item.ok:
            good.append(item.path)
        else:
            skipped.append({"path": item.path, "error": item.error})
    return good, skipped
