"""Tests for the CLI surface: exit codes and --json output shapes."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.errors import ExitCode

runner = CliRunner()


def _json(result) -> dict:
    return json.loads(result.stdout)


def test_init_and_validate_flow(tmp_path: Path):
    directory = str(tmp_path / "h")
    r = runner.invoke(app, ["init", directory, "--name", "cli-h", "--task", "T", "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["ok"] is True

    r = runner.invoke(app, ["validate", directory, "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["name"] == "cli-h"


def test_set_invalid_returns_spec_error_code(tmp_path: Path):
    directory = str(tmp_path / "h")
    runner.invoke(app, ["init", directory, "--name", "h", "--task", "T"])
    r = runner.invoke(app, ["set", "loop.max_turns", "0", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert _json(r)["ok"] is False


def test_catalog_json(tmp_path: Path):
    r = runner.invoke(app, ["catalog", "tools", "--json"])
    assert r.exit_code == ExitCode.OK
    payload = _json(r)
    names = [e["name"] for e in payload["entries"]]
    assert "file_read" in names


def test_catalog_unknown_kind(tmp_path: Path):
    r = runner.invoke(app, ["catalog", "widgets", "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR


def test_explain_json():
    r = runner.invoke(app, ["explain", "context.compaction", "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["path"] == "context.compaction"


def test_catalog_policies_lists_sequential_steps():
    r = runner.invoke(app, ["catalog", "policies", "--json"])
    assert r.exit_code == ExitCode.OK
    names = [e["name"] for e in _json(r)["entries"]]
    assert "sequential_steps" in names


def test_explain_loop_steps():
    r = runner.invoke(app, ["explain", "loop.steps", "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["path"] == "loop.steps"


def test_add_and_remove_tool(tmp_path: Path):
    directory = str(tmp_path / "h")
    runner.invoke(app, ["init", directory, "--name", "h", "--task", "T"])
    r = runner.invoke(app, ["add", "tool", "--builtin", "file_read", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.OK
    r = runner.invoke(app, ["remove", "file_read", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["removed"] == "file_read"


def test_add_guardrail_reports_replacement(tmp_path: Path):
    directory = str(tmp_path / "h")
    runner.invoke(app, ["init", directory, "--name", "h", "--task", "T"])

    # init's spec carries the default max_cost_usd of 1.00; adding one replaces it.
    r = runner.invoke(
        app, ["add", "guardrail", "--builtin", "max_cost_usd", "--value", "0.25",
              "--dir", directory, "--json"]
    )
    assert r.exit_code == ExitCode.OK
    payload = _json(r)
    assert payload["replaced"] == "guardrail"
    assert payload["before"] == [{"builtin": "max_cost_usd", "value": 1.00}]
    assert payload["after"] == {"builtin": "max_cost_usd", "value": 0.25}


def test_add_new_guardrail_reports_addition(tmp_path: Path):
    directory = str(tmp_path / "h")
    runner.invoke(app, ["init", directory, "--name", "h", "--task", "T"])
    r = runner.invoke(
        app, ["add", "guardrail", "--builtin", "tool_allowlist", "--dir", directory, "--json"]
    )
    assert r.exit_code == ExitCode.OK
    assert _json(r)["added"] == "guardrail"


def test_validate_missing_harness(tmp_path: Path):
    r = runner.invoke(app, ["validate", str(tmp_path / "nope"), "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR


def test_unexpected_error_uses_runtime_exit_and_json(tmp_path: Path, monkeypatch):
    from hiveloom import cli

    monkeypatch.setattr(
        cli.construct,
        "init_harness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = runner.invoke(app, ["init", str(tmp_path / "h"), "--name", "h", "--task", "T", "--json"])

    assert r.exit_code == ExitCode.RUNTIME_ERROR
    assert _json(r) == {"ok": False, "error": "boom"}


def test_schema_annotated_is_raw_yaml():
    r = runner.invoke(app, ["schema", "--annotated"])
    assert r.exit_code == ExitCode.OK
    assert "system_prompt:" in r.stdout


def test_run_dry_run_needs_no_api_key():
    import shutil
    from pathlib import Path as _P

    example = _P(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = _P(tmp) / "h"
        shutil.copytree(example, target)
        (target / "notes.txt").write_text("some source text to summarize")
        r = runner.invoke(app, ["run", str(target), "--input", "notes.txt", "--dry-run", "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["dry_run"] is True
