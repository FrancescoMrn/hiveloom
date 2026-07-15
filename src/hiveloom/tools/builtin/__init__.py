"""Builtin tools for v0: file_read, file_write, shell, http_get.

All builtins are sandboxed to the harness working directory (files) or an
allowlist (shell). ``shell`` is disabled unless the spec provides an allowlist.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from hiveloom import ext
from hiveloom.catalog import BUILTIN_TOOLS
from hiveloom.spec.schema import BuiltinToolRef
from hiveloom.tools.registry import Tool, ToolError

_MAX_HTTP_BYTES = 200_000


def _safe_path(base: Path, path: str) -> Path:
    """Resolve ``path`` and ensure it stays within ``base`` (no traversal)."""
    candidate = (base / path).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ToolError(f"path '{path}' escapes the working directory")
    return candidate


class FileReadTool(Tool):
    """Read a UTF-8 text file from the working directory."""

    def __init__(self, base: Path):
        self._base = base
        entry = BUILTIN_TOOLS["file_read"]
        self.name = "file_read"
        self.description = entry.description
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative file path."}},
            "required": ["path"],
        }

    def run(self, path: str = "", **_: Any) -> str:
        target = _safe_path(self._base, path)
        if not target.exists():
            raise ToolError(f"file not found: {path}")
        return target.read_text(encoding="utf-8")


class FileWriteTool(Tool):
    """Write a UTF-8 text file within the working directory."""

    def __init__(self, base: Path):
        self._base = base
        entry = BUILTIN_TOOLS["file_write"]
        self.name = "file_write"
        self.description = entry.description
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path."},
                "content": {"type": "string", "description": "File contents to write."},
            },
            "required": ["path", "content"],
        }

    def run(self, path: str = "", content: str = "", **_: Any) -> str:
        target = _safe_path(self._base, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"


class ShellTool(Tool):
    """Run an allowlisted shell command (disabled without an allowlist)."""

    def __init__(self, base: Path, allowed: list[str]):
        self._base = base
        self._allowed = set(allowed)
        entry = BUILTIN_TOOLS["shell"]
        self.name = "shell"
        self.description = entry.description
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Command to run."}},
            "required": ["command"],
        }

    def run(self, command: str = "", **_: Any) -> str:
        if not self._allowed:
            raise ToolError("shell is disabled: no command allowlist configured")
        parts = shlex.split(command)
        if not parts:
            raise ToolError("empty command")
        if parts[0] not in self._allowed:
            raise ToolError(f"command '{parts[0]}' is not in the allowlist")
        try:
            proc = subprocess.run(
                parts,
                cwd=self._base,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
            raise ToolError("command timed out") from exc
        out = proc.stdout + proc.stderr
        return f"exit={proc.returncode}\n{out}"


class HttpGetTool(Tool):
    """Perform an HTTP GET and return the (truncated) response body."""

    def __init__(self, base: Path):
        entry = BUILTIN_TOOLS["http_get"]
        self.name = "http_get"
        self.description = entry.description
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to fetch."}},
            "required": ["url"],
        }

    def run(self, url: str = "", **_: Any) -> str:
        if not url.startswith(("http://", "https://")):
            raise ToolError("url must start with http:// or https://")
        # Identify ourselves: many APIs (e.g. Wikipedia) 403 urllib's default UA.
        from hiveloom import __version__

        request = urlrequest.Request(
            url, headers={"User-Agent": f"hiveloom/{__version__} (+https://pypi.org/project/hiveloom)"}
        )
        try:
            with urlrequest.urlopen(request, timeout=30) as resp:  # noqa: S310 - scheme checked
                body = resp.read(_MAX_HTTP_BYTES)
        except (urlerror.URLError, ValueError) as exc:
            raise ToolError(f"http_get failed: {exc}") from exc
        return body.decode("utf-8", errors="replace")


def make_builtin_tool(ref: BuiltinToolRef, base: Path) -> Tool:
    """Instantiate the catalog tool named by ``ref`` (builtin or extension)."""
    return ext.build("tools", ref.builtin, ref.params(), ext.BuildContext(base=base))


def _register_factories() -> None:
    ext.register_builtin_factory("tools", "file_read", lambda _p, ctx: FileReadTool(ctx.base))
    ext.register_builtin_factory("tools", "file_write", lambda _p, ctx: FileWriteTool(ctx.base))
    ext.register_builtin_factory(
        "tools", "shell", lambda p, ctx: ShellTool(ctx.base, list(p.get("commands", []) or []))
    )
    ext.register_builtin_factory("tools", "http_get", lambda _p, ctx: HttpGetTool(ctx.base))


_register_factories()
