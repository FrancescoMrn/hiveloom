"""hiveloom — generate, run, and evolve agent harnesses on the fly.

The *hive* is the collective memory of runs; the *loom* weaves harnesses from
that memory. This package exposes the harness spec, its loader, the
construction API that the CLI and generator both drive, and the embedding SDK:

    from hiveloom import run_harness

    result = run_harness("./my-harness", "input.txt",
                         on_event=lambda e: print(e.type))

SDK surface (semver-stable): :func:`run_harness`, :func:`dry_run`,
:class:`RunResult`, :func:`generate_harness`, :func:`load_spec`,
:func:`validate_harness`, :class:`HarnessSpec`, :class:`Hive`. The other,
language-agnostic embedding interface is ``hiveloom run --stream`` (trace
events as JSONL on stdout, final ``run_result`` line last).
"""

__version__ = "0.2.0"

from hiveloom.spec.schema import HarnessSpec

_SDK = {
    "run_harness": ("hiveloom.runner", "run_harness"),
    "dry_run": ("hiveloom.runner", "dry_run"),
    "RunResult": ("hiveloom.loop.agent_loop", "RunResult"),
    # named generate_harness: plain `generate` would shadow the subpackage
    "generate_harness": ("hiveloom.generate.generator", "generate"),
    "load_spec": ("hiveloom.spec.loader", "load_spec"),
    "validate_harness": ("hiveloom.spec.loader", "validate_harness"),
    "Hive": ("hiveloom.logging.hive", "Hive"),
}


def __getattr__(name: str):
    """Lazy SDK exports: keep ``import hiveloom`` light for spec-only users."""
    if name in _SDK:
        import importlib

        module_name, attr = _SDK[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module 'hiveloom' has no attribute {name!r}")


__all__ = ["HarnessSpec", "__version__", *sorted(_SDK)]
