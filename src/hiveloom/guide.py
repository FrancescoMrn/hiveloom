"""The agent guidance and reference documents that ship with the package.

``AGENTS.md``, the ``skills/`` folder, and selected ``docs/`` references are the
guidance for an agent driving hiveloom, but an agent that ran
``pip install hiveloom`` has no repository to read them from. The wheel
therefore carries them under ``hiveloom/agent_docs/`` (the repo-root files are
the single source; the build copies them in), and ``hiveloom guide`` prints
them.

Resolution prefers the packaged copy and falls back to the repository layout,
so the command behaves identically in a checkout and in an install.
"""

from __future__ import annotations

from pathlib import Path

from hiveloom.errors import SpecError
from hiveloom.skills import parse_frontmatter

ENTRY_DOC = "AGENTS.md"
COMPACT_DOC = "SKILL.md"
SKILLS_DIR = "skills"
_SKILL_PREFIX = "hiveloom-"

REFERENCE_TOPICS = (
    (
        "confinement",
        "Product boundary: agent = model + harness, roles, guarantees, and limits.",
        "docs/task-confinement.md",
    ),
    (
        "spec",
        "Harness fields, builtins, playbooks, and enforced safety invariants.",
        "docs/spec.md",
    ),
    (
        "architecture",
        "Runtime components, data flow, journal, Hive, and evolution.",
        "docs/architecture.md",
    ),
    (
        "models",
        "Builtin model providers, pricing, caching, and custom model entries.",
        "docs/models.md",
    ),
    (
        "extending",
        "Extension packs, providers, MCP servers, hooks, skills, and SDK use.",
        "docs/extending.md",
    ),
    (
        "journal",
        "Run journals, integrity checks, forks, lineage, and model swaps.",
        "docs/journal.md",
    ),
    (
        "workbench",
        "Development UI, live run control, traces, versions, and proposals.",
        "docs/workbench.md",
    ),
    (
        "deploying",
        "Packaging, deployment topologies, trace collection, and gated evolution.",
        "docs/deploying-and-evolving.md",
    ),
    (
        "control-plane",
        "Authenticated operational HTTP API and concurrency model.",
        "docs/control-plane.md",
    ),
    (
        "sync-protocol",
        "Cloud link, trace sync, versioning, and trust model.",
        "docs/sync-protocol.md",
    ),
)


def agent_docs_dir() -> Path:
    """The directory holding the packaged guidance, skills, and references."""
    packaged = Path(__file__).resolve().parent / "agent_docs"
    if (packaged / ENTRY_DOC).exists():
        return packaged
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / ENTRY_DOC).exists():
        return repo_root
    raise SpecError(
        "agent guidance is not available in this installation "
        f"(looked in {packaged} and {repo_root})"
    )


def list_topics() -> list[dict[str, str]]:
    """Every readable entry, reference, and lifecycle-skill topic."""
    base = agent_docs_dir()
    topics = [
        {
            "name": "agents",
            "description": "Entry point: ground rules, exit codes, task-to-skill map.",
            "path": ENTRY_DOC,
        }
    ]
    if (base / COMPACT_DOC).exists():
        topics.append(
            {
                "name": "all",
                "description": "The compact all-in-one skill (every lifecycle stage in one file).",
                "path": COMPACT_DOC,
            }
        )
    for name, description, path in REFERENCE_TOPICS:
        if (base / path).exists():
            topics.append({"name": name, "description": description, "path": path})
    for skill_dir in sorted((base / SKILLS_DIR).glob(f"{_SKILL_PREFIX}*")):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        meta = parse_frontmatter(skill_file.read_text(encoding="utf-8"), str(skill_file))
        topics.append(
            {
                "name": skill_dir.name.removeprefix(_SKILL_PREFIX),
                "description": " ".join(str(meta.get("description", "")).split()),
                "path": f"{SKILLS_DIR}/{skill_dir.name}/SKILL.md",
            }
        )
    return topics


def read_topic(name: str) -> str:
    """Return the full markdown for one topic name (see :func:`list_topics`)."""
    topics = {t["name"]: t["path"] for t in list_topics()}
    if name not in topics:
        raise SpecError(f"unknown guide topic '{name}' (available: {', '.join(topics)})")
    return (agent_docs_dir() / topics[name]).read_text(encoding="utf-8")
