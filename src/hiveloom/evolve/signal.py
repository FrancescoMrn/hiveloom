"""Signal location: where, in the Hive's evidence, a harness change could matter.

Evolution used to hand one model a bag of failure counts and ask it to
"diagnose the failed layer". That confuses two jobs. Finding *where* the
evidence points — which tool, step, playbook, memory entry or input segment
separates the runs that failed from the runs that did not — is counting, and
counting is cheap, deterministic and testable. Deciding *what to change* there
is judgement, and is the only part worth a paid model call.

:func:`locate_signal` does the counting. For one harness version it:

* contrasts failing and successful runs feature by feature (Fisher's exact
  test, Benjamini-Hochberg across features), so a cause that only shows up as
  a difference — a tool the good runs call and the bad runs skip — is found as
  readily as one that shows up as an error;
* counts the mechanisms (friction per category and component) that a change
  aimed at one of them would be judged by, rather than the noisy success rate;
* says how much the sample can resolve at all: the success-rate interval and
  the smallest change this many runs could detect, so an underpowered
  population is reported as underpowered instead of mined for patterns;
* attributes each failed run to a loss class, which bounds how much any
  harness change can buy before a proposal is written.

Nothing here calls a model, and nothing here is evidence of causation: a
feature that separates failures is a place to look, and a prediction to test
(see :mod:`hiveloom.evolve.assess`), not a verdict.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from hiveloom.logging.hive import Hive
from hiveloom.spec.schema import ALWAYS_FROZEN, EvolutionConfig

from . import stats

# Where each feature family can be changed from. The first mutable lever is the
# one the proposer is pointed at; the rest are alternatives. `model` is listed
# so a signal on the executor is reported honestly as out of evolution's reach.
_FAMILY_LEVERS: dict[str, tuple[str, ...]] = {
    "tool": ("tools", "loop.steps", "system_prompt"),
    "tool_error": ("tools", "system_prompt", "memory.entries", "loop.steps"),
    "spilled": ("context.tool_results", "system_prompt", "memory.entries"),
    "step": ("loop.steps", "system_prompt"),
    "step_violation": ("loop.steps", "system_prompt"),
    "playbook": ("playbooks",),
    "notes": ("system_prompt",),
    "memory": ("memory.entries",),
    "delegation": ("delegation",),
    "model": ("model",),
    "input": ("system_prompt", "playbooks", "loop.steps"),
}
_FRICTION_LEVERS: dict[str, tuple[str, ...]] = {
    "tool_error": ("tools", "system_prompt", "memory.entries"),
    "retry": ("tools", "system_prompt"),
    "output_truncated": ("system_prompt", "loop.policy"),
    "context_compaction": ("context.strategy", "context.compaction", "loop.max_turns"),
    "user_steer": ("system_prompt", "memory.entries"),
    "guardrail_block": ("system_prompt",),
    "provider_error": ("model",),
}
_STATUS_LEVERS: dict[str, tuple[str, ...]] = {
    "max_turns": ("loop.max_turns", "loop.policy", "system_prompt"),
    "truncated": ("system_prompt", "loop.policy"),
    "verify_failed": ("system_prompt", "memory.entries", "tools"),
    "step_failed": ("loop.steps", "system_prompt"),
    "guardrail_halt": ("system_prompt",),
    "error": (),
}

# Loss classes, in attribution priority: a failed run is charged to the first
# class it qualifies for. "content" is what is left when nothing on the way
# went wrong — the model finished and was wrong, which prompts and memory can
# sometimes reach but plumbing never will.
LossClass = Literal["provider", "guardrail", "limits", "process", "tooling", "content", "other"]
_ADDRESSABLE_CLASSES = ("guardrail", "limits", "process", "tooling")

Verdict = Literal["no_runs", "no_failures", "underpowered", "actionable", "suggestive", "diffuse"]


class FeatureSignal(BaseModel):
    """One feature's association with failure in this version's runs."""

    feature: str
    family: str
    direction: Literal["risk", "protective"]
    failures_with: int
    successes_with: int
    failures_without: int
    successes_without: int
    failure_rate_with: float
    failure_rate_without: float
    p_value: float
    q_value: float
    strength: Literal["strong", "suggestive", "weak"]
    levers: list[str] = Field(default_factory=list)
    addressable: bool = False
    aliases: list[str] = Field(
        default_factory=list,
        description="Other features present in exactly the same runs; one signal, several names.",
    )

    def describe(self) -> str:
        presence = "present" if self.direction == "risk" else "absent"
        with_n = self.failures_with + self.successes_with
        without_n = self.failures_without + self.successes_without
        lever = ", ".join(self.levers) if self.levers else "no spec lever"
        reach = "" if self.addressable else " (not mutable by evolution)"
        return (
            f"{self.feature}: failure rate {self.failure_rate_with:.0%} of {with_n} runs "
            f"with it vs {self.failure_rate_without:.0%} of {without_n} without "
            f"(risk when {presence}; p={self.p_value:.3g}, q={self.q_value:.3g}, "
            f"{self.strength}) — lever: {lever}{reach}"
        )


class MechanismCount(BaseModel):
    """A countable failure mechanism: friction of one kind at one component."""

    target: str
    category: str
    component: str = ""
    events: int
    runs: int
    failed_runs: int
    recovered_events: int
    share_of_failures: float
    levers: list[str] = Field(default_factory=list)
    addressable: bool = False


class SignalQuality(BaseModel):
    """How much this population can resolve at all."""

    runs: int
    failures: int
    successes: int
    success_rate: float
    success_rate_ci: tuple[float, float]
    detectable_change: float = Field(
        description="Smallest absolute success-rate change detectable with this "
        "many runs per version (alpha 0.05, power 0.8)."
    )
    runs_per_version_for_10_points: int
    avg_cost_usd: float
    avg_turns: float
    unindexed_runs: int = Field(
        description="Runs ingested before feature indexing; invisible to the contrast."
    )


class LossAttribution(BaseModel):
    """What the failed runs lost to, and so what a harness change could buy."""

    failed_runs: int
    classes: dict[str, int] = Field(default_factory=dict)
    addressable_share: float = 0.0
    content_share: float = 0.0


class SignalMap(BaseModel):
    """Where the evidence for one harness version points, and how strongly."""

    harness_name: str
    version: str | None = None
    verdict: Verdict
    headline: list[str] = Field(default_factory=list)
    quality: SignalQuality
    loss: LossAttribution
    signals: list[FeatureSignal] = Field(default_factory=list)
    mechanisms: list[MechanismCount] = Field(default_factory=list)
    statuses: dict[str, int] = Field(default_factory=dict)
    targets: list[str] = Field(
        default_factory=list,
        description="Every id a proposal may name as its target signal.",
    )

    def knows_target(self, target: str) -> bool:
        return target in self.targets or target.startswith("metric:")


def _family(feature: str) -> str:
    return feature.split(":", 1)[0]


# When several features mark exactly the same runs they are one signal. The
# representative is the most specific name with the most direct lever: a tool
# error beats the friction row derived from it, a component beats its category.
_FAMILY_PRIORITY = (
    "tool_error", "step_violation", "step", "tool", "spilled", "memory", "playbook",
    "delegation", "notes", "input", "model", "friction",
)


def _representative_key(feature: str) -> tuple[int, int, str]:
    family = _family(feature)
    rank = _FAMILY_PRIORITY.index(family) if family in _FAMILY_PRIORITY else len(_FAMILY_PRIORITY)
    specific = 0 if "@" in feature or family != "friction" else 1
    return (rank, specific, feature)


def _levers_for(target: str) -> tuple[str, ...]:
    family, _, rest = target.partition(":")
    if family == "friction":
        return _FRICTION_LEVERS.get(rest.split("@", 1)[0], ("system_prompt",))
    if family == "status":
        return _STATUS_LEVERS.get(rest, ())
    if target == "success_rate":
        return ("system_prompt", "memory.entries", "tools", "loop.policy")
    return _FAMILY_LEVERS.get(family, ("system_prompt",))


def _covered(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    path_cf = path.casefold()
    return any(
        path_cf == p.casefold() or path_cf.startswith(p.casefold() + ".") for p in patterns
    )


def _touches(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    path_cf = path.casefold()
    return any(
        path_cf == p.casefold()
        or path_cf.startswith(p.casefold() + ".")
        or p.casefold().startswith(path_cf + ".")
        for p in patterns
    )


def _reachable(
    levers: tuple[str, ...], evolution: EvolutionConfig | None
) -> tuple[list[str], bool]:
    """The levers, mutable ones first, and whether any is within evolution's reach."""
    if evolution is None:
        return list(levers), False
    frozen = [*ALWAYS_FROZEN, *evolution.frozen]
    mutable = [
        lever for lever in levers
        if _covered(lever, evolution.mutable) and not _touches(lever, frozen)
    ]
    rest = [lever for lever in levers if lever not in mutable]
    return mutable + rest, bool(mutable)


