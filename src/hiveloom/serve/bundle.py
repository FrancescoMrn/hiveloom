"""A harness as a bundle: ``{"files": {"<relative path>": "<utf-8 text>"}}``.

A bundle arrives over the network, so the worker that materializes it must
never execute caller-supplied code. :func:`check_bundle` admits only what runs
without it — builtin tools and validators, HTTP MCP servers, skills
(markdown) and schemas (JSON) — and refuses any path that could escape the
directory or reach the runtime's private state.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from hiveloom.errors import SpecError

HARNESS_FILE = "harness.yaml"
MAX_FILES = 512

_CODE_SUFFIXES = {".py", ".pyc", ".pyo", ".so", ".sh", ".pyd", ".dylib"}
_CODE_NAMES = {"pyproject.toml", "setup.py", "setup.cfg"}
_SKIPPED_DIRS = {".hiveloom", "__pycache__"}
_HOOK_STRING = re.compile(r"^\S+\.py:[A-Za-z_]\w*$")


class BundleError(SpecError):
    """A bundle the worker refuses to materialize (HTTP 422)."""


def _refused_path(path: str) -> str | None:
    """Why ``path`` may not appear in a bundle, or ``None`` when it may."""
    if not path or "\\" in path or "\x00" in path:
        return "must be a non-empty POSIX relative path"
    pure = PurePosixPath(path)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", path):
        return "is absolute"
    if any(part in ("..", "") for part in path.split("/")) or ".." in pure.parts:
        return "contains '..' or an empty segment"
    if pure.parts[0] == ".hiveloom" or any(p in _SKIPPED_DIRS for p in pure.parts):
        return "targets the runtime's private state"
    if any(part.startswith(".env") for part in pure.parts):
        return "targets an environment file"
    name = pure.name
    if pure.suffix.lower() in _CODE_SUFFIXES or name in _CODE_NAMES or (
        name.startswith("requirements") and name.endswith(".txt")
    ):
        return "is code or a dependency manifest; a bundle carries no code"
    return None


def _spec_refusals(raw: Any) -> list[str]:
    """What in the raw spec would make the worker run caller code."""
    if not isinstance(raw, dict):
        return ["harness.yaml must be a mapping"]
    problems: list[str] = []
    if raw.get("extensions"):
        problems.append("extensions are code; a bundle may not declare them")
    if raw.get("hooks"):
        problems.append("hooks are code; a bundle may not declare them")
    for server in raw.get("mcp_servers") or []:
        if not isinstance(server, dict) or server.get("transport") != "http":
            name = server.get("name") if isinstance(server, dict) else server
            problems.append(f"mcp server {name!r}: only transport http is allowed")
    for tool in raw.get("tools") or []:
        if isinstance(tool, dict) and tool.get("builtin") == "shell":
            problems.append("the shell tool may not be declared in a bundle")

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            if "code" in node:
                problems.append(f"{where or 'spec'}: code hooks are not allowed in a bundle")
            for key, value in node.items():
                walk(value, f"{where}.{key}" if where else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}.{index}")
        elif isinstance(node, str) and _HOOK_STRING.match(node.strip()):
            problems.append(f"{where}: code hooks are not allowed in a bundle")

    for key, value in raw.items():
        if key in ("system_prompt", "description", "memory"):
            continue  # free text, never a hook reference
        walk(value, str(key))
    return problems


def check_bundle(bundle: Any) -> dict[str, str]:
    """Return the bundle's files, or raise :class:`BundleError` naming every refusal."""
    files = bundle.get("files") if isinstance(bundle, dict) else None
    if not isinstance(files, dict):
        raise BundleError('a bundle is {"files": {"<relative path>": "<text>"}}')
    if len(files) > MAX_FILES:
        raise BundleError(f"a bundle carries at most {MAX_FILES} files")
    problems: list[str] = []
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            problems.append(f"{path!r}: paths and contents must be strings")
            continue
        reason = _refused_path(path)
        if reason:
            problems.append(f"{path!r} {reason}")
    if HARNESS_FILE not in files:
        problems.append(f"{HARNESS_FILE} is required")
    elif isinstance(files[HARNESS_FILE], str):
        try:
            raw = yaml.safe_load(files[HARNESS_FILE])
        except yaml.YAMLError as exc:
            problems.append(f"{HARNESS_FILE} is not valid YAML: {exc}")
        else:
            problems.extend(_spec_refusals(raw))
    if problems:
        raise BundleError("bundle refused:\n- " + "\n- ".join(problems))
    return dict(files)


def materialize(bundle: Any, directory: str | Path) -> Path:
    """Check ``bundle`` and write it into ``directory`` (created if missing)."""
    files = check_bundle(bundle)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise BundleError(f"{path!r} escapes the bundle directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def extract(directory: str | Path) -> dict[str, Any]:
    """Read a harness directory back into a bundle.

    Leaves out whatever :func:`check_bundle` would refuse by path — private
    state, caches, environment files, code and manifests (``init`` writes a
    ``pyproject.toml``) — and anything that is not UTF-8 text.
    """
    root = Path(directory).resolve()
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if _refused_path(rel.as_posix()):
            continue  # private state, caches, env files, code and manifests
        try:
            files[rel.as_posix()] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return {"files": files}
