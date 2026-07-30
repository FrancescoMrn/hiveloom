"""Tests for the open ecosystem (M8): trust, --stream/SDK, blueprints, lock packs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import hiveloom
from hiveloom import construct, ext, runner, trust
from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError
from hiveloom.generate.generator import expand_blueprint, resolve_blueprint
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response

cli_runner = CliRunner()


# --------------------------------------------------------------------------- #
# Trust store
# --------------------------------------------------------------------------- #
def test_init_records_trust(harness_dir: Path):
    assert trust.is_trusted(harness_dir)


def test_record_trust_is_idempotent(harness_dir: Path):
    """Re-trusting an already-trusted dir must not rewrite the store.

    `run --approve` records trust on every invocation; the read-modify-write is
    unlocked, so repeated writes race under concurrent runs (e.g. an eval sweep
    firing many `hiveloom run` subprocesses at the same harness).
    """
    store = trust.trust_store_path()
    before = store.read_bytes()

    trust.record_trust(harness_dir)

    assert store.read_bytes() == before
    assert trust.is_trusted(harness_dir)


def test_untrusted_foreign_harness_is_rejected(harness_dir: Path, monkeypatch):
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)
    trust.revoke_trust(harness_dir)  # simulate a foreign folder
    with pytest.raises(SpecError, match="not trusted"):
        runner.run_harness(harness_dir, "go", provider=FakeModelProvider([]))


def test_trust_env_never_blocks_even_after_prompt(harness_dir: Path, monkeypatch):
    monkeypatch.setenv("HIVELOOM_TRUST", "never")
    trust.revoke_trust(harness_dir)
    with pytest.raises(SpecError, match="HIVELOOM_TRUST=never"):
        runner.dry_run(harness_dir, "go")


def test_approve_callback_records_trust(harness_dir: Path, monkeypatch):
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)
    trust.revoke_trust(harness_dir)
    asked: list[str] = []

    def approve(path: str) -> bool:
        asked.append(path)
        return True

    result = runner.run_harness(
        harness_dir, "go",
        provider=FakeModelProvider([text_response("done")]),
        approve_trust=approve,
    )
    assert result.status in ("success", "verify_failed")
    assert asked and trust.is_trusted(harness_dir)


def test_trust_cli_command(harness_dir: Path, monkeypatch):
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)
    trust.revoke_trust(harness_dir)
    r = cli_runner.invoke(app, ["trust", str(harness_dir), "--json"])
    assert r.exit_code == ExitCode.OK
    assert trust.is_trusted(harness_dir)
    r = cli_runner.invoke(app, ["trust", str(harness_dir), "--revoke", "--json"])
    assert r.exit_code == ExitCode.OK
    assert not trust.is_trusted(harness_dir)


def test_construct_on_untrusted_dir_is_rejected(harness_dir: Path, monkeypatch):
    monkeypatch.delenv("HIVELOOM_TRUST", raising=False)
    trust.revoke_trust(harness_dir)
    with pytest.raises(SpecError, match="not trusted"):
        construct.set_field(harness_dir, "loop.max_turns", "5")


def test_stats_ingestion_requires_trust_before_loading_spec(harness_dir: Path, monkeypatch):
    monkeypatch.setenv("HIVELOOM_TRUST", "never")
    trust.revoke_trust(harness_dir)
    with Hive() as hive, pytest.raises(SpecError, match="not trusted"):
        runner.resolve_and_ingest(harness_dir, hive)


# --------------------------------------------------------------------------- #
# Streaming & SDK
# --------------------------------------------------------------------------- #
def test_on_event_callback_streams_trace_events(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    seen: list[str] = []
    result = runner.run_harness(
        harness_dir, "go",
        provider=FakeModelProvider([text_response("done")]),
        on_event=lambda e: seen.append(e.type),
    )
    assert result.status == "success"
    assert seen[0] == "run_started"
    assert seen[-1] == "run_finished"


def test_broken_on_event_consumer_does_not_kill_run(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")

    def boom(event):
        raise RuntimeError("consumer crashed")

    result = runner.run_harness(
        harness_dir, "go",
        provider=FakeModelProvider([text_response("done")]),
        on_event=boom,
    )
    assert result.status == "success"


def test_sdk_exports_resolve():
    assert callable(hiveloom.run_harness)
    assert callable(hiveloom.generate_harness)
    assert hiveloom.RunResult.__name__ == "RunResult"
    assert hiveloom.Hive is not None
    with pytest.raises(AttributeError):
        hiveloom.not_a_thing  # noqa: B018


# --------------------------------------------------------------------------- #
# Blueprints
# --------------------------------------------------------------------------- #
def test_expand_blueprint_slots():
    text = "Build a $1 harness. Task: $ARGUMENTS. Second word: $2. Missing: $9."
    out = expand_blueprint(text, "scraper daily")
    assert out == "Build a scraper harness. Task: scraper daily. Second word: daily. Missing: ."


def test_blueprint_from_home_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    bp_dir = tmp_path / "blueprints"
    bp_dir.mkdir()
    (bp_dir / "scraper.md").write_text(
        "Always add no_network_write. Task: $ARGUMENTS", encoding="utf-8"
    )
    out = resolve_blueprint("scraper", "get HN stories")
    assert "no_network_write" in out and "get HN stories" in out
    assert "scraper" in ext.blueprint_names()


def test_blueprint_from_extension_registration():
    api = ext.ExtensionAPI(source="test:bp")
    api.register_blueprint("house", "Prefer builtins. $ARGUMENTS")
    assert "Prefer builtins" in resolve_blueprint("house", "t")


def test_unknown_blueprint_is_actionable():
    from hiveloom.generate.generator import PlanError

    with pytest.raises(PlanError, match="unknown blueprint"):
        resolve_blueprint("nope", "t")


def test_generate_with_blueprint_reaches_meta_prompt(tmp_path: Path):
    from hiveloom.generate.llm import FakeStrongModel

    api = ext.ExtensionAPI(source="test:bp")
    api.register_blueprint("strict", "ALWAYS include a regex_match validator.")
    plan = json.dumps({"name": "g", "task": "t", "steps": []})
    model = FakeStrongModel([plan])
    from hiveloom.generate.generator import generate

    generate("do a thing", tmp_path / "g", model, blueprint="strict")
    assert "ALWAYS include a regex_match validator." in model.prompts[0]["system"]


# --------------------------------------------------------------------------- #
# Lock packs & stream CLI
# --------------------------------------------------------------------------- #
def test_lock_records_packs_for_extension_entries(harness_dir: Path):
    api = ext.ExtensionAPI(source="pkg:acme-tools")
    ext._registry.pack_dists["pkg:acme-tools"] = {"name": "acme-tools", "version": "1.2.3"}
    api.register_tool(
        "acme_fetch", lambda p, c: None, description="Fetch from acme."
    )
    construct.add_tool(harness_dir, builtin="acme_fetch")

    from hiveloom.package import package_harness

    result = package_harness(harness_dir)
    assert result["name"] == "test-harness"
    import yaml

    lock = yaml.safe_load((harness_dir / "hiveloom.lock").read_text())
    assert lock["packs"] == [
        {"source": "pkg:acme-tools", "name": "acme-tools", "version": "1.2.3"}
    ]


def test_lock_has_no_packs_key_for_builtin_only(harness_dir: Path):
    from hiveloom.package import package_harness

    package_harness(harness_dir)
    import yaml

    lock = yaml.safe_load((harness_dir / "hiveloom.lock").read_text())
    assert "packs" not in lock


def test_run_stream_emits_jsonl(harness_dir: Path, monkeypatch):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    monkeypatch.setattr(
        "hiveloom.runner._default_provider",
        lambda base, name: FakeModelProvider([text_response("done")]),
    )
    r = cli_runner.invoke(
        app, ["run", str(harness_dir), "--input", "go", "--stream"]
    )
    assert r.exit_code == ExitCode.OK
    lines = [json.loads(line) for line in r.stdout.strip().splitlines()]
    assert lines[0]["type"] == "run_started"
    assert lines[-1]["type"] == "run_result"
    assert lines[-1]["status"] == "success"
