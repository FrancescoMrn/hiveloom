"""Tests for the local harness registry (`hiveloom registry ...`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import registry
from hiveloom.cli import app
from hiveloom.errors import SpecError

runner = CliRunner()


def test_register_list_unregister_roundtrip(harness_dir: Path):
    item = registry.register(harness_dir)
    assert item.name == "test-harness"
    assert item.path == str(harness_dir.resolve())

    listed = registry.registered()
    assert [i.name for i in listed] == ["test-harness"]
    assert listed[0].ok

    registry.unregister(harness_dir)
    assert registry.registered() == []


def test_register_is_idempotent(harness_dir: Path):
    registry.register(harness_dir)
    registry.register(harness_dir)
    assert len(registry.registered()) == 1


def test_unregister_by_name(harness_dir: Path):
    registry.register(harness_dir)
    removed = registry.unregister("test-harness")
    assert removed.path == str(harness_dir.resolve())
    assert registry.registered() == []


def test_unregister_unknown_target_raises(harness_dir: Path):
    with pytest.raises(SpecError, match="not a registered"):
        registry.unregister("nope")


def test_register_invalid_directory_raises(tmp_path: Path):
    with pytest.raises(SpecError):
        registry.register(tmp_path / "missing")


def test_broken_entry_is_reported_and_skipped(harness_dir: Path):
    registry.register(harness_dir)
    (harness_dir / "harness.yaml").unlink()

    listed = registry.registered()
    assert listed[0].ok is False
    assert listed[0].error

    good, skipped = registry.serveable()
    assert good == []
    assert skipped[0]["path"] == str(harness_dir.resolve())


def test_registry_cli_add_and_list_json(harness_dir: Path):
    result = runner.invoke(app, ["registry", "add", str(harness_dir), "--json"])
    assert result.exit_code == 0

    result = runner.invoke(app, ["registry", "list", "--json"])
    assert result.exit_code == 0
    assert "test-harness" in result.output

    result = runner.invoke(app, ["registry", "remove", "test-harness", "--json"])
    assert result.exit_code == 0
