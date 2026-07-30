"""Tests for `hiveloom package`."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from hiveloom import construct
from hiveloom.errors import SpecError
from hiveloom.package import package_harness


def _harness(tmp_path: Path) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="packme", task="Do a thing.")
    (directory / ".env").write_text("ANTHROPIC_API_KEY=secret\n")
    (directory / ".hiveloom" / "traces").mkdir(parents=True, exist_ok=True)
    (directory / ".hiveloom" / "traces" / "run_x.jsonl").write_text("{}\n")
    return directory


def _names(zip_path: str) -> list[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return archive.namelist()


def test_package_creates_zip_with_lock(tmp_path: Path):
    harness = _harness(tmp_path)
    result = package_harness(harness, output_dir=tmp_path / "dist")
    zip_path = result["zip_path"]
    assert Path(zip_path).exists()
    assert result["version_hash"] in Path(zip_path).name
    names = _names(zip_path)
    assert "packme/harness.yaml" in names
    assert "packme/hiveloom.lock" in names


def test_package_excludes_secrets_and_traces(tmp_path: Path):
    harness = _harness(tmp_path)
    (harness / ".env.local").write_text("TOKEN=secret\n")
    (harness / ".env.production").write_text("TOKEN=secret\n")
    result = package_harness(harness, output_dir=tmp_path / "dist")
    names = _names(result["zip_path"])
    assert not any(Path(n).name in {".env", ".env.local", ".env.production"} for n in names)
    assert any(n.endswith(".env.example") for n in names)  # .env.example kept
    assert not any(".hiveloom" in n for n in names)  # local run memory excluded


def test_package_excludes_configured_trace_directory(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(harness, "logging.trace_dir", "local-traces")
    trace = harness / "local-traces"
    trace.mkdir()
    (trace / "run_x.jsonl").write_text("{}\n")

    result = package_harness(harness, docker=True, output_dir=tmp_path / "dist")

    assert not any("local-traces" in name for name in _names(result["zip_path"]))
    assert "local-traces/" in (harness / ".dockerignore").read_text()


def test_package_rejects_harness_root_as_trace_directory(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(harness, "logging.trace_dir", ".")

    with pytest.raises(SpecError, match="cannot be the harness root"):
        package_harness(harness, output_dir=tmp_path / "dist")


def test_package_docker_emits_dockerfile(tmp_path: Path):
    harness = _harness(tmp_path)
    result = package_harness(harness, docker=True, output_dir=tmp_path / "dist")
    assert result["dockerfile"] is True
    assert (harness / "Dockerfile").exists()
    assert (harness / ".dockerignore").exists()
    assert "packme/Dockerfile" in _names(result["zip_path"])
    assert "packme/.dockerignore" in _names(result["zip_path"])
    dockerfile = (harness / "Dockerfile").read_text()
    dockerignore = (harness / ".dockerignore").read_text()
    assert "pip install --no-cache-dir hiveloom==" in dockerfile
    assert 'ENTRYPOINT ["hiveloom", "run", ".", "--json"]' in dockerfile
    assert ".env" in dockerignore
    assert ".hiveloom/" in dockerignore


def test_package_docker_serve_variant(tmp_path: Path):
    harness = _harness(tmp_path)
    result = package_harness(harness, docker=True, serve=True, output_dir=tmp_path / "dist")
    assert result["serve"] is True
    dockerfile = (harness / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["hiveloom", "serve", ".", "--host", "0.0.0.0"' in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "ENV HIVELOOM_TRUST=always" in dockerfile


def test_package_serve_requires_docker(tmp_path: Path):
    harness = _harness(tmp_path)
    with pytest.raises(SpecError, match="requires docker=True"):
        package_harness(harness, serve=True)


def test_package_docker_embeds_runtime_wheel(tmp_path: Path):
    harness = _harness(tmp_path)
    wheel = tmp_path / "hiveloom-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"test wheel")
    result = package_harness(
        harness, docker=True, output_dir=tmp_path / "dist", runtime_wheel=wheel
    )
    assert result["embedded_runtime"] == f"runtime/{wheel.name}"
    assert f"packme/runtime/{wheel.name}" in _names(result["zip_path"])
    dockerfile = (harness / "Dockerfile").read_text()
    assert f"COPY runtime/{wheel.name} /tmp/{wheel.name}" in dockerfile
    assert f"pip install --no-cache-dir /tmp/{wheel.name}" in dockerfile


def test_package_runtime_wheel_requires_docker(tmp_path: Path):
    harness = _harness(tmp_path)
    wheel = tmp_path / "hiveloom-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"test wheel")
    with pytest.raises(SpecError, match="requires docker=True"):
        package_harness(harness, runtime_wheel=wheel)


def test_package_rejects_invalid_harness(tmp_path: Path):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "harness.yaml").write_text("name: x\n")  # missing required fields
    with pytest.raises(SpecError):
        package_harness(directory, output_dir=tmp_path / "dist")
