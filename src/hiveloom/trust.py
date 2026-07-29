"""The harness trust store: protect a machine from foreign harness folders.

A harness folder carries executable code (tool/validator/guardrail/event
hooks, extensions) that runs with your permissions the moment the harness is
validated or run. Frozen paths protect a harness from its *evolution*; trust
protects a machine from a *foreign harness* — the same threat model pi's
project trust addresses.

Trust is per resolved directory path, remembered in
``~/.hiveloom/trust.json``:

* harnesses **constructed on this machine** (``init`` and every construct
  mutation) are trusted automatically;
* a foreign folder (unzipped artifact, git clone) prompts once interactively,
  or obeys ``HIVELOOM_TRUST=always|never`` in non-interactive runs (CI);
* ``hiveloom trust <dir>`` records a decision explicitly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from hiveloom.errors import SpecError
from hiveloom.paths import hiveloom_home
from hiveloom.spec.loader import atomic_write_text


def trust_store_path() -> Path:
    return hiveloom_home() / "trust.json"


def _key(base: str | Path) -> str:
    return str(Path(base).resolve())


def _load() -> dict:
    path = trust_store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    path = trust_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def is_trusted(base: str | Path) -> bool:
    return _key(base) in _load()


def record_trust(base: str | Path) -> None:
    data = _load()
    if _key(base) in data:
        # Idempotent: re-trusting must not rewrite the store. Callers invoke
        # this per run (e.g. `run --approve` in eval sweeps), and the unlocked
        # read-modify-write would race under concurrent runs.
        return
    data[_key(base)] = {"trusted_at": datetime.now(UTC).isoformat()}
    _save(data)


def revoke_trust(base: str | Path) -> bool:
    data = _load()
    removed = data.pop(_key(base), None) is not None
    if removed:
        _save(data)
    return removed


def ensure_trusted(base: str | Path, approve: Callable[[str], bool] | None = None) -> None:
    """Gate code-executing operations on a harness folder.

    Resolution order: already trusted → ``HIVELOOM_TRUST`` env policy
    (``always``/``never``) → interactive ``approve(path)`` callback →
    :class:`SpecError` with the ways to proceed.
    """
    if is_trusted(base):
        return
    key = _key(base)
    policy = os.environ.get("HIVELOOM_TRUST", "").strip().lower()
    if policy == "always":
        record_trust(base)
        return
    if policy == "never":
        raise SpecError(f"harness at {key} is not trusted (HIVELOOM_TRUST=never)")
    if approve is not None and approve(key):
        record_trust(base)
        return
    raise SpecError(
        f"harness at {key} is not trusted. Its code hooks would run with your "
        "permissions. Trust it with `hiveloom trust <dir>` (or --approve, or "
        "HIVELOOM_TRUST=always for CI)."
    )
