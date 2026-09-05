"""OS-level confinement for the processes the runtime spawns.

Two builtins start subprocesses: the ``shell`` tool (argv the *model* chose,
from a spec allowlist) and the ``command_succeeds`` validator (a command the
*spec* chose). Both previously ran with the full permissions, environment, and
lifetime of the hiveloom process. Allowlisting which command may run is a
different question from what that command may then do, and only the first was
answered.

This module answers the second. Every spawn goes through :func:`run_confined`,
which applies two layers:

**A portable baseline, always.** A scrubbed environment (the process's own
credentials are not inherited — an allowlisted command cannot read the API key
that pays for the run), closed stdin, its own session so a timeout kills the
whole process tree rather than orphaning grandchildren, POSIX resource limits,
and output drained through bounded head/tail collectors so a runaway writer
cannot exhaust the parent's memory or disk.

**A kernel sandbox where the platform has one.** ``bwrap`` (bubblewrap) on
Linux and ``sandbox-exec`` on macOS both give the same shape: the filesystem
readable but read-only except the harness directory and a private ``/tmp``, and
— unless ``network: true`` — no network at all. Availability is probed once and
cached, because the answer cannot change within a run.

``confinement.mode`` makes the OS layer optional: ``auto`` (default) uses a
backend when one is available and otherwise runs with the baseline alone,
``require`` refuses to spawn without one, and ``off`` always skips it. The
resolved backend is recorded in ``run_started``, so a journal states whether
the run's processes were actually isolated rather than implying it.

What this is *not*: a boundary around hiveloom itself. Code hooks, extensions,
and MCP servers run in the hiveloom process (or as its children, unconfined) by
design — they are the harness author's own code, gated by the trust prompt.
See `docs/task-confinement.md`.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveloom.errors import HiveloomError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.spec.schema import ConfinementConfig

try:  # pragma: no cover - platform dependent
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

#: Environment variables a confined process starts with. Everything else is
#: dropped: a spawned command has no business reading the credentials, cloud
#: tokens, or CI secrets that happen to be in the runtime's environment, and
#: an allowlist is the only form of that rule that stays true as new secrets
#: are invented.
_BASE_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin"

#: Backends, strongest first. Each entry is (name, probe argv, wrap builder).
BWRAP = "bwrap"
SANDBOX_EXEC = "sandbox-exec"
NONE = "none"

_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: dict[str, bool] = {}

# Commands whose variable arguments cannot discover new local data or open a
# network connection. Shared with the shell tool and the risk report so the
# enforced rule and the reported rule cannot drift.
PORTABLE_VARIABLE_ARGV = frozenset({"echo", "printf"})


def _decode(data: bytes) -> str:
    """Lossy decode — a byte boundary may cut a multi-byte character in half."""
    return data.decode("utf-8", errors="replace")


class ConfinementUnavailable(HiveloomError):
    """Raised when ``mode: require`` cannot be satisfied on this machine."""


@dataclass(frozen=True)
class ConfinedResult:
    """What a confined process produced, and how it was confined."""

    returncode: int
    stdout: str
    stderr: str
    backend: str
    timed_out: bool = False
    truncated: bool = False
    #: Bytes the command wrote past the budget: read, counted, and dropped.
    discarded_bytes: int = 0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


# --------------------------------------------------------------------------- #
# Backend discovery
# --------------------------------------------------------------------------- #
def _probe(name: str, argv: list[str]) -> bool:
    """Run a backend's smallest possible success case, once per process."""
    with _PROBE_LOCK:
        if name in _PROBE_CACHE:
            return _PROBE_CACHE[name]
    ok = False
    if shutil.which(argv[0]) is not None:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, timeout=10, check=False
            )
            ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    with _PROBE_LOCK:
        _PROBE_CACHE[name] = ok
    return ok


def _bwrap_available() -> bool:
    return _probe(
        BWRAP,
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--", "true"],
    )


