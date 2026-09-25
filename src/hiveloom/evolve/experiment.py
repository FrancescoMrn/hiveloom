"""Measured evolution: propose, apply, evaluate, keep or revert.

The proposal queue answers "should a human look at this"; an experiment
answers "did it work". Each round:

1. makes sure the current version has been measured on the eval (runs the eval
   once if it has not, or again with ``remeasure_baseline`` so a lucky first
   draw is not the permanent bar);
2. locates the signal and asks the strong model for one targeted, gated
   proposal — YAML only, code changes are never applied here;
3. applies it and runs the same eval on the new version;
4. assesses the new version against the proposal's own prediction, paired case
   by case (:mod:`hiveloom.evolve.assess`);
5. keeps a *confirmed* change and reverts a *regressed* or *refuted* one.
   *Inconclusive* is reverted unless ``keep_inconclusive``: an unproven change
   is not an improvement, it is a coin flip the harness now carries.

Every decision is recorded on the evolution row, and the next round's attempt
history carries it, so a refuted idea is not proposed again unchanged. The
safety layer is untouched: the same gate, the same frozen paths, the same
validation and rollback as any other apply. Spending is the operator's
explicit choice — ``rounds`` is bounded, and each round costs up to three
strong-model calls plus one or two eval runs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from hiveloom.errors import SpecError
from hiveloom.generate.llm import StrongModel
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import atomic_write_text, harness_path, load_spec

from .analyzer import analyze
from .assess import Assessment, assess_evolution
from .evolver import ProposalError, apply_proposal, propose, read_counter

MAX_ROUNDS = 10

RoundStatus = Literal[
    "kept", "reverted", "nothing_to_evolve", "no_proposal", "not_applied"
]


class RoundResult(BaseModel):
    """What one experiment round tried and decided."""

    round: int
    status: RoundStatus
    reason: str = ""
    old_version: str = ""
    new_version: str = ""
    rationale: str = ""
    target: dict[str, Any] | None = None
    changed_paths: list[str] = Field(default_factory=list)
    baseline_eval_run_id: str | None = None
    candidate_eval_run_id: str | None = None
    assessment: Assessment | None = None
    cost_usd: float = 0.0


#: Runs an eval document and returns its manifest (``eval_run_id``, cells).
EvalRunner = Callable[[], Any]


def _eval_cost(manifest: Any) -> float:
    return float(sum(getattr(cell, "cost_usd", 0.0) or 0.0 for cell in manifest.cells))


def run_experiment(
    harness_dir: str | Path,
    eval_path: str | Path,
    model: StrongModel,
    *,
    rounds: int = 1,
    keep_inconclusive: bool = False,
    remeasure_baseline: bool = False,
    notes: list[str] | None = None,
    hive: Hive | None = None,
    eval_runner: EvalRunner | None = None,
    approve_trust: Callable[[Path], bool] | None = None,
    on_round: Callable[[RoundResult], None] | None = None,
) -> list[RoundResult]:
    """Run up to ``rounds`` measured evolution rounds on one harness. See the module doc."""
    from hiveloom.eval_runner import run_eval
    from hiveloom.evals import resolve_eval_spec

    if not 1 <= rounds <= MAX_ROUNDS:
        raise SpecError(f"rounds must be between 1 and {MAX_ROUNDS}")
    yaml_path = harness_path(harness_dir)
    base = yaml_path.parent
    validated, _cases = resolve_eval_spec(eval_path, approve_trust=approve_trust)
    eval_dir = (
        validated.harness_path if validated.harness_path.is_dir()
        else validated.harness_path.parent
    )
    if eval_dir.resolve() != base.resolve():
        raise SpecError(
            f"eval {eval_path} runs harness {eval_dir}, not {base}; an experiment must "
            "measure the harness it changes"
        )
    eval_id = validated.identity.eval_id
    cells = validated.case_count * validated.spec.repetitions
    min_runs = max(1, min(5, cells))
    runner = eval_runner or (lambda: run_eval(eval_path, approve_trust=approve_trust))

    own_hive = hive is None
    hive = hive or Hive()
    results: list[RoundResult] = []
    try:
        for number in range(1, rounds + 1):
            spec = load_spec(yaml_path)
            key = spec.identity
            version = spec_version_hash(spec, base)
            result = RoundResult(round=number, status="nothing_to_evolve", old_version=version)

            if remeasure_baseline or hive.completed_eval_cells(key, version, eval_id) == 0:
                manifest = runner()
                result.baseline_eval_run_id = manifest.eval_run_id
                result.cost_usd += _eval_cost(manifest)

            report = analyze(
                hive,
                key,
                version=version,
                excerpt_config=spec.evolution.trace_excerpts,
                redaction=spec.logging.redact,
                objectives=spec.evolution.objectives,
                analyst_notes=notes,
                evolution=spec.evolution,
            )
            if report.is_empty():
                result.reason = "the current version has no failures or metric evidence"
                results.append(result)
                if on_round:
                    on_round(result)
                break

            try:
                proposal = propose(spec, report, model)
            except ProposalError as exc:
                result.status = "no_proposal"
                result.reason = str(exc)
                results.append(result)
                if on_round:
                    on_round(result)
                break
            result.rationale = proposal.rationale
            result.target = proposal.target.model_dump() if proposal.target else None

            before_text = yaml_path.read_text(encoding="utf-8")
            applied = apply_proposal(yaml_path, proposal, hive=hive, apply_yaml=True)
            if not applied.changed:
                result.status = "not_applied"
                result.reason = "; ".join(
                    f"{item['path']}: {item['reason']}" for item in applied.rejected
                ) or "the proposal gated to no applicable YAML change"
                results.append(result)
                if on_round:
                    on_round(result)
                continue
            result.new_version = applied.new_version_hash
            result.changed_paths = [change.path for change in applied.applied_yaml]

            manifest = runner()
            result.candidate_eval_run_id = manifest.eval_run_id
            result.cost_usd += _eval_cost(manifest)

            evolution = next(
                row for row in hive.evolutions(key)
                if row["new_version_hash"] == applied.new_version_hash
                and row["old_version_hash"] == applied.old_version_hash
            )
            assessment = assess_evolution(hive, key, evolution, min_runs=min_runs)
            result.assessment = assessment
            keep = assessment.verdict == "confirmed" or (
                keep_inconclusive and assessment.verdict in ("inconclusive", "pending")
            )
            if keep:
                result.status = "kept"
                result.reason = assessment.summary
            else:
                atomic_write_text(yaml_path, before_text)
                hive.record_evolution(
                    key,
                    applied.new_version_hash,
                    applied.old_version_hash,
                    read_counter(yaml_path),
                    f"revert: {assessment.verdict}",
                    datetime.now(UTC).isoformat(),
                    changes={"revert_of": evolution["evolution_id"]},
                )
                result.status = "reverted"
                result.reason = assessment.summary
            hive.record_evolution_decision(
                evolution["evolution_id"],
                {
                    "action": result.status,
                    "verdict": assessment.verdict,
                    "eval_id": eval_id,
                    "eval_run_ids": [
                        rid for rid in (result.baseline_eval_run_id, result.candidate_eval_run_id)
                        if rid
                    ],
                    "decided_at": datetime.now(UTC).isoformat(),
                },
            )
            results.append(result)
            if on_round:
                on_round(result)
    finally:
        if own_hive:
            hive.close()
    return results
