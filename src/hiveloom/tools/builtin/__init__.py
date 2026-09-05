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
from hiveloom.confine import (
    NONE,
    PORTABLE_VARIABLE_ARGV,
    ConfinementUnavailable,
    resolve_backend,
    run_confined,
)
from hiveloom.package import is_sensitive_path
from hiveloom.private import RunBoundary, env_files, is_private
from hiveloom.spec.schema import BuiltinToolRef, ConfinementConfig
from hiveloom.tools.registry import Artifact, Tool, ToolError, ToolResult

_MAX_HTTP_BYTES = 200_000

#: Ceilings on one ``recall_runs`` call. Prior runs compete with the task for
#: context, so the spec's `limit` is itself capped and each field is clipped.
_RECALL_MAX_LIMIT = 10
_RECALL_FIELD_CHARS = 1200

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
        run_boundary: RunBoundary | None = None,
    ):
        self._base = base
        self._trace_dir = trace_dir
        self._private_paths = private_paths
        self._run_boundary = run_boundary
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
            private_paths=(
                self._run_boundary.private_paths()
                if self._run_boundary is not None
                else self._private_paths
            ),
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
        run_boundary: RunBoundary | None = None,
    ):
        self._base = base
        self._trace_dir = trace_dir
        self._private_paths = private_paths
        self._run_boundary = run_boundary
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
            private_paths=(
                self._run_boundary.private_paths()
                if self._run_boundary is not None
                else self._private_paths
            ),
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
        run_boundary: RunBoundary | None = None,
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
        self._run_boundary = run_boundary
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
        _refuse_runtime_state_impl(
            parts,
            self._base,
            masked,
            self._trace_dir,
            protect_ancestors=resolve_backend(self._confinement) == NONE,
        )

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
        matched = next(
            (
                (argv, allow_extra)
                for argv, allow_extra in self._allowed
                if _matches_shell_rule(parts, argv, allow_extra)
            ),
            None,
        )
        if matched is None:
            raise ToolError(f"command '{parts[0]}' is not in the allowlist")
        if (
            matched[1]
            and parts[0] not in PORTABLE_VARIABLE_ARGV
            and resolve_backend(self._confinement) == NONE
        ):
            raise ToolError(
                f"variable arguments for '{parts[0]}' require an OS sandbox; "
                "without one, declare each permitted argv exactly"
            )
        # Re-globbed per call: a `.env` written during the run is private from
        # the moment it exists, not from the next process start.
        masked = (
            self._run_boundary.private_paths()
            if self._run_boundary is not None
            else [
                *self._masked,
                *(p for p in env_files(self._base) if p not in self._masked),
            ]
        )
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
    parts: list[str],
    base: Path,
    masked: list[Path],
    trace_dir: Path | None,
    *,
    protect_ancestors: bool = False,
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
    recursive_reader = _is_recursive_reader(parts)
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
        traverses_private = any(
            candidate == path or candidate in path.parents for path in hidden
        )
        if protect_ancestors and recursive_reader and traverses_private:
            raise ToolError(
                f"'{argument}' would recursively traverse the harness's runtime state"
            )
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if relative.parts and is_sensitive_path(relative, trace_dir=trace_dir):
            raise ToolError(f"'{argument}' is a protected path in this harness")


