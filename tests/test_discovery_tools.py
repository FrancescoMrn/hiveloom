"""The confined read-only discovery builtins: list_dir, glob, grep."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom.cli import app
from hiveloom.models.provider import ToolCall
from hiveloom.spec.schema import BuiltinToolRef, HarnessSpec
from hiveloom.tools.builtin import GlobTool, GrepTool, ListDirTool
from hiveloom.tools.registry import ToolError, build_registry


def _tree(base: Path) -> None:
    """A small repository with the things the tools must show and must hide."""
    (base / "src").mkdir()
    (base / "src" / "main.py").write_text("import os\nprint('needle')\n", encoding="utf-8")
    (base / "src" / "util").mkdir()
    (base / "src" / "util" / "helpers.py").write_text("def needle():\n    pass\n", encoding="utf-8")
    (base / "src" / "util" / "deep").mkdir()
    (base / "src" / "util" / "deep" / "leaf.txt").write_text("leaf needle\n", encoding="utf-8")
    (base / "README.md").write_text("# Project\nNEEDLE in caps\n", encoding="utf-8")
    (base / "node_modules").mkdir()
    (base / "node_modules" / "dep.js").write_text("needle in a dependency\n", encoding="utf-8")
    (base / ".hiveloom" / "traces").mkdir(parents=True)
    (base / ".hiveloom" / "traces" / "run.jsonl").write_text('{"needle": 1}\n', encoding="utf-8")
    (base / ".env").write_text("API_KEY=needle-secret\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# list_dir
# --------------------------------------------------------------------------- #
def test_list_dir_lists_the_top_level_sorted_with_directories_marked(tmp_path: Path):
    _tree(tmp_path)
    out = ListDirTool(tmp_path).run(path=".")
    assert out.splitlines() == ["README.md", "src/"]


def test_list_dir_depth_descends_breadth_first(tmp_path: Path):
    _tree(tmp_path)
    out = ListDirTool(tmp_path).run(path=".", depth=2)
    assert out.splitlines() == ["README.md", "src/", "src/main.py", "src/util/"]
    # Deeper than the cap is clamped, never an error.
    deep = ListDirTool(tmp_path).run(path="src", depth=99).splitlines()
    assert "src/util/deep/leaf.txt" in deep


def test_list_dir_hides_private_and_dependency_trees(tmp_path: Path):
    _tree(tmp_path)
    out = ListDirTool(tmp_path).run(path=".", depth=4)
    assert ".hiveloom" not in out
    assert ".env" not in out
    assert "node_modules" not in out
    with pytest.raises(ToolError, match="protected harness state"):
        ListDirTool(tmp_path).run(path=".hiveloom/traces")


def test_list_dir_errors_are_explicit(tmp_path: Path):
    _tree(tmp_path)
    with pytest.raises(ToolError, match="not a directory"):
        ListDirTool(tmp_path).run(path="README.md")
    with pytest.raises(ToolError, match="file not found"):
        ListDirTool(tmp_path).run(path="missing")
    with pytest.raises(ToolError, match="escapes"):
        ListDirTool(tmp_path).run(path="..")
    with pytest.raises(ToolError, match="depth must be a number"):
        ListDirTool(tmp_path).run(path=".", depth="two")


def test_list_dir_output_is_bounded(tmp_path: Path):
    for i in range(520):
        (tmp_path / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    lines = ListDirTool(tmp_path).run(path=".").splitlines()
    assert len(lines) == 501
    assert lines[-1].startswith("... stopped at 500 entries")


# --------------------------------------------------------------------------- #
# glob
# --------------------------------------------------------------------------- #
def test_glob_matches_relative_paths_across_directories(tmp_path: Path):
    _tree(tmp_path)
    out = GlobTool(tmp_path).run(pattern="*.py")
    assert out.splitlines() == ["src/main.py", "src/util/helpers.py"]
    assert GlobTool(tmp_path).run(pattern="*.py", path="src/util").splitlines() == [
        "src/util/helpers.py"
    ]
    assert GlobTool(tmp_path).run(pattern="*.rs") == "no matches"


def test_glob_hides_private_and_dependency_trees(tmp_path: Path):
    _tree(tmp_path)
    out = GlobTool(tmp_path).run(pattern="*")
    assert "node_modules" not in out
    assert ".env" not in out
    assert "traces" not in out
    with pytest.raises(ToolError, match="escapes"):
        GlobTool(tmp_path).run(pattern="*", path="../")
    with pytest.raises(ToolError, match="pattern"):
        GlobTool(tmp_path).run(pattern="")


def test_glob_output_is_bounded(tmp_path: Path):
    for i in range(520):
        (tmp_path / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    lines = GlobTool(tmp_path).run(pattern="*.txt").splitlines()
    assert len(lines) == 501
    assert lines[-1].startswith("... stopped at 500 matches")


# --------------------------------------------------------------------------- #
# grep
# --------------------------------------------------------------------------- #
def test_grep_reports_path_line_and_text(tmp_path: Path):
    _tree(tmp_path)
    out = GrepTool(tmp_path).run(pattern="needle")
    # Shallow files first: the same breadth-first order list_dir and glob use.
    assert out.splitlines() == [
        "src/main.py:2: print('needle')",
        "src/util/helpers.py:1: def needle():",
        "src/util/deep/leaf.txt:1: leaf needle",
    ]
    assert GrepTool(tmp_path).run(pattern="needle", glob="*.py").splitlines() == [
        "src/main.py:2: print('needle')",
        "src/util/helpers.py:1: def needle():",
    ]
    assert GrepTool(tmp_path).run(pattern="needle", path="README.md") == "no matches"
    assert GrepTool(tmp_path).run(pattern="needle", path="README.md", ignore_case=True) == (
        "README.md:2: NEEDLE in caps"
    )


def test_grep_never_reads_private_or_dependency_files(tmp_path: Path):
    _tree(tmp_path)
    out = GrepTool(tmp_path).run(pattern="needle")
    assert "secret" not in out
    assert "dependency" not in out
    assert "traces" not in out
    with pytest.raises(ToolError, match="protected harness state"):
        GrepTool(tmp_path).run(pattern="needle", path=".env")


def test_grep_skips_binary_and_oversized_files(tmp_path: Path):
    (tmp_path / "blob.bin").write_bytes(b"needle\x00binary")
    (tmp_path / "huge.log").write_text("needle\n" + "x" * 1_100_000, encoding="utf-8")
    (tmp_path / "small.txt").write_text("needle here\n", encoding="utf-8")
    assert GrepTool(tmp_path).run(pattern="needle") == "small.txt:1: needle here"


def test_grep_bounds_matches_and_line_length(tmp_path: Path):
    (tmp_path / "long.txt").write_text("needle " + "y" * 400 + "\n", encoding="utf-8")
    line = GrepTool(tmp_path).run(pattern="needle")
    assert len(line) < 400
    (tmp_path / "many.txt").write_text("needle\n" * 50, encoding="utf-8")
    lines = GrepTool(tmp_path).run(pattern="needle", max_matches=3).splitlines()
    assert len(lines) == 4
    assert lines[-1].startswith("... stopped at 3 matches")
    assert len(GrepTool(tmp_path).run(pattern="needle", max_matches=9999).splitlines()) == 51


def test_grep_rejects_bad_patterns(tmp_path: Path):
    with pytest.raises(ToolError, match="invalid regular expression"):
        GrepTool(tmp_path).run(pattern="(")
    with pytest.raises(ToolError, match="pattern"):
        GrepTool(tmp_path).run(pattern="")
    with pytest.raises(ToolError, match="pattern"):
        GrepTool(tmp_path).run(pattern="a" * 300)
    with pytest.raises(ToolError, match="max_matches must be a number"):
        GrepTool(tmp_path).run(pattern="x", max_matches="lots")
    # The guard transform_result's grep already applies to model-written patterns.
    with pytest.raises(ToolError, match="backtrack"):
        GrepTool(tmp_path).run(pattern="(a+)+$")


# --------------------------------------------------------------------------- #
# Review findings: names, symlinks, ** patterns, line numbers, scan bounds
# --------------------------------------------------------------------------- #
def test_pruning_is_by_directory_name_only(tmp_path: Path):
    (tmp_path / "target").write_text("a file named like a build tree\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.js").write_text("built\n", encoding="utf-8")
    assert ListDirTool(tmp_path).run(path=".").splitlines() == ["target"]
    assert GrepTool(tmp_path).run(pattern="named") == "target:1: a file named like a build tree"
    # An explicit path into a pruned tree still works: the prune is for walks.
    assert ListDirTool(tmp_path).run(path="build").splitlines() == ["build/out.js"]


def test_symlinked_directories_are_shown_but_not_descended(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "inner.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    listing = ListDirTool(tmp_path).run(path=".", depth=4).splitlines()
    assert listing == ["link/", "loop/", "real/", "real/inner.txt"]
    assert GlobTool(tmp_path).run(pattern="*.txt") == "real/inner.txt"
    assert GrepTool(tmp_path).run(pattern="needle") == "real/inner.txt:1: needle"


def test_glob_accepts_double_star_and_a_file_path(tmp_path: Path):
    _tree(tmp_path)
    assert GlobTool(tmp_path).run(pattern="**/*.py").splitlines() == [
        "src/main.py",
        "src/util/helpers.py",
    ]
    assert GlobTool(tmp_path).run(pattern="src/**/helpers.py") == "src/util/helpers.py"
    # `**/` is zero or more whole directories, never a prefix of a name.
    (tmp_path / "src" / "xmain.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "util" / "deep" / "main.py").write_text("", encoding="utf-8")
    assert GlobTool(tmp_path).run(pattern="src/**/main.py").splitlines() == [
        "src/main.py",
        "src/util/deep/main.py",
    ]
    assert GlobTool(tmp_path).run(pattern="*.md", path="README.md") == "README.md"
    assert GrepTool(tmp_path).run(pattern="Project", path="README.md", glob="*.md") == (
        "README.md:1: # Project"
    )


def test_grep_counts_newlines_only(tmp_path: Path):
    (tmp_path / "ff.txt").write_text("one\x0ctwo\nneedle\n", encoding="utf-8")
    assert GrepTool(tmp_path).run(pattern="needle") == "ff.txt:2: needle"


def test_walks_are_bounded_in_work_not_only_output(tmp_path: Path, monkeypatch):
    import hiveloom.tools.builtin as builtin

    for i in range(30):
        (tmp_path / f"f{i:02d}.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(builtin, "_DISCOVERY_MAX_VISITED", 10)
    assert GlobTool(tmp_path).run(pattern="*.zzz").splitlines() == [builtin._SCAN_LIMIT_NOTE]
    assert GrepTool(tmp_path).run(pattern="nothing").splitlines() == [builtin._SCAN_LIMIT_NOTE]
    listing = ListDirTool(tmp_path).run(path=".").splitlines()
    assert len(listing) == 11 and listing[-1] == builtin._SCAN_LIMIT_NOTE


# --------------------------------------------------------------------------- #
# Spec wiring: a declared tool is built with the run's private boundary
# --------------------------------------------------------------------------- #
def test_build_registry_wires_the_discovery_tools(tmp_path: Path):
    (tmp_path / "run_logs").mkdir()
    (tmp_path / "run_logs" / "run_x.jsonl").write_text('{"output": "distinctive-secret"}\n')
    (tmp_path / "notes.txt").write_text("distinctive-public\n", encoding="utf-8")
    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "tools": [{"builtin": "list_dir"}, {"builtin": "glob"}, {"builtin": "grep"}],
            "logging": {"trace_dir": "run_logs"},
        }
    )
    registry = build_registry(spec, tmp_path)
    assert {"list_dir", "glob", "grep"} <= set(registry.active_names())
    assert all("read" in registry.get(name).tags for name in ("list_dir", "glob", "grep"))
    listing = registry.dispatch(ToolCall(id="1", name="list_dir", input={"path": "."}))
    assert not listing.is_error and "run_logs" not in listing.content
    found = registry.dispatch(ToolCall(id="2", name="glob", input={"pattern": "*"}))
    assert found.content == "notes.txt"
    hits = registry.dispatch(ToolCall(id="3", name="grep", input={"pattern": "distinctive"}))
    assert "distinctive-secret" not in hits.content
    assert hits.content == "notes.txt:1: distinctive-public"
    assert BuiltinToolRef(builtin="grep").params() == {}


def test_catalog_lists_the_discovery_tools():
    result = CliRunner().invoke(app, ["catalog", "tools", "--json"])
    assert result.exit_code == 0
    names = {entry["name"] for entry in json.loads(result.stdout)["entries"]}
    assert {"list_dir", "glob", "grep"} <= names
