"""Harness bundles: what the worker accepts over the network."""

from __future__ import annotations

import pytest
import yaml

from hiveloom import construct
from hiveloom.serve.bundle import BundleError, check_bundle, extract, materialize


def _bundle(harness_dir, **extra_files):
    bundle = extract(harness_dir)
    bundle["files"].update(extra_files)
    return bundle


def _with_spec(harness_dir, mutate):
    raw = yaml.safe_load((harness_dir / "harness.yaml").read_text())
    mutate(raw)
    return _bundle(harness_dir, **{"harness.yaml": yaml.safe_dump(raw)})


def test_extract_leaves_out_private_state_env_and_manifests(harness_dir):
    files = extract(harness_dir)["files"]
    assert "harness.yaml" in files
    assert not any(p.startswith(".hiveloom") or p.startswith(".env") for p in files)
    assert "pyproject.toml" not in files
    check_bundle({"files": files})


def test_roundtrip(harness_dir, tmp_path):
    bundle = _bundle(harness_dir, **{"skills/x/SKILL.md": "# x\n"})
    out = materialize(bundle, tmp_path / "copy")
    assert extract(out) == bundle


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../up.txt", "a/../../b", ".hiveloom/hive.db", ".env", "tools/t.py",
     "x.sh", "pyproject.toml", "requirements-dev.txt", "lib.so"],
)
def test_refuses_dangerous_paths(harness_dir, path):
    with pytest.raises(BundleError):
        check_bundle(_bundle(harness_dir, **{path: "x"}))


def test_requires_harness_yaml():
    with pytest.raises(BundleError, match="harness.yaml is required"):
        check_bundle({"files": {"README.md": "x"}})


def test_refuses_code_validator(harness_dir):
    def mutate(raw):
        raw.setdefault("verify", {})["validators"] = [{"code": "validators/v.py:check"}]

    with pytest.raises(BundleError, match="code hooks"):
        check_bundle(_with_spec(harness_dir, mutate))


def test_refuses_stdio_mcp_shell_hooks_extensions(harness_dir):
    def mutate(raw):
        raw["mcp_servers"] = [{"name": "local", "transport": "stdio", "command": "x"}]
        raw["tools"] = [{"builtin": "shell", "commands": ["ls"]}]
        raw["hooks"] = [{"builtin": "audit_log", "event": "run_end"}]
        raw["extensions"] = ["ext.py"]

    with pytest.raises(BundleError) as exc:
        check_bundle(_with_spec(harness_dir, mutate))
    message = str(exc.value)
    for word in ("transport http", "shell", "hooks", "extensions"):
        assert word in message


def test_accepts_http_mcp_and_builtin_validator(tmp_path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="b", task="t")
    construct.add_mcp_server(
        directory, name="app", url="https://app.example/mcp",
        header_context={"Authorization": "tok"},
    )
    construct.add_validator(directory, builtin="regex_match", pattern="ok")
    check_bundle(extract(directory))
