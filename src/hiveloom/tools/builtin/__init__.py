"""Builtin tools for v0: file_read, file_write, shell, http_get.

All builtins are sandboxed to the harness working directory (files) or an
allowlist (shell). ``shell`` is disabled unless the spec provides an allowlist.
``file_read``/``file_write`` are further refused ``package.py``'s "never
leaves the harness" paths (``.hiveloom/``, ``.env*``, the trace dir) via
``_safe_path`` — a model can no more read its own harness's auth store or
credentials through a tool call than an HTTP caller can through `input_file`.
"""

from __future__ import annotations

import ipaddress
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

from hiveloom import ext
from hiveloom.catalog import BUILTIN_TOOLS
from hiveloom.package import is_sensitive_path
from hiveloom.spec.schema import BuiltinToolRef
from hiveloom.tools.registry import Tool, ToolError

_MAX_HTTP_BYTES = 200_000
_BLOCKED_SHELL_BINARIES = {
    "bash", "dash", "env", "fish", "lua", "node", "perl", "php", "python", "python3",
    "ruby", "sh", "zsh",
}
_BLOCKED_SHELL_ARGUMENTS = {"-exec", "-execdir", "-delete"}
_EXTRA_ARGS_SAFE_BINARIES = {
    "diff", "echo", "grep", "head", "ls", "printf", "pwd", "sort", "tail", "uniq", "wc",
}


def _safe_path(base: Path, path: str, *, trace_dir: Path | None = None) -> Path:
    """Resolve ``path``, ensure it stays within ``base`` (no traversal), and
    refuse anything ``package.py`` would never ship either (``.hiveloom/``,
    ``.env*`` except checked-in templates, VCS/cache noise, and — when the
    caller supplies it — the configured trace directory).

    Staying inside the harness directory is necessary but not sufficient:
    ``.hiveloom/`` (the trust store, construction log, and — for a served
    harness — its own auth store and every run's trace) and ``.env`` (a
    live ``ANTHROPIC_API_KEY`` in any real deployment) both live INSIDE it.
    This check is default-on here — the one chokepoint every caller
    (``file_read``/``file_write`` below, the evolver's code-change
    containment, the HTTP control plane's ``input_file``) already goes
    through for containment — so nothing has to remember a second check.
    ``trace_dir`` is optional only because a caller without a loaded spec
    (there is none today, but ``_safe_path`` doesn't require one) has
    nothing to pass; every current caller resolves and supplies it, so a
    reconfigured (non-default) trace directory is covered everywhere, not
    just the default location under ``.hiveloom/``.
    """
    candidate = (base / path).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ToolError(f"path '{path}' escapes the working directory")
    rel = candidate.relative_to(base_resolved)
    if is_sensitive_path(rel, trace_dir=trace_dir):
        raise ToolError(f"path '{path}' is protected harness state, not accessible here")
    return candidate


class FileReadTool(Tool):
    """Read a UTF-8 text file from the working directory."""

    def __init__(self, base: Path, *, trace_dir: Path | None = None):
        self._base = base
        self._trace_dir = trace_dir
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
        target = _safe_path(self._base, path, trace_dir=self._trace_dir)
        if not target.exists():
            raise ToolError(f"file not found: {path}")
        return target.read_text(encoding="utf-8")


class FileWriteTool(Tool):
    """Write a UTF-8 text file within the working directory."""

    def __init__(self, base: Path, *, trace_dir: Path | None = None):
        self._base = base
        self._trace_dir = trace_dir
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
        target = _safe_path(self._base, path, trace_dir=self._trace_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"


class ShellTool(Tool):
    """Run an allowlisted shell command (disabled without an allowlist)."""

    def __init__(self, base: Path, allowed: list[Any]):
        self._base = base
        self._allowed = [_parse_shell_rule(rule) for rule in allowed]
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
        if parts[0] in _BLOCKED_SHELL_BINARIES:
            raise ToolError(f"command '{parts[0]}' is not permitted in the shell tool")
        if any(arg in _BLOCKED_SHELL_ARGUMENTS for arg in parts[1:]):
            raise ToolError("command includes a dangerous shell argument")
        permitted = any(
            _matches_shell_rule(parts, argv, allow_extra) for argv, allow_extra in self._allowed
        )
        if not permitted:
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


def _parse_shell_rule(rule: Any) -> tuple[list[str], bool]:
    """Normalize a strict legacy command or a structured argv rule."""
    if isinstance(rule, str):
        argv = shlex.split(rule)
        allow_extra = False
    elif isinstance(rule, dict):
        argv = rule.get("argv")
        allow_extra = rule.get("allow_extra_args", False)
    else:
        raise ToolError("shell command rules must be strings or mappings")
    valid_argv = isinstance(argv, list) and argv and all(
        isinstance(arg, str) and arg for arg in argv
    )
    if not valid_argv:
        raise ToolError("shell command rules need a non-empty argv list")
    if not isinstance(allow_extra, bool):
        raise ToolError("shell command rule allow_extra_args must be boolean")
    if allow_extra and argv[0] not in _EXTRA_ARGS_SAFE_BINARIES:
        raise ToolError(
            f"shell rule for '{argv[0]}' cannot allow arbitrary extra arguments"
        )
    return argv, allow_extra


def _matches_shell_rule(parts: list[str], argv: list[str], allow_extra: bool) -> bool:
    return parts[: len(argv)] == argv and (allow_extra or len(parts) == len(argv))


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
        _validate_public_http_url(url)
        # Identify ourselves: many APIs (e.g. Wikipedia) 403 urllib's default UA.
        from hiveloom import __version__

        request = urlrequest.Request(
            url, headers={"User-Agent": f"hiveloom/{__version__} (+https://pypi.org/project/hiveloom)"}
        )
        try:
            opener = urlrequest.build_opener(_SafeRedirectHandler())
            with opener.open(request, timeout=30) as resp:  # noqa: S310 - validated URL and redirects
                body = resp.read(_MAX_HTTP_BYTES)
        except (urlerror.URLError, ValueError) as exc:
            raise ToolError(f"http_get failed: {exc}") from exc
        return body.decode("utf-8", errors="replace")


def _validate_public_http_url(url: str) -> None:
    """Reject non-HTTP URLs and destinations outside the public internet."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("url must be an absolute http:// or https:// URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"could not resolve host '{parsed.hostname}'") from exc
    if not addresses:
        raise ToolError(f"could not resolve host '{parsed.hostname}'")
    for _family, _socktype, _proto, _canonname, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            raise ToolError(f"url host '{parsed.hostname}' resolves to a non-public address")


class _SafeRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Limit and validate each redirect before urllib follows it."""

    max_repeats = 3
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_builtin_tool(ref: BuiltinToolRef, base: Path, *, trace_dir: Path | None = None) -> Tool:
    """Instantiate the catalog tool named by ``ref`` (builtin or extension)."""
    return ext.build(
        "tools", ref.builtin, ref.params(), ext.BuildContext(base=base, trace_dir=trace_dir)
    )


def _register_factories() -> None:
    ext.register_builtin_factory(
        "tools", "file_read", lambda _p, ctx: FileReadTool(ctx.base, trace_dir=ctx.trace_dir)
    )
    ext.register_builtin_factory(
        "tools", "file_write", lambda _p, ctx: FileWriteTool(ctx.base, trace_dir=ctx.trace_dir)
    )
    ext.register_builtin_factory(
        "tools", "shell", lambda p, ctx: ShellTool(ctx.base, list(p.get("commands", []) or []))
    )
    ext.register_builtin_factory("tools", "http_get", lambda _p, ctx: HttpGetTool(ctx.base))


_register_factories()
