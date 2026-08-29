"""hiveloom — generate, run, and evolve agent harnesses on the fly.

The *hive* is the collective memory of runs; the *loom* weaves harnesses from
that memory. This package exposes the harness spec, its loader, the
construction API that the CLI and generator both drive, and the embedding SDK:

    from hiveloom import run_harness

    result = run_harness("./my-harness", "input.txt",
                         on_event=lambda e: print(e.type))

SDK surface (semver-stable): :func:`run_harness`, :func:`dry_run`,
:class:`RunResult`, :func:`generate_harness`, :func:`load_spec`,
:func:`validate_harness`, :class:`HarnessSpec`, :class:`Hive`,
:class:`HarnessServer`. The other, language-agnostic embedding interfaces are
``hiveloom run --stream`` (trace events as JSONL on stdout, final
``run_result`` line last) and ``hiveloom serve`` (the same stream over HTTP).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "1.0.0"

from hiveloom.spec.schema import HarnessSpec

if TYPE_CHECKING:
    from hiveloom.generate.generator import generate as generate_harness  # noqa: F401
    from hiveloom.logging.hive import Hive  # noqa: F401
    from hiveloom.loop.agent_loop import RunResult  # noqa: F401
    from hiveloom.runner import dry_run, run_harness  # noqa: F401
    from hiveloom.serve import HarnessServer  # noqa: F401
    from hiveloom.spec.loader import load_spec, validate_harness  # noqa: F401

_SDK = {
    "run_harness": ("hiveloom.runner", "run_harness"),
    "dry_run": ("hiveloom.runner", "dry_run"),
    "RunResult": ("hiveloom.loop.agent_loop", "RunResult"),
    # named generate_harness: plain `generate` would shadow the subpackage
    "generate_harness": ("hiveloom.generate.generator", "generate"),
    "load_spec": ("hiveloom.spec.loader", "load_spec"),
    "validate_harness": ("hiveloom.spec.loader", "validate_harness"),
    "Hive": ("hiveloom.logging.hive", "Hive"),
    "HarnessServer": ("hiveloom.serve", "HarnessServer"),
    # Code-tool authoring surface: return a ToolResult carrying Artifacts to
    # hand structured output to the embedding caller.
    "Artifact": ("hiveloom.tools.registry", "Artifact"),
    "ToolResult": ("hiveloom.tools.registry", "ToolResult"),
}


def __getattr__(name: str) -> Any:
    """Lazy SDK exports: keep ``import hiveloom`` light for spec-only users."""
    if name in _SDK:
        import importlib

        module_name, attr = _SDK[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'hiveloom' has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include lazy SDK exports in introspection and interactive completion."""
    return sorted({*globals(), *_SDK})


__all__ = ["HarnessSpec", "__version__", *sorted(_SDK)]
