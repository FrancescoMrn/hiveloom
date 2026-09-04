"""Generator: a strong model drives the construction API to build a harness.

The meta-prompt is assembled at runtime from the pydantic schema, the annotated
template, and the builtin catalog, so the contract can never drift. The model
returns a JSON *construction plan* (not YAML); the plan is replayed through the
same validated ``init``/``set``/``add`` functions the CLI uses, with a
validate → repair loop (up to 2 repairs) as the final gate.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from hiveloom import construct
from hiveloom.catalog import CATALOGS
from hiveloom.errors import HiveloomError
from hiveloom.generate.llm import StrongModel
from hiveloom.spec import annotate
from hiveloom.spec.loader import load_spec, validate_harness
from hiveloom.spec.schema import HarnessSpec

_PROMPT_PATH = Path(__file__).parent / "prompts" / "harness_contract.md"
_ENV_PATTERN = re.compile(r"os\.(?:environ\[|getenv\()\s*['\"]([A-Z_][A-Z0-9_]*)['\"]")


class PlanError(HiveloomError):
    """Raised when a construction plan is malformed."""


# --------------------------------------------------------------------------- #
# Meta-prompt assembly (schema is the single source of truth)
# --------------------------------------------------------------------------- #
def build_builtin_catalog() -> str:
    """Render the catalog (builtins + extensions) as markdown for the meta-prompt."""
    from hiveloom import ext

    ext.ensure_environment_loaded()
    lines: list[str] = []
    # Eval loaders and scorers have their own document; they are visible via
    # `hiveloom catalog` but are not harness construction entries.
    for kind in ("tools", "guardrails", "validators", "policies", "compaction", "hooks"):
        entries = CATALOGS[kind]
        lines.append(f"### {kind}")
        for entry in entries.values():
            params = ", ".join(
                f"{p.name}:{p.type}{'*' if p.required else ''}" for p in entry.params
            )
            suffix = f" (params: {params})" if params else ""
            lines.append(f"- **{entry.name}** — {entry.description}{suffix}")
        lines.append("")
    return "\n".join(lines).strip()


def build_meta_prompt(blueprint: str | None = None) -> str:
    """Assemble the generation meta-prompt from the schema-derived contract.

    ``blueprint`` is expanded markdown appended as a binding house-style
    section (preferred tools, guardrail posture, validator patterns).
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    # Use replace (not str.format) so JSON braces in the template are safe.
    prompt = (
        template.replace("{json_schema}", json.dumps(annotate.json_schema(), indent=2))
        .replace("{annotated_template}", annotate.annotated_template().rstrip())
        .replace("{builtin_catalog}", build_builtin_catalog())
    )
    if blueprint:
        prompt += (
            "\n\n## Blueprint (house style — follow these directions)\n\n"
            + blueprint.strip()
        )
    return prompt


def expand_blueprint(text: str, task: str) -> str:
    """Fill a blueprint's argument slots from the task description.

    ``$ARGUMENTS`` / ``$@`` expand to the whole task; ``$1``..``$9`` to its
    whitespace-split words (empty when absent).
    """
    words = task.split()
    expanded = text.replace("$ARGUMENTS", task).replace("$@", task)
    for i in range(9, 0, -1):  # highest first so $1 does not eat $12
        expanded = expanded.replace(f"${i}", words[i - 1] if i <= len(words) else "")
    return expanded


def resolve_blueprint(name: str, task: str) -> str:
    """Look up a blueprint by name and expand its slots, or raise :class:`PlanError`."""
    from hiveloom import ext

    text = ext.get_blueprint(name)
    if text is None:
        available = ", ".join(ext.blueprint_names()) or "none"
        raise PlanError(
            f"unknown blueprint '{name}' (available: {available}). Blueprints live "
            "in ~/.hiveloom/blueprints/<name>.md or come from extension packs."
        )
    return expand_blueprint(text, task)


