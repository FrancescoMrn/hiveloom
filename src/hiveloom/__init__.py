"""hiveloom — generate, run, and evolve agent harnesses on the fly.

The *hive* is the collective memory of runs; the *loom* weaves harnesses from
that memory. This package exposes the harness spec, its loader, the
construction API that the CLI and generator both drive, and the embedding SDK:

    from hiveloom import run_harness

    result = run_harness("./my-harness", "input.txt",
                         on_event=lambda e: print(e.type))

SDK surface (semver-stable): :func:`run_harness`, :func:`dry_run`,
:class:`RunResult`, :func:`generate_harness`, :func:`load_spec`,
:func:`validate_harness`, :func:`migrate_harness`, :class:`HarnessSpec`, :class:`Hive`,
:class:`RunMetric`, :func:`record_run_metrics`, :class:`EvalSpec`,
:func:`run_scorers`, :class:`EvalManifest`, :func:`run_eval`,
:func:`resume_eval`, :func:`build_eval_report`, :func:`compare_evals`,
:class:`ModelProbeResult`, :func:`probe_model`,
:class:`HarnessServer`, :class:`VerificationContext`. The
other, language-agnostic embedding interfaces are
``hiveloom run --stream`` (trace events as JSONL on stdout, final
``run_result`` line last) and ``hiveloom serve`` (the same stream over HTTP).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "1.0.0"

from hiveloom.spec.schema import HarnessSpec

if TYPE_CHECKING:
    from hiveloom.eval_reports import build_eval_report, compare_evals  # noqa: F401
    from hiveloom.eval_runner import EvalManifest, resume_eval, run_eval  # noqa: F401
    from hiveloom.evals import (  # noqa: F401
        DatasetLoader,
        EvalCase,
        EvalSpec,
        Scorer,
        ScorerContext,
        ScorerOutput,
        ScoringResult,
        load_eval_spec,
        run_scorers,
    )
    from hiveloom.execution import (  # noqa: F401
        RunExecutionEnvelope,
        StepExecutionRecord,
        VerificationSummary,
    )
    from hiveloom.generate.generator import generate as generate_harness  # noqa: F401
    from hiveloom.logging.hive import Hive  # noqa: F401
    from hiveloom.loop.agent_loop import RunResult  # noqa: F401
    from hiveloom.metrics import RunMetric, record_run_metrics  # noqa: F401
    from hiveloom.models.capabilities import ModelProbeResult, probe_model  # noqa: F401
    from hiveloom.runner import dry_run, run_harness  # noqa: F401
    from hiveloom.serve import HarnessServer  # noqa: F401
    from hiveloom.spec.loader import load_spec, validate_harness  # noqa: F401
    from hiveloom.spec.migrate import migrate_harness  # noqa: F401
    from hiveloom.verify.base import (  # noqa: F401
        ToolEvidenceRecord,
        VerificationContext,
    )

_SDK = {
    "run_harness": ("hiveloom.runner", "run_harness"),
    "dry_run": ("hiveloom.runner", "dry_run"),
    "RunResult": ("hiveloom.loop.agent_loop", "RunResult"),
    "RunExecutionEnvelope": ("hiveloom.execution", "RunExecutionEnvelope"),
    "StepExecutionRecord": ("hiveloom.execution", "StepExecutionRecord"),
    "ToolEvidenceRecord": ("hiveloom.verify.base", "ToolEvidenceRecord"),
    "VerificationContext": ("hiveloom.verify.base", "VerificationContext"),
    "VerificationSummary": ("hiveloom.execution", "VerificationSummary"),
    "ModelProbeResult": ("hiveloom.models.capabilities", "ModelProbeResult"),
    "probe_model": ("hiveloom.models.capabilities", "probe_model"),
    # named generate_harness: plain `generate` would shadow the subpackage
    "generate_harness": ("hiveloom.generate.generator", "generate"),
    "load_spec": ("hiveloom.spec.loader", "load_spec"),
    "validate_harness": ("hiveloom.spec.loader", "validate_harness"),
    "migrate_harness": ("hiveloom.spec.migrate", "migrate_harness"),
    "Hive": ("hiveloom.logging.hive", "Hive"),
    "RunMetric": ("hiveloom.metrics", "RunMetric"),
    "record_run_metrics": ("hiveloom.metrics", "record_run_metrics"),
    "DatasetLoader": ("hiveloom.evals", "DatasetLoader"),
    "EvalCase": ("hiveloom.evals", "EvalCase"),
    "EvalSpec": ("hiveloom.evals", "EvalSpec"),
    "Scorer": ("hiveloom.evals", "Scorer"),
    "ScorerContext": ("hiveloom.evals", "ScorerContext"),
    "ScorerOutput": ("hiveloom.evals", "ScorerOutput"),
    "ScoringResult": ("hiveloom.evals", "ScoringResult"),
    "load_eval_spec": ("hiveloom.evals", "load_eval_spec"),
    "run_scorers": ("hiveloom.evals", "run_scorers"),
    "EvalManifest": ("hiveloom.eval_runner", "EvalManifest"),
    "run_eval": ("hiveloom.eval_runner", "run_eval"),
    "resume_eval": ("hiveloom.eval_runner", "resume_eval"),
    "build_eval_report": ("hiveloom.eval_reports", "build_eval_report"),
    "compare_evals": ("hiveloom.eval_reports", "compare_evals"),
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
