"""Tests for the CLI surface: exit codes and --json output shapes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError

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


def test_set_frozen_root_still_works_locally(tmp_path: Path):
    """Fix-round-4: the HTTP control plane refuses ALWAYS_FROZEN roots
    (guardrails/model/logging.redact/extensions/hooks/evolution.auto_propose)
    for a remote `mutate`-scoped caller — that check lives entirely in
    serve/app.py. The local CLI, backed directly by construct.set_value,
    must keep working exactly as before for every field, frozen or not:
    construct IS the sanctioned way to edit a spec locally.
    """
    directory = str(tmp_path / "h")
    runner.invoke(app, ["init", directory, "--name", "h", "--task", "T"])
    r = runner.invoke(app, ["set", "model.id", "claude-sonnet-5", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["ok"] is True


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


def test_json_output_is_plain_under_forced_colour(tmp_path: Path):
    """`--json` is a machine contract: no ANSI, even when the shell forces colour.

    CI runners and agent shells export FORCE_COLOR/CLICOLOR_FORCE; routing the
    payload through rich would then inject escape codes and break json.loads
    for every caller of the CLI. CliRunner cannot see this — rich decides on the
    real stdout — so this one goes through a subprocess.
    """
    import os
    import subprocess
    import sys

    env = {**os.environ, "FORCE_COLOR": "1", "CLICOLOR_FORCE": "1"}
    env.pop("NO_COLOR", None)
    def run(args: list[str]):
        return subprocess.run(
            [sys.executable, "-c", "from hiveloom.cli import app; app()", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )

    ok = run(["catalog", "tools", "--json"])
    assert ok.returncode == ExitCode.OK
    assert "\x1b" not in ok.stdout
    assert json.loads(ok.stdout)["ok"] is True

    err = run(["validate", str(tmp_path / "nope"), "--json"])
    assert err.returncode == ExitCode.SPEC_ERROR
    assert "\x1b" not in err.stdout
    assert json.loads(err.stdout)["ok"] is False


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


# --------------------------------------------------------------------------- #
# Packaged agent guidance
# --------------------------------------------------------------------------- #
def test_guide_lists_every_topic():
    r = runner.invoke(app, ["guide", "--list", "--json"])
    assert r.exit_code == ExitCode.OK
    names = [t["name"] for t in _json(r)["topics"]]
    assert names[:2] == ["agents", "all"]
    # One topic per lifecycle skill, named without the hiveloom- prefix.
    assert {"build", "run", "evolve", "extend", "ship"} <= set(names)
    assert all(t["description"] for t in _json(r)["topics"])


def test_guide_prints_raw_markdown():
    r = runner.invoke(app, ["guide"])
    assert r.exit_code == ExitCode.OK
    assert r.stdout.startswith("# hiveloom for agents")

    skill = runner.invoke(app, ["guide", "build"])
    assert skill.exit_code == ExitCode.OK
    assert "name: hiveloom-build" in skill.stdout


def test_guide_unknown_topic_is_a_spec_error():
    r = runner.invoke(app, ["guide", "nope", "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert "unknown guide topic" in _json(r)["error"]


def test_guide_reads_the_packaged_copy_when_present(tmp_path: Path, monkeypatch):
    """In a wheel there is no repo root — the docs come from hiveloom/agent_docs.

    The build force-includes them there; this pins the resolution order so a
    packaging change that drops them fails here rather than in a user's install.
    """
    from hiveloom import guide

    packaged = Path(guide.__file__).resolve().parent / "agent_docs"
    monkeypatch.setattr(guide, "__file__", str(tmp_path / "hiveloom" / "guide.py"))
    assert not (tmp_path / "hiveloom" / "agent_docs").exists()

    # No packaged copy and no repo root above the fake location: a clean error,
    # never a traceback.
    with pytest.raises(SpecError, match="agent guidance is not available"):
        guide.agent_docs_dir()

    (tmp_path / "hiveloom" / "agent_docs").mkdir(parents=True)
    (tmp_path / "hiveloom" / "agent_docs" / "AGENTS.md").write_text("# packaged\n")
    assert guide.agent_docs_dir() == tmp_path / "hiveloom" / "agent_docs"
    assert packaged.name == "agent_docs"  # the real constant is unchanged
