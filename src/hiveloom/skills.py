"""Skills: progressive-disclosure capability files for the executor.

A skill is a ``skills/<name>/SKILL.md`` folder inside the harness (following
the Agent Skills convention): YAML frontmatter with ``name`` and
``description``, then freeform instructions. Only the name + description enter
the system prompt; the model reads the full file on demand with ``file_read``.
For a small executor on a tight token budget this moves instructions out of
the always-paid system prompt into pay-on-demand files.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from hiveloom.errors import SpecError
from hiveloom.spec.schema import HarnessSpec

SKILLS_DIR = "skills"
SKILL_FILENAME = "SKILL.md"


class Skill(BaseModel):
    """One loadable skill: its index entry plus where the full text lives."""

    name: str
    description: str
    path: str  # relative to the harness dir, e.g. skills/pdf-report/SKILL.md


def skill_path(name: str) -> str:
    return f"{SKILLS_DIR}/{name}/{SKILL_FILENAME}"


def parse_frontmatter(text: str, source: str) -> dict:
    """Parse the leading ``---`` YAML frontmatter block of a SKILL.md."""
    if not text.startswith("---"):
        raise SpecError(f"{source}: SKILL.md must start with '---' YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SpecError(f"{source}: unterminated frontmatter (missing closing '---')")
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise SpecError(f"{source}: invalid frontmatter YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{source}: frontmatter must be a YAML mapping")
    return data


def load_skill(base: Path, name: str) -> Skill:
    rel = skill_path(name)
    file_path = base / rel
    if not file_path.exists():
        raise SpecError(f"skill '{name}' not found: expected {rel}")
    front = parse_frontmatter(file_path.read_text(encoding="utf-8"), source=rel)
    description = str(front.get("description", "")).strip()
    if not description:
        raise SpecError(f"{rel}: frontmatter needs a non-empty 'description'")
    return Skill(name=str(front.get("name", name)), description=description, path=rel)


def load_skills(spec: HarnessSpec, base_dir: str | Path) -> list[Skill]:
    """Load and validate every skill the spec declares."""
    base = Path(base_dir)
    if base.is_file():
        base = base.parent
    return [load_skill(base, name) for name in spec.skills]


def skill_index(skills: list[Skill]) -> str:
    """The system-prompt section listing available skills."""
    if not skills:
        return ""
    lines = [
        "# Skills",
        "When a skill below matches the task, read its file with the file_read "
        "tool and follow its instructions.",
    ]
    lines += [f"- {s.name} ({s.path}): {s.description}" for s in skills]
    return "\n".join(lines)