# --------------------------------------------------------------------------- #
# Plan parsing & execution
# --------------------------------------------------------------------------- #
def parse_plan(text: str) -> dict[str, Any]:
    """Parse a construction plan from model text (tolerating code fences)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    try:
        plan = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PlanError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict) or "name" not in plan or "task" not in plan:
        raise PlanError("plan must be a JSON object with 'name' and 'task'")
    if not isinstance(plan.get("steps", []), list):
        raise PlanError("plan 'steps' must be a list")
    return plan


def _apply_step(directory: Path, step: dict[str, Any]) -> None:
    op = step.get("op")
    if op == "set":
        construct.set_value(directory, step["path"], step["value"])
    elif op == "add_tool":
        construct.add_tool(
            directory,
            builtin=step.get("builtin"),
            code=step.get("code"),
            description=step.get("description"),
        )
    elif op == "add_validator":
        if step.get("builtin") == "output_schema":
            _ensure_schema(directory, step.get("schema_file", "./schemas/output.json"))
        construct.add_validator(
            directory,
            builtin=step.get("builtin"),
            code=step.get("code"),
            description=step.get("description"),
            schema_file=step.get("schema_file"),
            pattern=step.get("pattern"),
            path=step.get("path"),
            command=step.get("command"),
        )
    elif op == "add_guardrail":
        construct.add_guardrail(
            directory,
            builtin=step["builtin"],
            value=step.get("value"),
            pattern=step.get("pattern"),
        )
    else:
        raise PlanError(f"unknown plan op: {op!r}")


def _ensure_schema(directory: Path, schema_file: str) -> None:
    """Scaffold a permissive JSON schema if an output_schema validator needs one."""
    target = directory / schema_file
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"type": "object"}, indent=2) + "\n", encoding="utf-8"
    )


def _execute_plan(output_dir: Path, plan: dict[str, Any]) -> HarnessSpec:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    construct.init_harness(output_dir, name=plan["name"], task=plan["task"])
    for step in plan.get("steps", []):
        _apply_step(output_dir, step)
    return validate_harness(output_dir)


def _finalize(output_dir: Path) -> list[str]:
    """Scan code hooks for env vars and append them to .env.example."""
    found: set[str] = set()
    for sub in ("tools", "validators"):
        directory = output_dir / sub
        if not directory.exists():
            continue
        for py_file in directory.glob("*.py"):
            found.update(_ENV_PATTERN.findall(py_file.read_text(encoding="utf-8")))

    env_example = output_dir / ".env.example"
    existing = env_example.read_text(encoding="utf-8") if env_example.exists() else ""
    lines = [line for line in existing.splitlines() if line.strip()]
    known = {line.split("=", 1)[0] for line in lines}
    for var in sorted(found):
        if var not in known:
            lines.append(f"{var}=")
    env_example.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sorted(found)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def generate(
    task_description: str,
    output_dir: str | Path,
    model: StrongModel | None = None,
    *,
    model_id: str | None = None,
    available_tools: list[str] | None = None,
    max_repairs: int = 2,
    blueprint: str | None = None,
) -> HarnessSpec:
    """Generate a harness for ``task_description`` into ``output_dir``.

    A strong model produces a construction plan; hiveloom replays it through the
    validated construction API. Pass a ``model`` implementation for embedding
    and tests, or let hiveloom resolve ``model_id`` (the default when omitted)
    exactly as the CLI does. Supplying both is an error. On failure the error is
    fed back to the model for up to ``max_repairs`` self-corrections.
    ``blueprint`` names a reusable house-style prompt fragment
    (``hiveloom generate --blueprint``).
    """
    directory = Path(output_dir)
    if model is not None and model_id is not None:
        raise PlanError("pass either model or model_id, not both")
    if model is None:
        from hiveloom.generate.llm import build_strong_model

        model = build_strong_model(model_id, directory)
    blueprint_text = (
        resolve_blueprint(blueprint, task_description) if blueprint else None
    )
    system = build_meta_prompt(blueprint_text)
    tool_names = available_tools or sorted(CATALOGS["tools"])
    user = (
        f"Task: {task_description}\n"
        f"Available builtin tools: {', '.join(tool_names)}\n\n"
        "Produce the construction plan JSON."
    )

    last_error: Exception | None = None
    for attempt in range(max_repairs + 1):
        if attempt == 0:
            prompt = user
        else:
            prompt = (
                user
                + f"\n\nYour previous plan failed with this error:\n{last_error}\n"
                "Return a corrected construction plan (JSON only)."
            )
        response = model.generate(system=system, user=prompt)
        try:
            plan = parse_plan(response)
            spec = _execute_plan(directory, plan)
            _finalize(directory)
            return spec
        except (PlanError, HiveloomError) as exc:
            last_error = exc
    raise PlanError(f"generation failed after {max_repairs} repairs: {last_error}")


def load_generated(output_dir: str | Path) -> HarnessSpec:
    """Convenience: load the spec that was generated into ``output_dir``."""
    return load_spec(output_dir)
