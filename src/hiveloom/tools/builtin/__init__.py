"""Builtin tools for v0: file_read, file_write, shell, http_get.

All builtins are sandboxed to the harness working directory (files) or an
allowlist (shell). ``shell`` is disabled unless the spec provides an allowlist.
``file_read``/``file_write`` are further refused the runtime-private path set
(``.hiveloom/``, ``.env*``, the trace dir, the Hive and user-level state) via
``safe_path`` — a model can no more read its own harness's auth store or
credentials through a tool call than an HTTP caller can through `input_file`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import shlex
import socket
import threading
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

from hiveloom import ext
from hiveloom.catalog import BUILTIN_TOOLS
from hiveloom.confine import ConfinementUnavailable, run_confined
from hiveloom.package import is_sensitive_path
from hiveloom.private import env_files, is_private
from hiveloom.spec.schema import BuiltinToolRef, ConfinementConfig
from hiveloom.tools.registry import Artifact, Tool, ToolError, ToolResult

_MAX_HTTP_BYTES = 200_000

# One lock per resolved target path (never dropped: paths per process are few
# and bounded by the working directory's file count).
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _path_write_lock(target: Path) -> threading.Lock:
    key = str(target)
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.Lock())
_BLOCKED_SHELL_BINARIES = {
    "bash", "dash", "env", "fish", "lua", "node", "perl", "php", "python", "python3",
    "ruby", "sh", "zsh",
}
_BLOCKED_SHELL_ARGUMENTS = {"-exec", "-execdir", "-delete"}
_EXTRA_ARGS_SAFE_BINARIES = {
    "diff", "echo", "grep", "head", "ls", "printf", "pwd", "sort", "tail", "uniq", "wc",
}


def safe_path(
    base: Path,
    path: str,
    *,
    trace_dir: Path | None = None,
    private_paths: list[Path] | None = None,
) -> Path:
    """Resolve ``path``, ensure it stays within ``base`` (no traversal), and
    refuse anything ``package.py`` would never ship or the runtime-private path
    resolver names (``.hiveloom/``, ``.env*`` except checked-in templates,
    VCS/cache noise, the configured trace directory, the Hive and user-level
    runtime state).

    Staying inside the harness directory is necessary but not sufficient:
    ``.hiveloom/`` (the trust store, construction log, and — for a served
    harness — its own auth store and every run's trace) and ``.env`` (a
    live ``ANTHROPIC_API_KEY`` in any real deployment) both live INSIDE it.
    This check is default-on here — the one chokepoint every caller
    (``file_read``/``file_write`` below, the evolver's code-change
    containment, the HTTP control plane's ``input_file``) already goes
    through for containment — so nothing has to remember a second check.
    ``trace_dir`` is optional only because a caller without a loaded spec
    (there is none today, but ``safe_path`` doesn't require one) has
    nothing to pass; every current caller resolves and supplies it, so a
    reconfigured (non-default) trace directory is covered everywhere, not
    just the default location under ``.hiveloom/``.
    """
    candidate = (base / path).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ToolError(f"path '{path}' escapes the working directory")
    rel = candidate.relative_to(base_resolved)
    if is_sensitive_path(rel, trace_dir=trace_dir) or (
        private_paths is not None
        and is_private(candidate, private_paths, base=base_resolved)
    ):
        raise ToolError(f"path '{path}' is protected harness state, not accessible here")
    return candidate


class FileReadTool(Tool):
    """Read a UTF-8 text file from the working directory."""

    def __init__(
        self,
        base: Path,
        *,
        trace_dir: Path | None = None,
        private_paths: list[Path] | None = None,
    ):
        self._base = base
        self._trace_dir = trace_dir
        self._private_paths = private_paths
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
        target = safe_path(
            self._base,
            path,
            trace_dir=self._trace_dir,
            private_paths=self._private_paths,
        )
        if not target.exists():
            raise ToolError(f"file not found: {path}")
        return target.read_text(encoding="utf-8")


def _file_state(target: Path) -> dict[str, Any] | None:
    """Size and sha256 of a file, or None when it does not exist yet."""
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class FileWriteTool(Tool):
    """Write a UTF-8 text file within the working directory."""

    def __init__(
        self,
        base: Path,
        *,
        trace_dir: Path | None = None,
        private_paths: list[Path] | None = None,
    ):
        self._base = base
        self._trace_dir = trace_dir
        self._private_paths = private_paths
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

    def run(self, path: str = "", content: str = "", **_: Any) -> ToolResult:
        target = safe_path(
            self._base,
            path,
            trace_dir=self._trace_dir,
            private_paths=self._private_paths,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        # Serialize writes per resolved path: with loop.tool_execution set to
        # parallel, two calls in one batch may target the same file.
        with _path_write_lock(target):
            previous = _file_state(target)
            target.write_text(content, encoding="utf-8")
            after = _file_state(target)

        # The model reads the string; the caller reads the artifact. A write is
        # both a deliverable ("here is the file this run produced") and a
        # mutation ("this path changed, from this content to that"), and
        # neither is recoverable from the result string.
        #
        # Hashes and sizes, not the content itself: a journal that inlined
        # every version of every written file would grow without bound, and the
        # new content is already in the call's `input`. What the before-hash
        # buys is the one thing the journal genuinely could not otherwise say —
        # whether this write changed anything, and what it replaced.
        return ToolResult(
            content=f"wrote {len(content)} chars to {path}",
            artifacts=[
                Artifact(
                    kind="file",
                    data={
                        "path": path,
                        "action": "created" if previous is None else "modified",
                        "unchanged": previous is not None and previous == after,
                        "bytes": after["bytes"],
                        "sha256": after["sha256"],
                        "previous": previous,
                    },
                )
            ],
        )


class LoadSkillTool(Tool):
    """Read one of the harness's declared skills in full.

    Progressive disclosure for harnesses that must not ship ``file_read``: a
    guardrailed agent — say one whose only data access is a constrained SQL
    tool — still gets pay-on-demand playbooks, but the reachable set is exactly
    the spec's ``skills:`` list rather than the working directory.
    """

    def __init__(self, base: Path, skills: list[str]):
        self._base = base
        self._skills = list(skills)
        entry = BUILTIN_TOOLS["load_skill"]
        self.name = "load_skill"
        self.description = entry.description + (
            f" Available: {', '.join(self._skills)}." if self._skills else ""
        )
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name exactly as listed in the system prompt.",
                    # Enumerated so a cheap executor cannot invent a name.
                    **({"enum": self._skills} if self._skills else {}),
                }
            },
            "required": ["name"],
        }

    def run(self, name: str = "", **_: Any) -> str:
        from hiveloom.errors import SpecError
        from hiveloom.skills import skill_body

        wanted = (name or "").strip()
        if wanted not in self._skills:
            available = ", ".join(self._skills) or "(none declared)"
            raise ToolError(f"unknown skill '{name}'. Available: {available}")
        try:
            return skill_body(self._base, wanted)
        except SpecError as exc:  # declared, but missing or malformed on disk
            raise ToolError(str(exc)) from exc


class ShellTool(Tool):
    """Run an allowlisted shell command (disabled without an allowlist).

    The allowlist answers *which* command may run; ``spec.confinement`` answers
    what it may do once it runs. Both are checked here, in that order, and the
    spawn itself goes through :func:`hiveloom.confine.run_confined`.
    """

    def __init__(
        self,
        base: Path,
        allowed: list[Any],
        confinement: Any = None,
        *,
        trace_root: Path | None = None,
        trace_dir: Path | None = None,
        private_paths: list[Path] | None = None,
    ):
        self._base = base
        self._allowed = [_parse_shell_rule(rule) for rule in allowed]
        self._confinement = confinement or ConfinementConfig()
        self._trace_dir = trace_dir
        # The runtime's own state is not part of the machine a spawned command
        # gets to see: the journal records every tool result in full, the spill
        # store beside it holds the ones too large to inline, and the Hive holds
        # every earlier run. One resolver decides what that set is — see
        # :mod:`hiveloom.private`.
        self._masked = private_paths or [base / ".hiveloom"]
        if trace_root is not None and trace_root not in self._masked:
            self._masked.append(trace_root)
        entry = BUILTIN_TOOLS["shell"]
        self.name = "shell"
        self.description = entry.description
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Command to run."}},
            "required": ["command"],
        }

    def _refuse_runtime_state(self, parts: list[str], masked: list[Path]) -> None:
        _refuse_runtime_state_impl(parts, self._base, masked, self._trace_dir)

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
        # Re-globbed per call: a `.env` written during the run is private from
        # the moment it exists, not from the next process start.
        masked = [*self._masked, *(p for p in env_files(self._base) if p not in self._masked)]
        self._refuse_runtime_state(parts, masked)
        try:
            result = run_confined(
                parts,
                cwd=self._base,
                config=self._confinement,
                mask=masked,
            )
        except ConfinementUnavailable as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError(f"command could not be started: {exc}") from exc

        notes = []
        if result.timed_out:
            notes.append(
                f"the command was killed after {self._confinement.timeout_seconds}s"
            )
        if result.truncated:
            notes.append(
                f"{result.discarded_bytes} bytes of output were dropped; the head "
                "and tail shown fit the configured budget"
            )
        suffix = f"\n[{'; '.join(notes)}]" if notes else ""
        return f"exit={result.returncode}\n{result.output}{suffix}"


def _refuse_runtime_state_impl(
    parts: list[str], base: Path, masked: list[Path], trace_dir: Path | None
) -> None:
    """Refuse a command that *names* a path holding the runtime's own state.

    The kernel mask is the real control, and it holds wherever a sandbox
    backend exists. This is the portable half: it catches the direct form
    (``grep secret .hiveloom/traces``) on a machine with no backend, using the
    same predicate ``file_read`` refuses paths with. It cannot catch a
    recursive walk that never names the directory — which is what
    ``confinement.mode: require`` is for.
    """
    root = base.resolve()
    hidden = [path.resolve() for path in masked]
    for argument in parts[1:]:
        if not argument or argument.startswith("-"):
            continue
        candidate = Path(argument)
        candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if any(candidate == path or path in candidate.parents for path in hidden):
            raise ToolError(
                f"'{argument}' is inside the harness's runtime state "
                "(.hiveloom / the trace directory), which the shell tool cannot read"
            )
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if relative.parts and is_sensitive_path(relative, trace_dir=trace_dir):
            raise ToolError(f"'{argument}' is a protected path in this harness")


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



def make_builtin_tool(
    ref: BuiltinToolRef,
    base: Path,
    *,
    trace_dir: Path | None = None,
    skills: list[str] | None = None,
    confinement: Any = None,
    trace_root: Path | None = None,
    private_paths: list[Path] | None = None,
) -> Tool:
    """Instantiate the catalog tool named by ``ref`` (builtin or extension)."""
    return ext.build(
        "tools",
        ref.builtin,
        ref.params(),
        ext.BuildContext(
            base=base,
            trace_dir=trace_dir,
            skills=list(skills or []),
            confinement=confinement,
            trace_root=trace_root,
            private_paths=list(private_paths or []),
        ),
    )


def _register_factories() -> None:
    ext.register_builtin_factory(
        "tools",
        "file_read",
        lambda _p, ctx: FileReadTool(
            ctx.base,
            trace_dir=ctx.trace_dir,
            private_paths=list(ctx.private_paths or []),
        ),
    )
    ext.register_builtin_factory(
        "tools",
        "file_write",
        lambda _p, ctx: FileWriteTool(
            ctx.base,
            trace_dir=ctx.trace_dir,
            private_paths=list(ctx.private_paths or []),
        ),
    )
    ext.register_builtin_factory(
        "tools", "load_skill", lambda _p, ctx: LoadSkillTool(ctx.base, ctx.skills)
    )
    ext.register_builtin_factory(
        "tools",
        "shell",
        lambda p, ctx: ShellTool(
            ctx.base,
            list(p.get("commands", []) or []),
            confinement=ctx.confinement,
            trace_root=ctx.trace_root,
            trace_dir=ctx.trace_dir,
            private_paths=list(ctx.private_paths or []),
        ),
    )
    ext.register_builtin_factory("tools", "http_get", lambda _p, ctx: HttpGetTool(ctx.base))


_register_factories()
