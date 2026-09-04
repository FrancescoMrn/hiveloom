"""Atomic harness document migrations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from hiveloom import trust
from hiveloom.errors import HiveloomError
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import (
    atomic_write_text,
    dump_spec,
    harness_path,
    load_raw,
    validate_harness,
)


class HarnessMigrationResult(BaseModel):
    """Machine-readable receipt for one document migration."""

    changed: bool
    path: str
    from_field: str
    to_field: str = "schema_version"
    schema_version: str
    behavior_hash_before: str
    behavior_hash_after: str


def migrate_harness(
    path: str | Path,
    *,
    approve_trust: Callable[[str], bool] | None = None,
) -> HarnessMigrationResult:
    """Rewrite legacy ``version`` to ``schema_version`` transactionally.

    Full hook validation runs before and after the atomic replacement. If the
    post-write validation fails, the original bytes are restored before the
    error reaches the caller.
    """
    yaml_path = harness_path(path)
    base = yaml_path.parent.resolve()
    trust.ensure_trusted(base, approve_trust)
    raw = load_raw(yaml_path)
    original = yaml_path.read_text(encoding="utf-8")
    spec = validate_harness(yaml_path)
    before_hash = spec_version_hash(spec, base)
    from_field = "version" if "version" in raw else "schema_version"

    if "version" not in raw:
        return HarnessMigrationResult(
            changed=False,
            path=str(yaml_path.resolve()),
            from_field=from_field,
            schema_version=spec.schema_version,
            behavior_hash_before=before_hash,
            behavior_hash_after=before_hash,
        )

    try:
        atomic_write_text(yaml_path, dump_spec(spec))
        migrated = validate_harness(yaml_path)
    except Exception as exc:  # noqa: BLE001 - rollback must cover every validator failure
        try:
            atomic_write_text(yaml_path, original)
        except Exception as rollback_exc:  # noqa: BLE001 - report both failures
            raise HiveloomError(
                "harness migration failed and the original spec could not be restored: "
                f"{type(rollback_exc).__name__}: {rollback_exc}"
            ) from exc
        if isinstance(exc, HiveloomError):
            raise
        raise HiveloomError(
            f"harness migration failed: {type(exc).__name__}: {exc}"
        ) from exc

    after_hash = spec_version_hash(migrated, base)
    return HarnessMigrationResult(
        changed=True,
        path=str(yaml_path.resolve()),
        from_field=from_field,
        schema_version=migrated.schema_version,
        behavior_hash_before=before_hash,
        behavior_hash_after=after_hash,
    )
