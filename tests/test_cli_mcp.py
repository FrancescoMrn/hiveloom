"""Tests for the MCP CLI surface: `add mcp-server` and `mcp list-tools`."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hiveloom import trust
from hiveloom.cli import app
from hiveloom.errors import ExitCode
from hiveloom.tools.mcp import McpBridge

runner = CliRunner()
FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")


def _json(result) -> dict:
    return json.loads(result.stdout)


def _init(tmp_path: Path) -> str:
    directory = str(tmp_path / "h")
    runner.invoke(app, ["init", directory, "--name", "h", "--task", "T"])
    return directory


def test_add_mcp_server_stdio_cli_writes_expected_yaml(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "echo",
            "--stdio-command", sys.executable,
            "--stdio-arg", FIXTURE,
            "--env", "FOO=bar",
            "--env-from-host", "TOKEN=HOST_TOKEN",
            "--cwd", "sub",
            "--dir", directory, "--json",
        ],
    )
    assert r.exit_code == ExitCode.OK
    assert _json(r) == {"ok": True, "added": "mcp-server", "ref": "echo"}

    raw = yaml.safe_load((Path(directory) / "harness.yaml").read_text())
    entry = raw["mcp_servers"][0]
    assert entry["name"] == "echo"
    assert entry["transport"] == "stdio"
    assert entry["command"] == sys.executable
    assert entry["args"] == [FIXTURE]
    assert entry["env"] == {"FOO": "bar"}
    assert entry["env_from_host_env"] == {"TOKEN": "HOST_TOKEN"}
    assert entry["cwd"] == "sub"


def test_add_mcp_server_http_cli_writes_expected_yaml(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "jira",
            "--url", "https://mcp.acme.invalid/mcp",
            "--header", "X-Foo: bar",
            "--header-env", "Authorization=ACME_MCP_TOKEN",
            "--tool", "search_issues",
            "--deferred",
            "--dir", directory, "--json",
        ],
    )
    assert r.exit_code == ExitCode.OK
    raw = yaml.safe_load((Path(directory) / "harness.yaml").read_text())
    entry = raw["mcp_servers"][0]
    assert entry["transport"] == "http"
    assert entry["url"] == "https://mcp.acme.invalid/mcp"
    assert entry["headers"] == {"X-Foo": "bar"}
    assert entry["header_env"] == {"Authorization": "ACME_MCP_TOKEN"}
    assert entry["tools"] == ["search_issues"]
    assert entry["deferred"] is True


def test_add_mcp_server_malformed_env_pair(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "echo", "--stdio-command", "npx",
            "--env", "not-a-kv-pair", "--dir", directory, "--json",
        ],
    )
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert "--env" in _json(r)["error"]


def test_add_mcp_server_malformed_header(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "jira", "--url", "https://x.invalid",
            "--header", "no-colon-here", "--dir", directory, "--json",
        ],
    )
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert "--header" in _json(r)["error"]


def test_add_mcp_server_neither_command_nor_url(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(app, ["add", "mcp-server", "--name", "x", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR


def test_add_mcp_server_both_command_and_url(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "x", "--stdio-command", "npx",
            "--url", "https://x.invalid", "--dir", directory, "--json",
        ],
    )
    assert r.exit_code == ExitCode.SPEC_ERROR


def test_mcp_list_tools_json_discovers_real_tools(tmp_path: Path):
    directory = _init(tmp_path)
    runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "echo",
            "--stdio-command", sys.executable, "--stdio-arg", FIXTURE,
            "--dir", directory,
        ],
    )
    before = threading.active_count()
    r = runner.invoke(app, ["mcp", "list-tools", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.OK
    names = {t["name"] for t in _json(r)["tools"]}
    assert {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__boom"} <= names
    # Reuses build_registry's bridge/closer -- must tear the subprocess and
    # portal thread down cleanly, not leak them after the command returns.
    assert threading.active_count() == before


def test_mcp_list_tools_empty_when_no_servers(tmp_path: Path):
    directory = _init(tmp_path)
    r = runner.invoke(app, ["mcp", "list-tools", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.OK
    assert _json(r)["tools"] == []


def test_mcp_list_tools_trust_gate_blocks_before_any_subprocess_spawns(
    tmp_path: Path, monkeypatch
):
    directory = _init(tmp_path)
    runner.invoke(
        app,
        [
            "add", "mcp-server", "--name", "echo",
            "--stdio-command", sys.executable, "--stdio-arg", FIXTURE,
            "--dir", directory,
        ],
    )
    monkeypatch.setenv("HIVELOOM_TRUST", "never")
    trust.revoke_trust(directory)

    def _fail_if_called(self, *args, **kwargs):
        pytest.fail("McpBridge.connect_stdio must not be called before trust is established")

    monkeypatch.setattr(McpBridge, "connect_stdio", _fail_if_called)

    r = runner.invoke(app, ["mcp", "list-tools", "--dir", directory, "--json"])
    assert r.exit_code == ExitCode.SPEC_ERROR
    assert "not trusted" in _json(r)["error"]
