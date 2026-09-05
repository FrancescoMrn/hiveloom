"""The one definition of *runtime-private state*, and who may see it.

A run leaves state on disk that belongs to the runtime rather than to the task:
the journal (which records every tool result in full), the spill store beside
it, the Hive, the trust and authorized-key stores, and the credentials the
harness runs with. None of it is task input, and all of it is exactly what an
injected instruction would ask a tool to fetch and hand back to a remote model.

Before this module the answer lived in three places — ``file_read``'s path
check, the packager's exclusion list, and the shell tool's own idea of what to
refuse — and three definitions of one boundary drift. :func:`runtime_private_paths`
is now the single resolver: absolute paths, for the callers that mask or refuse
them, derived from the same facts the packager uses.

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

from pathlib import Path
from typing import TYPE_CHECKING

from hiveloom.package import resolve_trace_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.spec.schema import HarnessSpec

#: Credential files inside a harness. ``.env.example`` and friends are checked
#: in on purpose and carry no secrets — the packager ships them too.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


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
    from hiveloom.logging.hive import default_db_path
    from hiveloom.paths import hiveloom_home
    from hiveloom.trust import trust_store_path

    root = Path(base).resolve()
    paths: list[Path] = [root / ".hiveloom"]

    if spec is not None:
        traces = resolve_trace_dir(root, spec.logging.trace_dir)
        paths += [traces, traces / "spill"]

    # The user-level directory holds the trust store, the model declarations
    # and — by default — the Hive. Named explicitly rather than left to
    # `hide_home`, because $HIVELOOM_HOME may point anywhere.
    paths.append(hiveloom_home())
    paths.append(trust_store_path())

    database = Path(hive_path) if hive_path is not None else default_db_path()
    paths.append(database)
    # SQLite's write-ahead log and shared-memory files hold committed rows that
    # are not yet in the main database file. Masking only the database would
    # leave the most recent runs readable beside it.
    paths += [database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")]

    paths += _env_files(root)

    resolved: list[Path] = []
    for path in paths:
        # Resolved, so a symlink into private state is masked and refused by
        # its target rather than by the name that points at it.
        candidate = path if path.is_absolute() else (root / path)
        candidate = candidate.resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


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
