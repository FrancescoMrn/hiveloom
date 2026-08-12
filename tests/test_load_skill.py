"""The load_skill builtin: progressive disclosure without a filesystem reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import load_spec
from hiveloom.tools.registry import ToolError, build_registry

SKILL = """---
name: targeting
description: Turn a cohort into a confirmable proposal.
---

# Targeting

Always check marketing_consent before proposing a campaign.
"""


def _harness_with_skill(tmp_path: Path, *, declare: bool = True) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="skill-harness", task="Profile users.")
    construct.add_tool(directory, builtin="load_skill")
    skill_dir = directory / "skills" / "targeting"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL)
    if declare:
        construct.add_skill(
            directory, name="targeting", description="Turn a cohort into a proposal."
        )
    return directory


def _tool(directory: Path):
    spec = load_spec(directory / "harness.yaml")
    registry = build_registry(spec, directory)
    return registry.get("load_skill")


# --------------------------------------------------------------------------- #
# Reading declared skills
# --------------------------------------------------------------------------- #
def test_loads_a_declared_skill_without_its_frontmatter(tmp_path: Path):
    body = _tool(_harness_with_skill(tmp_path)).run(name="targeting")
    assert body.startswith("# Targeting")
    assert "Always check marketing_consent" in body
    assert "description:" not in body


def test_undeclared_skills_are_unreachable(tmp_path: Path):
    """A SKILL.md on disk that the spec does not declare stays invisible."""
    directory = _harness_with_skill(tmp_path, declare=False)
    with pytest.raises(ToolError, match="unknown skill"):
        _tool(directory).run(name="targeting")


def test_cannot_be_used_to_read_arbitrary_files(tmp_path: Path):
    directory = _harness_with_skill(tmp_path)
    (directory / ".env").write_text("ANTHROPIC_API_KEY=sk-secret")
    for attempt in ("../../etc/passwd", ".env", "skills/targeting/SKILL.md"):
        with pytest.raises(ToolError, match="unknown skill"):
            _tool(directory).run(name=attempt)


def test_schema_enumerates_the_declared_skills(tmp_path: Path):
    tool = _tool(_harness_with_skill(tmp_path))
    assert tool.input_schema["properties"]["name"]["enum"] == ["targeting"]
    assert "targeting" in tool.description


# --------------------------------------------------------------------------- #
# System-prompt wiring
# --------------------------------------------------------------------------- #
def test_skill_index_points_at_load_skill_when_present(tmp_path: Path):
    directory = _harness_with_skill(tmp_path)
    info = runner.dry_run(directory, "go")
    assert "load_skill tool" in info["system"]
    assert "file_read" not in info["system"]
    assert [t["name"] for t in info["tools"]] == ["load_skill"]


def test_skill_index_falls_back_to_file_read(tmp_path: Path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="fr", task="t")
    construct.add_tool(directory, builtin="file_read")
    skill_dir = directory / "skills" / "targeting"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL)
    construct.add_skill(directory, name="targeting", description="d")

    info = runner.dry_run(directory, "go")
    assert "file_read" in info["system"]
    assert "load_skill" not in info["system"]


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def test_model_can_load_a_skill_mid_run(tmp_path: Path):
    directory = _harness_with_skill(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("load_skill", {"name": "targeting"}, call_id="c1"),
            text_response("Consenso verificato."),
        ]
    )
    result = runner.run_harness(directory, "go", provider=provider, literal_input=True)

    assert result.status == "success"
    delivered = provider.calls[1]["messages"][-1]["content"][0]["content"]
    assert "marketing_consent" in delivered


def test_unknown_skill_surfaces_to_the_model_as_a_tool_error(tmp_path: Path):
    directory = _harness_with_skill(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("load_skill", {"name": "nope"}, call_id="c1"),
            text_response("ok"),
        ]
    )
    result = runner.run_harness(directory, "go", provider=provider, literal_input=True)
    assert result.status == "success"
    delivered = provider.calls[1]["messages"][-1]["content"][0]
    assert delivered["is_error"] is True
    assert "Available: targeting" in delivered["content"]