def _loss_class(run: dict[str, Any]) -> str:
    features: set[str] = run["features"]
    status = run["status"]
    if status == "error" or any(f.startswith("friction:provider_error") for f in features):
        return "provider"
    if status == "guardrail_halt":
        return "guardrail"
    if status in ("max_turns", "truncated"):
        return "limits"
    if status == "step_failed" or any(
        f.startswith(("step_violation:", "step:")) for f in features
    ):
        return "process"
    if any(
        f.startswith(("tool_error:", "friction:tool_error", "friction:retry"))
        for f in features
    ):
        return "tooling"
    if status == "verify_failed" or run.get("outcome") == "failure":
        return "content"
    return "other"


def _strength(p: float, q: float) -> Literal["strong", "suggestive", "weak"]:
    if q <= 0.1 and p <= 0.05:
        return "strong"
    if p <= 0.1:
        return "suggestive"
    return "weak"


def locate_signal(
    hive: Hive,
    harness_name: str,
    *,
    version: str | None = None,
    evolution: EvolutionConfig | None = None,
    max_signals: int = 12,
    max_mechanisms: int = 12,
    min_support: int = 2,
) -> SignalMap:
    """Rank where this harness version's evidence points. See the module docstring.

    ``evolution`` (the spec's section) marks which levers evolution may
    actually pull; without it every signal is reported as not addressable
    rather than guessed at. ``min_support`` drops features seen in fewer runs
    than that, which could never reach significance and only add noise to the
    multiple-comparison correction.
    """
    population = hive.feature_population(harness_name, version=version)
    runs = len(population)
    failed = [run for run in population if run["failed"]]
    passed = [run for run in population if not run["failed"]]
    n_fail, n_pass = len(failed), len(passed)

    rate = n_pass / runs if runs else 0.0
    per_arm = runs  # a comparison against the next version, at this size
    quality = SignalQuality(
        runs=runs,
        failures=n_fail,
        successes=n_pass,
        success_rate=rate,
        success_rate_ci=stats.wilson_interval(n_pass, runs),
        detectable_change=stats.detectable_change(rate, per_arm),
        runs_per_version_for_10_points=stats.runs_needed(rate, 0.10),
        avg_cost_usd=(sum(r["cost_usd"] for r in population) / runs) if runs else 0.0,
        avg_turns=(sum(r["turns"] for r in population) / runs) if runs else 0.0,
        unindexed_runs=sum(1 for r in population if not r["indexed"]),
    )

    classes = Counter(_loss_class(run) for run in failed)
    addressable = sum(classes[c] for c in _ADDRESSABLE_CLASSES)
    loss = LossAttribution(
        failed_runs=n_fail,
        classes=dict(classes.most_common()),
        addressable_share=(addressable / n_fail) if n_fail else 0.0,
        content_share=(classes["content"] / n_fail) if n_fail else 0.0,
    )
    statuses = dict(Counter(run["status"] for run in failed).most_common())

    # ---- contrast --------------------------------------------------------- #
    indexed_fail = [r for r in failed if r["indexed"]]
    indexed_pass = [r for r in passed if r["indexed"]]
    members: dict[str, set[str]] = {}
    for run in (*indexed_fail, *indexed_pass):
        for feature in run["features"]:
            members.setdefault(feature, set()).add(run["run_id"])
    failed_ids = {run["run_id"] for run in indexed_fail}
    groups: dict[frozenset[str], list[str]] = {}
    for feature, runs_with in members.items():
        groups.setdefault(frozenset(runs_with), []).append(feature)
    candidates: list[tuple[str, int, int]] = []
    aliases: dict[str, list[str]] = {}
    for runs_with, names in groups.items():
        a = len(runs_with & failed_ids)
        b = len(runs_with) - a
        if a + b < min_support:
            continue
        # A feature in every run cannot separate anything.
        if a == len(indexed_fail) and b == len(indexed_pass):
            continue
        names.sort(key=_representative_key)
        candidates.append((names[0], a, b))
        aliases[names[0]] = names[1:]
    p_values = [
        stats.fisher_exact(a, b, len(indexed_fail) - a, len(indexed_pass) - b)
        for _, a, b in candidates
    ]
    q_values = stats.benjamini_hochberg(p_values)
    signals: list[FeatureSignal] = []
    for (feature, a, b), p, q in zip(candidates, p_values, q_values, strict=True):
        c, d = len(indexed_fail) - a, len(indexed_pass) - b
        rate_with = a / (a + b) if a + b else 0.0
        rate_without = c / (c + d) if c + d else 0.0
        levers, reachable = _reachable(_levers_for(feature), evolution)
        signals.append(
            FeatureSignal(
                feature=feature,
                family=_family(feature),
                direction="risk" if rate_with >= rate_without else "protective",
                failures_with=a,
                successes_with=b,
                failures_without=c,
                successes_without=d,
                failure_rate_with=rate_with,
                failure_rate_without=rate_without,
                p_value=p,
                q_value=q,
                strength=_strength(p, q),
                levers=levers,
                addressable=reachable,
                aliases=aliases[feature],
            )
        )
    signals.sort(key=lambda s: (s.p_value, -abs(s.failure_rate_with - s.failure_rate_without)))
    signals = signals[:max_signals]

    # ---- mechanisms --------------------------------------------------------- #
    mechanisms: list[MechanismCount] = []
    for row in hive.friction_by_component(harness_name, version=version)[:max_mechanisms]:
        category, component = row["category"], row["component"] or ""
        target = f"friction:{category}" + (f"@{component}" if component else "")
        levers, reachable = _reachable(_levers_for(f"friction:{category}"), evolution)
        mechanisms.append(
            MechanismCount(
                target=target,
                category=category,
                component=component,
                events=row["events"],
                runs=row["runs"],
                failed_runs=row["failed_runs"],
                recovered_events=row["recovered"] or 0,
                share_of_failures=(row["failed_runs"] / n_fail) if n_fail else 0.0,
                levers=levers,
                addressable=reachable,
            )
        )

    # ---- verdict ------------------------------------------------------------ #
    verdict: Verdict
    if runs == 0:
        verdict = "no_runs"
    elif n_fail == 0:
        verdict = "no_failures"
    elif len(indexed_fail) < 2 or len(indexed_pass) < 2:
        verdict = "underpowered"
    elif any(s.strength == "strong" for s in signals):
        verdict = "actionable"
    elif any(s.strength == "suggestive" for s in signals):
        verdict = "suggestive"
    else:
        verdict = "diffuse"

    targets = ["success_rate"]
    targets += [f"status:{status}" for status in statuses]
    for s in signals:
        targets += [s.feature, *s.aliases]
    targets += [m.target for m in mechanisms if m.target not in targets]

    signal_map = SignalMap(
        harness_name=harness_name,
        version=version,
        verdict=verdict,
        quality=quality,
        loss=loss,
        signals=signals,
        mechanisms=mechanisms,
        statuses=statuses,
        targets=targets,
    )
    signal_map.headline = _headline(signal_map)
    return signal_map