def _is_recursive_reader(parts: list[str]) -> bool:
    """Recognize builtin commands whose path arguments walk descendants."""
    command = Path(parts[0]).name
    flags = set(parts[1:])
    if command in {"find", "rg", "ripgrep", "tree"}:
        return True
    if command == "grep":
        return bool(flags & {"-r", "-R", "--recursive"}) or any(
            flag.startswith("-") and "r" in flag.casefold().lstrip("-")
            for flag in flags
        )
    if command == "ls":
        return bool(flags & {"-R", "--recursive"}) or any(
            flag.startswith("-") and "R" in flag.lstrip("-") for flag in flags
        )
    return False


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

    def __init__(self, base: Path, hosts: list[str] | None = None):
        del base
        self._hosts = _normalize_http_hosts(hosts or [])
        self._approved_hosts: set[str] = set()
        entry = BUILTIN_TOOLS["http_get"]
        self.name = "http_get"
        declared = ", ".join(self._hosts) or "none"
        self.description = (
            f"{entry.description} Pre-approved hosts: {declared}; any other host "
            "requires an operator decision for this run."
        )
        self.tags = list(entry.tags)
        self.input_schema = {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to fetch."}},
            "required": ["url"],
        }

    def run(self, url: str = "", **_: Any) -> str:
        _validate_public_http_url(url, self.allowed_hosts)
        # Identify ourselves: many APIs (e.g. Wikipedia) 403 urllib's default UA.
        from hiveloom import __version__

        request = urlrequest.Request(
            url, headers={"User-Agent": f"hiveloom/{__version__} (+https://pypi.org/project/hiveloom)"}
        )
        try:
            opener = urlrequest.build_opener(_SafeRedirectHandler(self.allowed_hosts))
            with opener.open(request, timeout=30) as resp:  # noqa: S310 - validated URL and redirects
                body = resp.read(_MAX_HTTP_BYTES)
        except (urlerror.URLError, ValueError) as exc:
            raise ToolError(f"http_get failed: {exc}") from exc
        return body.decode("utf-8", errors="replace")

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """Static and operator-approved destinations for this run."""
        return (*self._hosts, *sorted(self._approved_hosts))

    def network_destination(self, tool_input: dict[str, Any]) -> str:
        """Return the hostname whose authority this call requests."""
        parsed = urlsplit(str(tool_input.get("url", "")))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ToolError("url must be an absolute http:// or https:// URL")
        return parsed.hostname.rstrip(".").casefold()

    def destination_allowed(self, hostname: str) -> bool:
        return _host_allowed(hostname, self.allowed_hosts)

    def approve_destination(self, hostname: str) -> None:
        self._approved_hosts.add(hostname.rstrip(".").casefold())


def _normalize_http_hosts(hosts: list[str]) -> tuple[str, ...]:
    """Validate and normalize an HTTP destination allowlist."""
    normalized: list[str] = []
    for raw in hosts:
        host = str(raw).strip().rstrip(".").casefold()
        bare = host[2:] if host.startswith("*.") else host
        if not bare or ":" in bare or "/" in bare or bare.startswith("."):
            raise ToolError(f"invalid http_get host allowlist entry: {raw!r}")
        normalized.append(host)
    return tuple(dict.fromkeys(normalized))


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    hostname = host.rstrip(".").casefold()
    return any(
        hostname == rule
        or (rule.startswith("*.") and hostname.endswith(rule[1:]) and hostname != rule[2:])
        for rule in allowed
    )


