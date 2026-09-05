"""The one definition of *runtime-private state*, and who may see it.

A run leaves state on disk that belongs to the runtime rather than to the task:
the journal (which records every tool result in full), the spill store beside
it, the Hive, the trust and authorized-key stores, and the credentials the
harness runs with. None of it is task input, and all of it is exactly what an
injected instruction would ask a tool to fetch and hand back to a remote model.

Before this module the answer lived in three places — ``file_read``'s path
check, the packager's exclusion list, and the shell tool's own idea of what to
refuse — and three definitions of one boundary drift. :class:`RunBoundary` is
now the single per-run resolver, created after caller overrides are known and
shared by everything that masks, refuses, writes, or reports these paths.

Two consumers, two enforcement mechanisms:

* :mod:`hiveloom.confine` masks these paths inside the OS sandbox, so a spawned
  process cannot reach them at all — including through a symlink, because a
  mask is applied to the resolved target.
* :mod:`hiveloom.tools.builtin` refuses arguments and file-tool paths that
  resolve into them, which is what holds on a machine with no sandbox.

The boundary is deliberately one-way for spill objects:
:mod:`hiveloom.context.spill`'s retrieval tools are the *only* sanctioned route
back into a model request, and they carry a per-run authorization the
filesystem does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hiveloom.package import resolve_trace_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.spec.schema import HarnessSpec

#: Credential files inside a harness. ``.env.example`` and friends are checked
#: in on purpose and carry no secrets — the packager ships them too.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


@dataclass(frozen=True)
class RunBoundary:
    """The effective runtime-private filesystem boundary for one run.

    This object is resolved only after caller overrides have been applied and
    is then passed to every tool, verifier, trace writer and diagnostic.  It
    prevents a runtime ``trace_dir``/``hive_path`` from becoming a second,
    less-protected interpretation of the spec.

    ``private_paths`` is deliberately a method rather than a frozen list:
    credential files are discovered again immediately before each access or
    spawn, so an ``.env`` created during a run is private straight away.
    """

    base: Path
    trace_dir: Path
    spill_dir: Path
    hive_path: Path
    home: Path
    trust_store: Path

    @classmethod
    def resolve(
        cls,
        base: str | Path,
        spec: HarnessSpec | None = None,
        *,
        trace_dir: str | Path | None = None,
        hive_path: str | Path | None = None,
    ) -> RunBoundary:
        """Resolve the actual paths this run will use, including overrides."""
        from hiveloom.logging.hive import default_db_path
        from hiveloom.paths import hiveloom_home
        from hiveloom.trust import trust_store_path

        root = Path(base).expanduser().resolve()
        if trace_dir is not None:
            traces = Path(trace_dir).expanduser()
            # Preserve the SDK/CLI override contract: like Path.resolve(), an
            # explicit relative override is relative to the caller's cwd. The
            # trace_dir inside the spec remains relative to the harness.
            traces = traces if traces.is_absolute() else Path.cwd() / traces
            traces = traces.resolve()
        elif spec is not None:
            traces = resolve_trace_dir(root, spec.logging.trace_dir).resolve()
        else:
            traces = (root / ".hiveloom" / "traces").resolve()

        database = Path(hive_path) if hive_path is not None else default_db_path()
        database = database.expanduser()
        database = database if database.is_absolute() else Path.cwd() / database
        database = database.resolve()
        home = hiveloom_home().expanduser().resolve()
        trust_store = trust_store_path().expanduser().resolve()
        return cls(
            base=root,
            trace_dir=traces,
            spill_dir=traces / "spill",
            hive_path=database,
            home=home,
            trust_store=trust_store,
        )

    @property
    def trace_dir_relative(self) -> Path | None:
        """Trace directory relative to the harness, when it is inside it."""
        try:
            return self.trace_dir.relative_to(self.base)
        except ValueError:
            return None

    def private_paths(self) -> list[Path]:
        """Return the current private set, re-discovering credential files."""
        database = self.hive_path
        candidates = [
            self.base / ".hiveloom",
            self.trace_dir,
            self.spill_dir,
            self.home,
            self.trust_store,
            database,
            *(
                database.with_name(database.name + suffix)
                for suffix in ("-wal", "-shm", "-journal")
            ),
            *env_files(self.base),
        ]
        resolved: list[Path] = []
        for path in candidates:
            candidate = path.resolve()
            if candidate not in resolved:
                resolved.append(candidate)
        return resolved


def env_files(base: str | Path) -> list[Path]:
    """Credential files inside a harness, as they exist right now.

    Re-read at spawn time rather than resolved once: a harness that writes an
    ``.env`` mid-run must not thereby have it readable for the rest of it.
    """
    return _env_files(Path(base))


def _env_files(base: Path) -> list[Path]:
    return [
        path
        for path in base.glob(".env*")
        if path.name.casefold() not in ENV_TEMPLATES
    ]


def runtime_private_paths(
    base: str | Path,
    spec: HarnessSpec | None = None,
    *,
    trace_dir: str | Path | None = None,
    hive_path: str | Path | None = None,
) -> list[Path]:
    """Every absolute path holding this run's private state, existing or not.

    Callers filter for existence themselves — a mask needs a real target, a
    refusal does not, and a path that does not exist yet (a trace directory
    before the first write) must still be refused.

    ``spec`` supplies the configured trace directory; without one the default
    location under ``.hiveloom`` is already covered. ``hive_path`` names a
    Hive outside the user directory, as an embedding caller may pass.
    """
    return RunBoundary.resolve(
        base, spec, trace_dir=trace_dir, hive_path=hive_path
    ).private_paths()


def is_private(
    candidate: str | Path, private_paths: list[Path], *, base: str | Path | None = None
) -> bool:
    """Whether ``candidate`` is, or is inside, runtime-private state.

    ``base`` adds the name-based half of the rule for paths under a harness:
    a ``.env`` that does not exist yet is still private, and answering "no"
    for it would make the check depend on write order.
    """
    target = Path(candidate).resolve()
    if any(target == path or path in target.parents for path in private_paths):
        return True
    if base is None:
        return False
    from hiveloom.package import is_sensitive_path

    try:
        relative = target.relative_to(Path(base).resolve())
    except ValueError:
        return False
    return bool(relative.parts) and is_sensitive_path(relative)
