"""Schema, behavior, and execution identity remain separate contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import construct
from hiveloom import migrate_harness as public_migrate_harness
from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import TraceWriter, spec_version_hash
from hiveloom.spec.loader import dump_spec, load_spec
from hiveloom.spec.migrate import migrate_harness

cli = CliRunner()


def _legacy_harness(tmp_path: Path) -> Path:
    harness = tmp_path / "h"
    construct.init_harness(harness, name="identity-fixture", task="Return an answer.")
    yaml_path = harness / "harness.yaml"
    canonical = yaml_path.read_text(encoding="utf-8")
    assert canonical.startswith("schema_version: 0.2.0\n")
    yaml_path.write_text(
        canonical.replace("schema_version:", "version:", 1),
        encoding="utf-8",
    )
    return harness


def test_legacy_version_loads_but_serializes_canonical_field(tmp_path: Path):
    harness = _legacy_harness(tmp_path)
    spec = load_spec(harness)

    assert spec.schema_version == "0.2.0"
    assert spec.version == "0.2.0"
    dumped = dump_spec(spec)
    assert dumped.startswith("schema_version: 0.2.0\n")
    assert "\nversion:" not in dumped


def test_schema_and_annotated_contract_use_canonical_field():
    from hiveloom.spec import annotate

    contract = annotate.json_schema()
    assert "schema_version" in contract["properties"]
    assert "version" not in contract["properties"]
    assert annotate.annotated_template().startswith(
        "# hiveloom harness spec — annotated template."
    )
    assert "\nschema_version: 0.2.0\n" in annotate.annotated_template()


def test_conflicting_format_fields_are_rejected(tmp_path: Path):
    yaml_path = tmp_path / "harness.yaml"
    yaml_path.write_text(
        "version: 0.1.0\nschema_version: 0.2.0\n"
        "name: h\ndescription: d\nsystem_prompt: s\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecError, match="conflicting version and schema_version"):
        load_spec(yaml_path)


def test_cli_migrate_is_atomic_and_behavior_neutral(tmp_path: Path):
    harness = _legacy_harness(tmp_path)
    before = spec_version_hash(load_spec(harness), harness)

    result = cli.invoke(app, ["migrate", str(harness), "--json"])

    assert result.exit_code == ExitCode.OK, result.stdout
    payload = json.loads(result.stdout)
    assert payload["changed"] is True
    assert payload["from_field"] == "version"
    assert payload["to_field"] == "schema_version"
    assert payload["behavior_hash_before"] == before
    assert payload["behavior_hash_after"] == before
    assert (harness / "harness.yaml").read_text().startswith(
        "schema_version: 0.2.0\n"
    )


def test_migrate_current_harness_is_byte_identical_noop(tmp_path: Path):
    harness = tmp_path / "h"
    construct.init_harness(harness, name="current", task="Return an answer.")
    yaml_path = harness / "harness.yaml"
    before = yaml_path.read_bytes()

    result = public_migrate_harness(harness)

    assert result.changed is False
    assert result.from_field == "schema_version"
    assert yaml_path.read_bytes() == before


def test_migrate_rolls_back_post_write_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    harness = _legacy_harness(tmp_path)
    yaml_path = harness / "harness.yaml"
    original = yaml_path.read_bytes()
    migrate_mod = importlib.import_module("hiveloom.spec.migrate")
    real_validate = migrate_mod.validate_harness
    calls = 0

    def fail_second_validation(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SpecError("synthetic post-write failure")
        return real_validate(path)

    monkeypatch.setattr(migrate_mod, "validate_harness", fail_second_validation)

    with pytest.raises(SpecError, match="synthetic post-write failure"):
        migrate_harness(harness)

    assert yaml_path.read_bytes() == original
    assert load_spec(harness).schema_version == "0.2.0"


def test_migration_keeps_old_hive_version_bucket_queryable(tmp_path: Path):
    harness = _legacy_harness(tmp_path)
    spec = load_spec(harness)
    behavior_hash = spec_version_hash(spec, harness)
    writer = TraceWriter(
        tmp_path / "traces",
        "historical-run",
        spec.name,
        behavior_hash,
        harness_id=spec.identity,
    )
    writer.emit("run_started", input="synthetic")
    writer.emit(
        "run_finished",
        status="success",
        turns=1,
        cost_usd=0.0,
        duration_seconds=0.1,
    )
    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(writer.path)

    migrate_harness(harness)
    migrated_hash = spec_version_hash(load_spec(harness), harness)

    with Hive(tmp_path / "hive.db") as hive:
        buckets = hive.version_stats(spec.identity)
    assert migrated_hash == behavior_hash
    assert buckets == [
        {
            "version": behavior_hash,
            "runs": 1,
            "successes": 1,
            "success_rate": 1.0,
            "avg_cost_usd": 0.0,
            "avg_turns": 1.0,
            "swapped_runs": 0,
        }
    ]


def test_behavior_hash_tracks_referenced_playbook_prompt(tmp_path: Path):
    harness = tmp_path / "h"
    construct.init_harness(harness, name="prompted", task="Return an answer.")
    construct.add_playbook(harness, name="answer", description="Answer the task.")
    spec = load_spec(harness)
    prompt = harness / "playbooks" / "answer.md"
    before = spec_version_hash(spec, harness)

    prompt.write_text("Use a materially different strategy.\n", encoding="utf-8")

    after = spec_version_hash(load_spec(harness), harness)
    assert before != after


def test_behavior_hash_matches_pre_rename_serialization(tmp_path: Path):
    harness = _legacy_harness(tmp_path)
    spec = load_spec(harness)
    legacy_dump = dump_spec(spec).replace("schema_version:", "version:", 1)
    expected = hashlib.sha256(legacy_dump.encode("utf-8")).hexdigest()[:12]

    assert spec_version_hash(spec) == expected