def _validate_public_http_url(
    url: str, allowed_hosts: tuple[str, ...] | None = None
) -> None:
    """Reject non-HTTP URLs and destinations outside the public internet."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("url must be an absolute http:// or https:// URL")
    if allowed_hosts is not None and not _host_allowed(parsed.hostname, allowed_hosts):
        raise ToolError(f"url host '{parsed.hostname}' is not declared by this harness")
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

    def __init__(self, allowed_hosts: tuple[str, ...]):
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl, self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)



class RecallRunsTool(Tool):
    """Look up this harness's own prior runs in the Hive (opt-in).

    The evolver already reads run history *between* runs; this makes the same
    evidence available *during* one, on request. A small executor benefits
    disproportionately: a worked example of the same task from the same harness
    is worth more than another paragraph of instruction, and a past failure
    carries the verifier feedback that rejected it.

    Two boundaries make that safe to hand a model:

    * **Own history only.** Every query is keyed on the running harness's own
      key, taken from the run context rather than from tool input, so a model
      cannot name another harness — or another harness's customer data.
    * **Already redacted.** The Hive is ingested from journals, and
      ``logging.redact`` is applied before a journal is written, so recall can
      only return what the record already kept.

    Recall is disabled for a run whose Hive is unreachable rather than being an
    error the model has to reason about: history is an aid, never a dependency.
    """

    def __init__(
        self,
        *,
        limit: int = 3,
        include_output: bool = True,
        scope: str = "harness",
    ):
        entry = BUILTIN_TOOLS["recall_runs"]
        self.name = "recall_runs"
        self.description = entry.description
        self.tags = list(entry.tags)
        self.guidelines = (
            "recall_runs returns this harness's own earlier runs. Prefer it over "
            "guessing at house style or output shape: a past success shows what "
            "was accepted, a past failure shows what the validators rejected and "
            "why. It never returns another harness's runs."
        )
        self._limit = max(1, min(int(limit), _RECALL_MAX_LIMIT))
        self._include_output = bool(include_output)
        if scope not in ("harness", "version"):
            raise ToolError(
                f"recall_runs scope must be 'harness' or 'version' (got {scope!r})"
            )
        self._scope = scope
        self.wants_run_context = True
        self.input_schema = {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["success", "failed", "any"],
                    "description": (
                        "'success' for worked examples (default), 'failed' for runs "
                        "the validators rejected and why, 'any' for both."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Only runs whose task or output contains this text.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": f"Runs to return (at most {self._limit}).",
                },
            },
        }

    def run(
        self,
        status: str = "success",
        query: str = "",
        limit: Any = None,
        run_context: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        context = run_context or {}
        harness_key = context.get("harness_id") or context.get("harness_name") or ""
        if not harness_key:
            raise ToolError("recall_runs has no harness identity to scope the lookup to")
        if status not in ("success", "failed", "any"):
            raise ToolError(
                f"status must be 'success', 'failed' or 'any' (got {status!r})"
            )
        try:
            wanted = self._limit if limit in (None, "") else int(limit)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"limit must be a number (got {limit!r})") from exc

        from hiveloom.logging.hive import Hive  # local import: sqlite only when used

        try:
            with Hive(context.get("hive_path")) as hive:
                runs = hive.recall(
                    harness_key,
                    status=status,
                    query=query or None,
                    version=(
                        context.get("harness_version_hash")
                        if self._scope == "version"
                        else None
                    ),
                    limit=max(1, min(wanted, self._limit)),
                    exclude_run_id=context.get("run_id"),
                )
        except Exception as exc:  # noqa: BLE001 - history is an aid, not a dependency
            raise ToolError(f"the run history is unavailable: {exc}") from exc

        if not runs:
            scope = "this harness version" if self._scope == "version" else "this harness"
            hint = f" matching '{query}'" if query else ""
            return f"no earlier {status} runs of {scope}{hint} are recorded yet"
        return "\n\n".join(self._render(run) for run in runs)

    def _render(self, run: dict[str, Any]) -> str:
        lines = [
            f"{run.get('run_id', '?')} · {run.get('status', '?')} · "
            f"{run.get('finished_at') or 'unfinished'} · "
            f"v{run.get('harness_version_hash', '?')}"
        ]
        task = (run.get("task") or "").strip()
        if task:
            lines.append(f"task: {_clip(task, _RECALL_FIELD_CHARS)}")
        if run.get("status") == "success":
            if self._include_output:
                output = (run.get("output") or "").strip()
                lines.append(
                    f"output: {_clip(output, _RECALL_FIELD_CHARS)}"
                    if output
                    else "output: (not recorded)"
                )
        else:
            reason = (run.get("reason") or "").strip()
            if reason:
                lines.append(f"reason: {_clip(reason, _RECALL_FIELD_CHARS)}")
            for verdict in run.get("failed_verifications", []):
                lines.append(
                    f"rejected by {verdict.get('verifier', '?')}: "
                    f"{_clip((verdict.get('feedback') or '').strip(), _RECALL_FIELD_CHARS)}"
                )
            for trigger in run.get("guardrail_triggers", []):
                lines.append(
                    f"guardrail {trigger.get('guardrail', '?')} "
                    f"({trigger.get('kind', '?')}): {trigger.get('reason', '')}"
                )
        return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [+{len(text) - limit} chars]"


def make_builtin_tool(
    ref: BuiltinToolRef,
    base: Path,
    *,
    trace_dir: Path | None = None,
    skills: list[str] | None = None,
    confinement: Any = None,
    trace_root: Path | None = None,
    private_paths: list[Path] | None = None,
    run_boundary: RunBoundary | None = None,
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
            run_boundary=run_boundary,
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
            run_boundary=ctx.run_boundary,
        ),
    )
    ext.register_builtin_factory(
        "tools",
        "file_write",
        lambda _p, ctx: FileWriteTool(
            ctx.base,
            trace_dir=ctx.trace_dir,
            private_paths=list(ctx.private_paths or []),
            run_boundary=ctx.run_boundary,
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
            run_boundary=ctx.run_boundary,
        ),
    )
    ext.register_builtin_factory(
        "tools", "http_get", lambda p, ctx: HttpGetTool(ctx.base, list(p.get("hosts", [])))
    )
    ext.register_builtin_factory(
        "tools",
        "recall_runs",
        lambda p, _ctx: RecallRunsTool(
            limit=p.get("limit", 3),
            include_output=p.get("include_output", True),
            scope=p.get("scope", "harness"),
        ),
    )


_register_factories()