def _sandbox_exec_available() -> bool:
    return _probe(
        SANDBOX_EXEC, ["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]
    )


def available_backend() -> str:
    """The strongest sandbox this machine can actually run, or ``none``."""
    system = platform.system()
    if system == "Linux" and _bwrap_available():
        return BWRAP
    if system == "Darwin" and _sandbox_exec_available():
        return SANDBOX_EXEC
    return NONE


def resolve_backend(config: ConfinementConfig) -> str:
    """The backend a spawn under ``config`` would use. ``off`` means baseline only."""
    if config.mode == "off":
        return NONE
    return available_backend()


def describe(config: ConfinementConfig) -> dict[str, Any]:
    """A record of how this machine will confine spawned processes.

    Written into ``run_started`` and printed by ``hiveloom confinement``: the
    same question ("is this actually enforced here?") asked ahead of a run and
    answered after one. Every field states an *outcome*, not an intention —
    the baseline alone is never reported as isolation of anything.
    """
    backend = resolve_backend(config)
    sandboxed = backend != NONE
    return {
        "mode": config.mode,
        "backend": backend,
        "filesystem_isolated": sandboxed,
        # The one that matters for the exfiltration path: can a spawned process
        # reach .hiveloom, the journal, the spill store, the Hive?
        "runtime_state_hidden": sandboxed,
        "network_isolated": sandboxed and not config.network,
        "home_hidden": sandboxed and config.hide_home,
        "limits_applied": resource is not None,
        "platform": platform.system(),
    }


def risk_facts(spec: Any, *, provider_egress_active: bool) -> dict[str, Any]:
    """Describe the opinionated prompt-injection blast-radius baseline.

    These are effective facts, not more policy knobs. The runtime always
    enforces them: variable file-reading shell arguments need a sandbox,
    undeclared HTTP hosts need a run-scoped operator decision, and external
    arguments pass through the egress screen.
    """
    from urllib.parse import urlsplit

    dynamic_shell_commands: list[str] = []
    http_hosts: set[str] = set()
    network_tools: list[str] = []
    for ref in spec.tools:
        if getattr(ref, "builtin", None) == "shell":
            for rule in ref.params().get("commands", []):
                if not isinstance(rule, dict) or not rule.get("allow_extra_args"):
                    continue
                argv = rule.get("argv") or []
                if argv and argv[0] not in PORTABLE_VARIABLE_ARGV:
                    dynamic_shell_commands.append(str(argv[0]))
        if getattr(ref, "builtin", None) == "http_get":
            network_tools.append("http_get")
            http_hosts.update(str(host).casefold() for host in ref.params().get("hosts", []))

    mcp_destinations: list[str] = []
    for server in spec.mcp_servers:
        network_tools.append(f"mcp:{server.name}")
        if getattr(server, "transport", None) == "http":
            hostname = urlsplit(server.url).hostname
            if hostname:
                mcp_destinations.append(hostname.casefold())
        else:
            mcp_destinations.append(f"stdio:{server.name}")

    sandboxed = resolve_backend(spec.confinement) != NONE
    return {
        "safe_for_untrusted_input": provider_egress_active,
        "shell_exposed": any(getattr(ref, "builtin", None) == "shell" for ref in spec.tools),
        "shell_dynamic_readers": sorted(set(dynamic_shell_commands)),
        "shell_dynamic_readers_available": sandboxed or not dynamic_shell_commands,
        "shell_dynamic_readers_blocked_without_sandbox": bool(
            dynamic_shell_commands and not sandboxed
        ),
        "provider_egress_active": provider_egress_active,
        "network_tools": sorted(set(network_tools)),
        "http_preapproved_hosts": sorted(http_hosts),
        "http_undeclared_hosts_require_approval": "http_get" in network_tools,
        "mcp_destinations": sorted(set(mcp_destinations)),
    }


def _backend_hint() -> str:
    return {
        "Linux": "install bubblewrap (e.g. `apt install bubblewrap`)",
        "Darwin": "sandbox-exec is missing or refused a trivial profile",
    }.get(platform.system(), f"no process sandbox is available on {platform.system()}")


def unavailable_reason(config: ConfinementConfig) -> str | None:
    """Why this policy cannot be honored here, or None when it can.

    Only ``require`` is an unmet policy when no backend exists. ``auto`` is
    deliberately opportunistic: prompt-safety controls such as tool
    allowlisting, private-path argument refusal, environment scrubbing and the
    provider egress screen still apply, but they are not represented as
    filesystem isolation.
    """
    if config.mode == "off" or resolve_backend(config) != NONE:
        return None
    if config.mode == "require":
        return (
            "confinement.mode is 'require' but no OS sandbox is available: "
            f"{_backend_hint()}. Set confinement.mode to 'off' to run with "
            "resource limits and a scrubbed environment alone."
        )
    return None


# --------------------------------------------------------------------------- #
# Spawning
# --------------------------------------------------------------------------- #
def shell_argv(command: str) -> list[str]:
    """The argv that runs ``command`` through this platform's shell.

    ``command_succeeds`` takes a shell string on purpose (a spec author writes
    ``pytest -q && ruff check``), so it needs a shell — but naming ``/bin/sh``
    outright made an OS-independent package Unix-only.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
    return ["/bin/sh", "-c", command]


def _real_home() -> Path | None:
    """The operator's home directory, when it is a real one worth hiding."""
    home = os.environ.get("HOME") or ""
    if not home:
        return None
    path = Path(home)
    return path if path.is_absolute() and path != Path("/") and path.is_dir() else None


def _child_environment(config: ConfinementConfig, tmpdir: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    env.setdefault("PATH", _FALLBACK_PATH)
    # HOME is a scratch directory, never the operator's: `~/.aws/credentials`
    # should not even be a path a spawned command can name, let alone read.
    # Where a backend exists the directory is masked outright; this is the part
    # that holds on every platform.
    env["HOME"] = os.environ.get("HOME", tmpdir) if not config.hide_home else tmpdir
    for name in config.env_passthrough:
        if name in os.environ:
            env[name] = os.environ[name]
    # A terminal-shaped TERM invites curses output nobody will read.
    env["TERM"] = "dumb"
    env["TMPDIR"] = tmpdir
    return env


def _limits(config: ConfinementConfig) -> list[tuple[int, tuple[int, int]]]:
    """Resource limits as (constant, (soft, hard)) pairs, resolved before fork."""
    if resource is None:  # pragma: no cover - Windows
        return []
    limits: list[tuple[int, tuple[int, int]]] = [(resource.RLIMIT_CORE, (0, 0))]
    if config.max_memory_mb:
        size = config.max_memory_mb * 1024 * 1024
        # RLIMIT_AS is address space, which some allocators reserve far beyond
        # what they touch; RLIMIT_DATA is the closer analogue of "memory used"
        # where the platform has it. Setting both is the portable compromise.
        limits.append((resource.RLIMIT_AS, (size, size)))
        if hasattr(resource, "RLIMIT_DATA"):
            limits.append((resource.RLIMIT_DATA, (size, size)))
    if config.max_processes and hasattr(resource, "RLIMIT_NPROC"):
        limits.append((resource.RLIMIT_NPROC, (config.max_processes, config.max_processes)))
    return limits


def _preexec(limits: list[tuple[int, tuple[int, int]]]):
    """Build the post-fork hook, or None when there is nothing to apply.

    The values are resolved *before* the fork so the child only calls
    ``setrlimit`` — the less that runs between fork and exec in a process with
    live threads (parallel tool execution has them), the better.
    """
    if not limits or resource is None:  # pragma: no cover - Windows
        return None

    def apply() -> None:  # pragma: no cover - runs in the forked child
        for constant, value in limits:
            try:
                resource.setrlimit(constant, value)
            except (ValueError, OSError):
                # A limit the kernel refuses (a lower hard limit already in
                # force) must not stop the command from running at all.
                pass

    return apply


def _bwrap_argv(
    argv: list[str],
    *,
    cwd: Path,
    config: ConfinementConfig,
    scratch: Path,
    mask: list[Path],
) -> list[str]:
    """Wrap ``argv`` for bubblewrap.

    The whole filesystem is bound read-only rather than a curated allowlist of
    system directories: the command still finds its interpreter, its libraries
    and its data wherever the machine happens to keep them, and the property
    that matters — *nothing outside the harness directory can be written* —
    holds without having to predict the layout of every distribution.
    """
    wrapped = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
    ]
    home = _real_home() if config.hide_home else None
    if home is not None:
        # Read-only is not enough for the one directory that holds SSH keys,
        # cloud credentials, shell history and the Hive. An empty tmpfs over it
        # is; the harness directory is bound back in below if it lives there.
        wrapped += ["--tmpfs", str(home)]
    # The scratch directory serves as HOME and TMPDIR, so it has to survive the
    # tmpfs that just masked /tmp — bound back in, writable, and removed with
    # the rest of the temporary directory when the call returns.
    wrapped += ["--bind", str(scratch), str(scratch)]
    # The harness directory is always bound back in, read-write or read-only:
    # `--tmpfs /tmp` masks anything under /tmp, and a harness that lives there
    # (a scratch checkout, a test fixture) would otherwise vanish from inside
    # its own sandbox.
    wrapped += ["--bind" if config.writable else "--ro-bind", str(cwd), str(cwd)]
    # Masked *after* the harness bind, so a path inside the harness directory
    # is hidden even though the directory around it is available. This is what
    # keeps `.hiveloom` — the journal, and the spilled tool results beside it —
    # out of reach of a command that is allowed to read the harness.
    for path in mask:
        if path.is_dir():
            wrapped += ["--tmpfs", str(path)]
        else:
            # A file cannot carry a tmpfs. /dev/null reads as empty and is
            # read-only, so the path still exists (a command that stats it does
            # not behave differently) while its contents are gone.
            wrapped += ["--ro-bind", os.devnull, str(path)]
    wrapped += [
        "--chdir", str(cwd),
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--die-with-parent",
        "--new-session",
    ]
    if not config.network:
        wrapped.append("--unshare-net")
    return [*wrapped, "--", *argv]


def _sb_literal(value: str) -> str:
    """Quote a path for a Seatbelt profile.

    SBPL string literals are C-like: a path containing a quote or a backslash
    would otherwise end the literal early and the rest of it would be read as
    policy. A profile is generated from filesystem paths the operator chose,
    so this is escaping, not sanitising — but a profile that silently parses
    into something other than what was meant is the worst failure mode here.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sb_path(path: Path) -> str:
    """A subpath rule for ``path``, or a literal rule when it is a file.

    ``subpath`` on a regular file matches nothing, so a file named that way
    would be silently unprotected.
    """
    rule = "subpath" if path.is_dir() else "literal"
    return f"({rule} {_sb_literal(str(path))})"


def _sandbox_profile(
    cwd: Path, config: ConfinementConfig, scratch: Path, mask: list[Path]
) -> str:
    """A seatbelt profile: readable everywhere, writable almost nowhere.

    Written as allow-by-default minus writes and network rather than
    deny-by-default, because a deny-default profile has to re-permit the whole
    dynamic loader and every system service a process touches before it can
    start — and a profile that is wrong in that direction fails as a confusing
    crash rather than as a refusal.
    """
    # The scratch directory is named explicitly: macOS puts temporary files
    # under /var/folders/…, which no fixed /tmp rule would cover.
    scratch_rule = f"(subpath {_sb_literal(str(scratch.resolve()))})"
    cwd_rule = f"(subpath {_sb_literal(str(cwd))})"
    writable = [cwd_rule, scratch_rule, '(subpath "/private/tmp")',
                '(subpath "/private/var/tmp")']
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
    ]
    home = _real_home() if config.hide_home else None
    if home is not None and cwd != home:
        # Seatbelt has no tmpfs, so the equivalent is a read denial with the
        # harness directory carved back out — ordered last so it wins.
        lines.append(f"(deny file-read* (subpath {_sb_literal(str(home.resolve()))}))")
        carved = [
            rule
            for rule, path in ((cwd_rule, cwd), (scratch_rule, scratch.resolve()))
            if str(path).startswith(f"{home.resolve()}/")
        ]
        if carved:
            lines.append(f"(allow file-read* {' '.join(carved)})")
    if config.writable:
        lines.append(f"(allow file-write* {' '.join(writable)})")
    else:
        lines.append(f"(allow file-write* {scratch_rule} (subpath \"/private/tmp\"))")
    lines.append('(allow file-write-data (literal "/dev/null") (literal "/dev/stdout")'
                 ' (literal "/dev/stderr"))')
    if not config.network:
        lines.append("(deny network*)")
    # Last, so it wins over the read allowance the harness directory carries.
    for path in mask:
        lines.append(f"(deny file-read* file-write* {_sb_path(path)})")
    return "".join(lines)


def _sandbox_exec_argv(
    argv: list[str],
    *,
    cwd: Path,
    config: ConfinementConfig,
    scratch: Path,
    mask: list[Path],
) -> list[str]:
    return ["sandbox-exec", "-p", _sandbox_profile(cwd, config, scratch, mask), *argv]


def run_confined(
    argv: list[str],
    *,
    cwd: str | Path,
    config: ConfinementConfig,
    timeout: float | None = None,
    mask: Sequence[str | Path] = (),
) -> ConfinedResult:
    """Run ``argv`` under the strongest confinement this machine allows.

    ``mask`` names paths the command must not reach even though it may read the
    directory containing them — the harness's own ``.hiveloom`` and trace
    directory. Masking is a *kernel* control, so it holds only where a backend
    exists; :class:`hiveloom.tools.builtin.ShellTool` also refuses arguments
    naming those paths, which is the portable half of the same rule.

    Raises :class:`ConfinementUnavailable` when ``mode: require`` cannot be
    met — the point of ``require`` is that an unconfined spawn never silently
    happens instead.
    """
    reason = unavailable_reason(config)
    if reason is not None:
        raise ConfinementUnavailable(reason)

    # Absolute, and with symlinks followed: a backend resolves the paths in
    # its own arguments against *its* working directory, not the caller's, and
    # a macOS profile written against /tmp would not match the /private/tmp the
    # kernel actually sees.
    directory = Path(cwd).resolve()
    backend = resolve_backend(config)
    limit = config.timeout_seconds if timeout is None else timeout
    # Only paths that exist (bwrap fails the whole spawn on a mount target it
    # cannot find, and a trace directory is created lazily), and never the
    # working directory itself or a directory containing it: a harness
    # configured to keep its traces at `.` would otherwise mask the very
    # directory the command has to run in.
    hidden = [
        path
        for path in (Path(m).resolve() for m in mask)
        if path.exists() and path != directory and path not in directory.parents
    ]

    with tempfile.TemporaryDirectory(prefix="hiveloom-run-") as tmpdir:
        # The scratch directory the command gets as HOME/TMPDIR is a subfolder,
        # so nothing the command can reach is used by the runtime itself.
        scratch = Path(tmpdir) / "home"
        scratch.mkdir()
        env = _child_environment(config, str(scratch))
        if backend == BWRAP:
            command = _bwrap_argv(
                argv, cwd=directory, config=config, scratch=scratch, mask=hidden
            )
        elif backend == SANDBOX_EXEC:
            command = _sandbox_exec_argv(
                argv, cwd=directory, config=config, scratch=scratch, mask=hidden
            )
        else:
            command = list(argv)

        # Streamed through pipes into bounded collectors rather than captured
        # to files: a command that writes without bound then costs neither
        # memory nor disk — the excess is read and dropped, and only the count
        # of dropped bytes survives.
        process = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
            command,
            cwd=str(directory),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
            preexec_fn=_preexec(_limits(config)),  # noqa: PLW1509
        )
        out = _BoundedCapture(config.max_output_bytes)
        err = _BoundedCapture(config.max_output_bytes)
        readers = [
            threading.Thread(target=_drain, args=(stream, sink), daemon=True)
            for stream, sink in ((process.stdout, out), (process.stderr, err))
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        deadline = time.monotonic() + limit
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
            returncode = _reap(process)
        # Descendants can keep the inherited pipes open after the direct child
        # exits. Pipe draining is part of the same deadline, not an extra
        # unbounded tail on the configured timeout.
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            timed_out = True
            _kill_tree(process)
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except (AttributeError, OSError):
                    pass
            for reader in readers:
                reader.join(timeout=0.1)

    return ConfinedResult(
        returncode=returncode,
        stdout=out.text(),
        stderr=err.text(),
        backend=backend,
        timed_out=timed_out,
        truncated=out.discarded > 0 or err.discarded > 0,
        discarded_bytes=out.discarded + err.discarded,
    )


class _BoundedCapture:
    """Keeps a bounded head and tail of a stream, counting what it drops.

    The tail matters as much as the head: a build that fails after ten minutes
    of output says why on its last line. A quarter of the budget is reserved
    for it, and the middle is counted rather than kept.
    """

    def __init__(self, cap: int) -> None:
        self._tail_cap = max(1, cap // 4)
        self._head_cap = max(1, cap - self._tail_cap)
        self._head = bytearray()
        self._tail = deque(maxlen=self._tail_cap)
        self.discarded = 0

    def feed(self, chunk: bytes) -> None:
        room = self._head_cap - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        # Anything past the head is a tail candidate; whatever the tail pushes
        # out is gone, and only its size is remembered.
        overflow = max(0, len(self._tail) + len(chunk) - self._tail_cap)
        self.discarded += overflow
        self._tail.extend(chunk)

    def text(self) -> str:
        head = _decode(bytes(self._head))
        if not self._tail:
            return head
        tail = _decode(bytes(self._tail))
        if not self.discarded:
            return head + tail
        return f"{head}\n[... {self.discarded} bytes of output dropped ...]\n{tail}"


def _drain(stream: Any, sink: _BoundedCapture) -> None:
    """Read a pipe to EOF, keeping what fits and discarding the rest.

    Draining is not optional: a pipe left unread fills its buffer and blocks
    the command forever, which would turn an output cap into a hang.
    """
    try:
        with stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                sink.feed(chunk)
    except (OSError, ValueError):  # pragma: no cover - closed under a kill
        return


def _reap(process: subprocess.Popen[Any]) -> int:
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL was ignored
        return -9


def _kill_tree(process: subprocess.Popen[Any]) -> None:
    """Kill the timed-out command *and* anything it started.

    ``start_new_session`` put the child in its own process group precisely so
    this can be group-wide: killing only the direct child leaves a background
    grandchild holding the machine's resources with nothing left to reap it.

    POSIX only. Windows has no process groups to signal here, so the fallback
    kills the direct child and a detached grandchild can outlive it — one of
    the reasons the sandbox backends are the platforms that have one.
    """
    try:
        # ``start_new_session`` makes the leader pid the process-group id. Use
        # it directly: the leader may already have exited while a descendant
        # still owns its stdout/stderr pipes, in which case getpgid(leader)
        # fails even though the group still exists.
        os.killpg(process.pid, 9)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        process.kill()
