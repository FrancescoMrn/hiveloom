"""Assessment: did an applied evolution do what it predicted?

Every applied evolution is a claim. Since signal location, a proposal names
the signal it aims at and the direction that signal should move
(:class:`~hiveloom.evolve.evolver.SignalTarget`); the Hive keeps the claim
beside the old and new version hashes. :func:`assess_evolution` checks it
against the runs each version has actually produced:

* **Target first.** The share of runs carrying the targeted signal (a tool
  error, a status, a friction mechanism, the success rate itself) or the mean
  of a targeted metric, before versus after. Counting the mechanism a change
  was aimed at settles questions at sample sizes where the overall success
  rate cannot.
* **Success as the guard.** A change that moves its target but significantly
  lowers the success rate has *regressed*, whatever it fixed.
* **Paired when possible.** When both versions ran the same eval cases (same
  eval, case and repetition), outcomes are compared pair by pair — McNemar's
  exact test for rates, the sign test for metrics — which is what makes small
  evals decisive. Otherwise the two populations are compared unpaired.
* **Honest about power.** Too few runs of the new version is *pending*; an
  effect too small for the sample to show is *inconclusive*, and says how many
  runs would settle it. A prediction with a stated size that the sample could
  have detected and did not is *refuted*.

Verdicts are evidence for a human (or for :mod:`hiveloom.evolve.experiment`)
to act on. Nothing here changes a harness.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive

from . import stats
from .analyzer import MAX_ATTEMPT_HISTORY, AttemptRecord, queue_record

Verdict = Literal["pending", "confirmed", "refuted", "regressed", "inconclusive"]
ALPHA = 0.05
#: Pairs needed before a comparison is made pair by pair rather than unpaired.
MIN_PAIRS = 5


class Measure(BaseModel):
    """One quantity before and after, and whether the difference is real."""

    quantity: str
    before: float | None = None
    after: float | None = None
    before_n: int = 0
    after_n: int = 0
    before_count: int | None = None
    after_count: int | None = None
    test: Literal["mcnemar", "fisher", "sign", "welch", "none"] = "none"
    pairs: int = 0
    improved_pairs: int | None = Field(
        default=None, description="Pairs where the quantity went up (paired tests only)."
    )
    worsened_pairs: int | None = Field(
        default=None, description="Pairs where the quantity went down (paired tests only)."
    )
    p_value: float = 1.0

    def moved(self) -> Literal["increase", "decrease", "none"]:
        if self.before is None or self.after is None or self.p_value > ALPHA:
            return "none"
        if self.after > self.before:
            return "increase"
        if self.after < self.before:
            return "decrease"
        return "none"

    def describe(self) -> str:
        if self.before is None or self.after is None:
            return f"{self.quantity}: not measurable yet"
        if self.before_count is not None and self.after_count is not None:
            values = (
                f"{self.before_count}/{self.before_n} -> {self.after_count}/{self.after_n}"
            )
        else:
            values = f"{self.before:.3g} (n={self.before_n}) -> {self.after:.3g} (n={self.after_n})"
        paired = f", {self.pairs} pairs" if self.pairs else ""
        return f"{self.quantity}: {values} ({self.test}{paired}, p={self.p_value:.3g})"


class Assessment(BaseModel):
    """What one applied evolution predicted and what the runs since say."""

    evolution_id: int
    proposal_id: str | None = None
    old_version: str
    new_version: str
    counter: int | None = None
    created_at: str | None = None
    rationale: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    target: str
    expect: Literal["increase", "decrease"]
    predicted_by: float | None = None
    implicit_target: bool = Field(
        default=False,
        description="True when the evolution named no target and is judged on success rate.",
    )
    verdict: Verdict
    target_measure: Measure
    success: Measure
    runs_needed: int | None = Field(
        default=None,
        description="Runs per version that would detect the predicted (or a 10-point) change.",
    )
    decision: dict[str, Any] | None = None
    summary: str = ""

    def measured(self) -> dict[str, Any]:
        """Compact, prompt-safe measurement for attempt history."""
        result: dict[str, Any] = {
            "verdict": self.verdict,
            "target": f"{self.target} expected to {self.expect}",
            "target_measure": self.target_measure.describe(),
            "success": self.success.describe(),
        }
        if self.runs_needed:
            result["runs_needed"] = self.runs_needed
        if self.decision:
            result["decision"] = self.decision.get("action")
        return result


# --------------------------------------------------------------------------- #
# Measuring
# --------------------------------------------------------------------------- #
def _rate_indicator(
    hive: Hive, target: str, runs: list[dict[str, Any]]
) -> dict[str, bool]:
    """Per run: does it carry ``target`` (True/False)."""
    if target == "success_rate":
        return {run["run_id"]: not run["failed"] for run in runs}
    family, _, detail = target.partition(":")
    if family == "status":
        return {run["run_id"]: run["status"] == detail for run in runs}
    if family == "friction":
        category, _, component = detail.partition("@")
        hits = hive.runs_with_friction(
            [run["run_id"] for run in runs], category, component or None
        )
        return {run["run_id"]: run["run_id"] in hits for run in runs}
    return {run["run_id"]: target in run["features"] for run in runs}


def _measure_rate(
    quantity: str,
    before: dict[str, bool],
    after: dict[str, bool],
    pairs: list[tuple[str, str]],
) -> Measure:
    usable = [(left, right) for left, right in pairs if left in before and right in after]
    before_k, after_k = sum(before.values()), sum(after.values())
    measure = Measure(
        quantity=quantity,
        before=(before_k / len(before)) if before else None,
        after=(after_k / len(after)) if after else None,
        before_n=len(before),
        after_n=len(after),
        before_count=before_k,
        after_count=after_k,
    )
    if not before or not after:
        return measure
    if len(usable) >= MIN_PAIRS:
        up = sum(1 for left, right in usable if not before[left] and after[right])
        down = sum(1 for left, right in usable if before[left] and not after[right])
        measure.test = "mcnemar"
        measure.pairs = len(usable)
        measure.improved_pairs, measure.worsened_pairs = up, down
        measure.p_value = stats.binomial_two_sided(up, up + down)
    else:
        measure.test = "fisher"
        measure.p_value = stats.fisher_exact(
            before_k, len(before) - before_k, after_k, len(after) - after_k
        )
    return measure


def _measure_metric(
    quantity: str,
    before: dict[str, float],
    after: dict[str, float],
    pairs: list[tuple[str, str]],
) -> Measure:
    measure = Measure(
        quantity=quantity,
        before=(sum(before.values()) / len(before)) if before else None,
        after=(sum(after.values()) / len(after)) if after else None,
        before_n=len(before),
        after_n=len(after),
    )
    if not before or not after:
        return measure
    usable = [(left, right) for left, right in pairs if left in before and right in after]
    if len(usable) >= MIN_PAIRS:
        up = sum(1 for left, right in usable if after[right] > before[left])
        down = sum(1 for left, right in usable if after[right] < before[left])
        measure.test = "sign"
        measure.pairs = len(usable)
        measure.improved_pairs, measure.worsened_pairs = up, down
        measure.p_value = stats.binomial_two_sided(up, up + down)
        return measure
    # Unpaired: Welch's z on the two means. Approximate at small n, which is
    # why paired evals are preferred and a verdict here needs a clear margin.
    if len(before) < 2 or len(after) < 2:
        return measure

    def variance(values: list[float]) -> float:
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / (len(values) - 1)

    se = math.sqrt(
        variance(list(before.values())) / len(before)
        + variance(list(after.values())) / len(after)
    )
    measure.test = "welch"
    if se == 0:
        measure.p_value = 1.0 if measure.before == measure.after else 0.0
    else:
        z = abs((measure.after or 0.0) - (measure.before or 0.0)) / se
        measure.p_value = math.erfc(z / math.sqrt(2))
    return measure


def _target_of(evolution: dict[str, Any]) -> tuple[str, str, float | None, bool]:
    """(target, expect, by, implicit) for one evolution record."""
    prediction = evolution.get("prediction") or {}
    target = prediction.get("target") if isinstance(prediction, dict) else None
    if isinstance(target, dict) and target.get("signal") and target.get("expect"):
        return str(target["signal"]), str(target["expect"]), target.get("by"), False
    for expectation in (prediction or {}).get("objective_expectations") or []:
        if isinstance(expectation, dict) and expectation.get("metric"):
            return (
                f"metric:{expectation['metric']}",
                str(expectation.get("expected_change") or "increase"),
                None,
                False,
            )
    # Every change implicitly claims it does not make the harness worse and
    # hopes it makes it better.
    return "success_rate", "increase", None, True


def assess_evolution(
    hive: Hive,
    harness_name: str,
    evolution: dict[str, Any],
    *,
    min_runs: int = 5,
) -> Assessment:
    """Assess one row of :meth:`Hive.evolutions` against the runs since."""
    old, new = evolution["old_version_hash"], evolution["new_version_hash"]
    before_runs = hive.feature_population(harness_name, version=old)
    after_runs = hive.feature_population(harness_name, version=new)
    pairs = [
        (pair["left"]["run_id"], pair["right"]["run_id"])
        for pair in hive.eval_pairs(harness_name, old, new)
    ]
    target, expect, by, implicit = _target_of(evolution)

    success = _measure_rate(
        "success_rate",
        _rate_indicator(hive, "success_rate", before_runs),
        _rate_indicator(hive, "success_rate", after_runs),
        pairs,
    )
    if target == "success_rate":
        target_measure = success
    elif target.startswith("metric:"):
        name = target.split(":", 1)[1]
        target_measure = _measure_metric(
            target,
            hive.metric_values([run["run_id"] for run in before_runs], name),
            hive.metric_values([run["run_id"] for run in after_runs], name),
            pairs,
        )
    else:
        target_measure = _measure_rate(
            f"share of runs with {target}",
            _rate_indicator(hive, target, before_runs),
            _rate_indicator(hive, target, after_runs),
            pairs,
        )

    base_rate = target_measure.before if target_measure.before is not None else 0.5
    change = by if (by and not target.startswith("metric:")) else 0.10
    runs_needed = stats.runs_needed(min(max(base_rate, 0.0), 1.0), min(change, 1.0))
    detectable = stats.detectable_change(base_rate, min(len(before_runs), len(after_runs)))

    verdict: Verdict
    if len(after_runs) < min_runs:
        verdict = "pending"
        note = f"{len(after_runs)} run(s) of the new version; {min_runs} needed to assess."
    elif success.moved() == "decrease":
        verdict = "regressed"
        note = "The success rate fell significantly, whatever the target did."
    elif target_measure.moved() == expect:
        verdict = "confirmed"
        note = f"{target} moved as predicted ({expect})."
    elif target_measure.moved() not in ("none", expect):
        verdict = "refuted"
        note = f"{target} moved significantly the other way."
    elif by and not target.startswith("metric:") and detectable <= by:
        verdict = "refuted"
        note = (
            f"A change of {by:.0%} was predicted and this sample could detect "
            f"{detectable:.0%}; no significant change appeared."
        )
    else:
        verdict = "inconclusive"
        note = (
            f"No significant change; about {runs_needed} runs per version would detect "
            f"a {change:.0%} change."
        )

    changes = evolution.get("changes") or {}
    changed_paths = [*(changes.get("paths") or []), *(changes.get("code") or [])]
    summary = (
        f"{verdict}: {note} {target_measure.describe()}"
        + ("" if target == "success_rate" else f"; {success.describe()}")
    )
    return Assessment(
        evolution_id=int(evolution.get("evolution_id") or 0),
        proposal_id=evolution.get("proposal_id"),
        old_version=old,
        new_version=new,
        counter=evolution.get("counter"),
        created_at=evolution.get("created_at"),
        rationale=str(evolution.get("rationale") or ""),
        changed_paths=changed_paths,
        target=target,
        expect=expect,  # type: ignore[arg-type]
        predicted_by=by,
        implicit_target=implicit,
        verdict=verdict,
        target_measure=target_measure,
        success=success,
        runs_needed=runs_needed if verdict in ("inconclusive", "pending") else None,
        decision=evolution.get("decision"),
        summary=summary,
    )


def is_revert(evolution: dict[str, Any]) -> bool:
    changes = evolution.get("changes") or {}
    return bool(changes.get("revert_of"))


def assess_all(
    hive: Hive, harness_name: str, *, min_runs: int = 5, limit: int = MAX_ATTEMPT_HISTORY
) -> list[Assessment]:
    """Assess the newest applied evolutions of a harness (reverts excluded)."""
    rows = [row for row in hive.evolutions(harness_name) if not is_revert(row)]
    return [
        assess_evolution(hive, harness_name, row, min_runs=min_runs) for row in rows[:limit]
    ]


def attempt_history(
    hive: Hive, harness_name: str, *, min_runs: int = 5
) -> list[AttemptRecord]:
    """Search memory with measurements: applied evolutions, assessed, plus rejections.

    Replaces the unmeasured queue ledger as evolution's default history. Every
    applied evolution (queued or ``--yes``) appears with its verdict — or, when
    an experiment decided to keep or revert it, with that decision — so the
    proposer sees which ideas were tried *and what happened*, not only that
    someone clicked apply. Rejected proposals still appear, unmeasured.
    """
    records: list[tuple[str, AttemptRecord]] = []
    linked: set[str] = set()
    by_id = {row.get("evolution_id"): row for row in hive.evolutions(harness_name)}
    for assessment in assess_all(hive, harness_name, min_runs=min_runs):
        if assessment.proposal_id:
            linked.add(assessment.proposal_id)
        changes = by_id.get(assessment.evolution_id, {}).get("changes") or {}
        decision = assessment.decision or {}
        records.append(
            (
                assessment.created_at or "",
                AttemptRecord(
                    outcome=str(decision.get("action") or assessment.verdict),
                    rationale=assessment.rationale,
                    changed_paths=assessment.changed_paths,
                    yaml_diff=str(changes.get("yaml_diff") or ""),
                    measured=assessment.measured(),
                    version_hash=assessment.new_version,
                    note=assessment.summary,
                ),
            )
        )
    for row in hive.list_proposals(harness_name):
        if row.get("id") in linked:
            continue
        record = queue_record(row)
        if record is not None:
            stamp = str(row.get("resolved_at") or row.get("created_at") or "")
            records.append((stamp, record))
    records.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in records[:MAX_ATTEMPT_HISTORY]]
