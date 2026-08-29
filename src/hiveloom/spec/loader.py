"""YAML <-> spec loading, round-trip-safe dumping, and code-hook resolution.

The loader is the enforcement layer: every path that creates or mutates a
harness goes through :func:`load_spec` (structural validation) and, for a full
check, :func:`resolve_hooks` (code hook import + signature check). Both fail
fast with actionable messages rather than surfacing errors mid-run.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from hiveloom.errors import SpecError
from hiveloom.spec.schema import (
    CodeGuardrailRef,
    CodeHookRef,
    CodeToolRef,
    CodeValidatorRef,
    HarnessSpec,
)

HARNESS_FILENAME = "harness.yaml"


# --------------------------------------------------------------------------- #
# YAML dumping (round-trip safe)
# --------------------------------------------------------------------------- #
class _SpecDumper(yaml.SafeDumper):
    """A dumper that keeps key order and renders multi-line strings as blocks."""


def _represent_str(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_SpecDumper.add_representer(str, _represent_str)


def spec_to_dict(spec: HarnessSpec) -> dict[str, Any]:
    """Serialize a spec to a plain dict with a stable, readable shape."""
    data = spec.model_dump(mode="json", exclude_none=True)
    # Empty lists for these fields are the default; omit them so specs that
    # predate a field keep their exact YAML shape (and version hash) on rewrite.
    for optional_list in ("extensions", "hooks", "skills"):
        if not data.get(optional_list):
            data.pop(optional_list, None)
    return data


def dump_spec(spec: HarnessSpec) -> str:
    """Serialize a spec to YAML. ``load_spec(dump_spec(s))`` is stable."""
    return yaml.dump(
        spec_to_dict(spec),
        Dumper=_SpecDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def atomic_write_text(path: str | Path, content: str) -> None:
    """Durably replace a text file without exposing a partially-written spec."""
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Loading & validation
# --------------------------------------------------------------------------- #
def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"invalid harness spec ({source}):"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def spec_from_dict(
    data: dict[str, Any], source: str = "<dict>", base_dir: Path | None = None
) -> HarnessSpec:
    """Validate a raw dict into a :class:`HarnessSpec`, raising :class:`SpecError`.

    Extensions run first — environment ones (packs, user dir) always, plus the
    dict's own ``extensions:`` when ``base_dir`` is given — so ``builtin:`` refs
    they register validate exactly like builtins.
    """
    from hiveloom import ext

    ext.ensure_environment_loaded()
    declared = data.get("extensions") or []
    if declared and base_dir is not None:
        ext.load_harness_extensions(declared, Path(base_dir))
    try:
        return HarnessSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(_format_validation_error(exc, source)) from exc


def harness_path(path: str | Path) -> Path:
    """Resolve a harness dir or file path to the ``harness.yaml`` file path."""
    p = Path(path)
    if p.is_dir():
        return p / HARNESS_FILENAME
    return p


def load_raw(path: str | Path) -> dict[str, Any]:
    """Load a harness YAML file into a raw dict (no schema validation)."""
    yaml_path = harness_path(path)
    if not yaml_path.exists():
        raise SpecError(f"no harness spec found at {yaml_path}")
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"could not parse YAML in {yaml_path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SpecError(f"{yaml_path} must contain a YAML mapping at the top level")
    return data


def load_spec(path: str | Path) -> HarnessSpec:
    """Load and structurally validate a harness spec from a dir or file path."""
    yaml_path = harness_path(path)
    return spec_from_dict(
        load_raw(yaml_path), source=str(yaml_path), base_dir=yaml_path.parent
    )


# --------------------------------------------------------------------------- #
# Code-hook resolution
# --------------------------------------------------------------------------- #
def import_hook(code_ref: str, base_dir: Path):
    """Import ``path.py:function`` relative to ``base_dir`` and return the callable."""
    rel_path, func_name = code_ref.rsplit(":", 1)
    file_path = (base_dir / rel_path).resolve()
    if not file_path.exists():
        raise SpecError(f"code hook file not found: {rel_path} (resolved: {file_path})")

    module_name = f"hiveloom_hook_{abs(hash(str(file_path)))}"
    module_spec = importlib.util.spec_from_file_location(module_name, file_path)
    if module_spec is None or module_spec.loader is None:
        raise SpecError(f"could not load code hook module: {rel_path}")
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surface any import-time failure clearly
        raise SpecError(f"error importing code hook {rel_path}: {exc}") from exc

    if not hasattr(module, func_name):
        raise SpecError(f"code hook '{func_name}' not found in {rel_path}")
    func = getattr(module, func_name)
    if not callable(func):
        raise SpecError(f"code hook {code_ref} is not callable")
    return func


def _accepts_n_params(func, minimum: int) -> bool:
    """True if ``func`` can be called with at least ``minimum`` positional args."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return True  # builtins / C callables — assume ok
    positional = 0
    has_var = False
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
        elif param.kind == inspect.Parameter.VAR_POSITIONAL:
            has_var = True
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            has_var = True
    return has_var or positional >= minimum


def resolve_hooks(spec: HarnessSpec, base_dir: str | Path) -> None:
    """Import every code hook and check its signature against the protocol.

    Raises :class:`SpecError` on the first failure with an actionable message.
    Validators must accept ``(run_output, run_context)``; tools and guardrails
    must be callable and accept at least one argument.
    """
    base = Path(base_dir)
    if base.is_file():
        base = base.parent

    for tool in spec.tools:
        if isinstance(tool, CodeToolRef):
            func = import_hook(tool.code, base)
            if not _accepts_n_params(func, 1):
                raise SpecError(
                    f"tool hook {tool.code} must accept at least one parameter"
                )

    for validator in spec.verify.validators:
        if isinstance(validator, CodeValidatorRef):
            func = import_hook(validator.code, base)
            if not _accepts_n_params(func, 2):
                raise SpecError(
                    f"validator hook {validator.code} must accept "
                    "(run_output, run_context)"
                )

    for guardrail in spec.guardrails:
        if isinstance(guardrail, CodeGuardrailRef):
            func = import_hook(guardrail.code, base)
            if not _accepts_n_params(func, 1):
                raise SpecError(
                    f"guardrail hook {guardrail.code} must accept at least one parameter"
                )

    for hook in spec.hooks:
        if isinstance(hook, CodeHookRef):
            func = import_hook(hook.code, base)
            if not _accepts_n_params(func, 1):
                raise SpecError(
                    f"event hook {hook.code} must accept the event payload parameter"
                )

    if spec.skills:
        from hiveloom.skills import load_skills

        load_skills(spec, base)  # raises SpecError on missing/invalid skills


def validate_harness(path: str | Path) -> HarnessSpec:
    """Full validation: structural spec load plus code-hook resolution."""
    yaml_path = harness_path(path)
    spec = load_spec(yaml_path)
    resolve_hooks(spec, yaml_path.parent)
    return spec