_VERDICT_TEXT: dict[str, str] = {
    "no_runs": "No finished runs of this version yet: nothing to locate.",
    "no_failures": (
        "No failures in this version. Any change is an opportunity, not a fix; "
        "name what it should improve and how that will be measured."
    ),
    "underpowered": (
        "Too few failing or passing runs to contrast them. Count mechanisms instead "
        "of comparing rates, or collect more runs before changing anything."
    ),
    "actionable": (
        "At least one feature separates failures from successes beyond chance "
        "(q <= 0.1). Aim the change at it and predict how its count will move."
    ),
    "suggestive": (
        "Some features lean toward failure but none survive correction for the "
        "number tested. Treat them as hypotheses; prefer a change whose effect "
        "is directly countable."
    ),
    "diffuse": (
        "No feature separates failures from successes. The loss is spread out, "
        "which points at task content (knowledge, reasoning) rather than one "
        "piece of plumbing."
    ),
}


def _headline(signal_map: SignalMap) -> list[str]:
    q = signal_map.quality
    lines: list[str] = []
    if q.runs:
        low, high = q.success_rate_ci
        lines.append(
            f"{q.runs} runs: {q.successes} succeeded ({q.success_rate:.0%}, 95% CI "
            f"{low:.0%}-{high:.0%}). At this size only a success-rate change of about "
            f"{q.detectable_change:.0%} or more is detectable; "
            f"{q.runs_per_version_for_10_points} runs per version would detect 10 points."
        )
    if q.unindexed_runs:
        lines.append(
            f"{q.unindexed_runs} run(s) predate feature indexing and are left out of the "
            "contrast; re-ingest their traces to include them."
        )
    loss = signal_map.loss
    if loss.failed_runs:
        parts = ", ".join(f"{count} {name}" for name, count in loss.classes.items())
        lines.append(
            f"Failures by loss class: {parts}. About {loss.addressable_share:.0%} show a "
            f"harness-level cause; {loss.content_share:.0%} finished and were wrong."
        )
    for signal in signal_map.signals[:3]:
        if signal.strength != "weak":
            lines.append(signal.describe())
    for mechanism in signal_map.mechanisms[:2]:
        if mechanism.failed_runs:
            lines.append(
                f"Mechanism {mechanism.target}: {mechanism.events} events in "
                f"{mechanism.runs} runs, {mechanism.failed_runs} of them failed "
                f"({mechanism.share_of_failures:.0%} of failures)."
            )
    lines.append(_VERDICT_TEXT[signal_map.verdict])
    return lines
