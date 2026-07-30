"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveloom import construct, ext

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_HARNESS = REPO_ROOT / "harnesses" / "example-summarizer"


@pytest.fixture(autouse=True)
def _isolated_hive(tmp_path_factory, monkeypatch) -> None:
    """Point the Hive at a throwaway DB so tests never touch ~/.hiveloom."""
    db = tmp_path_factory.mktemp("hive") / "hive.db"
    monkeypatch.setenv("HIVELOOM_DB", str(db))


@pytest.fixture(autouse=True)
def _isolated_extensions(tmp_path_factory, monkeypatch) -> None:
    """Isolate the extension registry and ~/.hiveloom from the real environment."""
    home = tmp_path_factory.mktemp("hiveloom_home")
    monkeypatch.setenv("HIVELOOM_HOME", str(home))
    # Bypass the harness trust prompt; dedicated trust tests override this.
    monkeypatch.setenv("HIVELOOM_TRUST", "always")
    # A real $HIVELOOM_AUTHORIZED_KEYS in the developer/CI environment must
    # never leak into the suite — auth tests would read or write it instead
    # of a throwaway store.
    monkeypatch.delenv("HIVELOOM_AUTHORIZED_KEYS", raising=False)
    monkeypatch.setattr(ext, "_iter_entry_points", lambda: [])
    ext.reset()
    yield
    ext.reset()


@pytest.fixture()
def harness_dir(tmp_path: Path) -> Path:
    """A freshly initialized, valid harness directory."""
    directory = tmp_path / "h"
    construct.init_harness(directory, name="test-harness", task="Do a small thing.")
    return directory
