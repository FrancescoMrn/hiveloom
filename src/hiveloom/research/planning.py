"""Cost and power planning, and sequential experiment rules.

A research program is small-sample by nature, so the engine says out loud what
the budget can buy and what a sample can show, and runs experiments so that
looking early is allowed where it is safe:

* **harm** may stop an experiment at any look (a significant regression is
  worth acting on as soon as it appears — checking often for harm cannot
  manufacture a false *benefit*);
* **futility** stops it when even a perfect remainder could not reach
  significance;
* **benefit** is only declared at the planned size, so peeking cannot inflate
  false positives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from hiveloom.evolve import stats

ALPHA = 0.05
#: Fractions of the planned pairs at which an experiment is looked at.
LOOKS = (1 / 3, 2 / 3, 1.0)

LookDecision = Literal["continue", "harm", "futile", "complete"]


@dataclass
class CostModel:
    runs: int
    mean_usd: float
    p90_usd: float

    @classmethod
    def from_costs(cls, costs: list[float]) -> CostModel:
        values = sorted(float(c) for c in costs)
        if not values:
            return cls(runs=0, mean_usd=0.0, p90_usd=0.0)
        index = min(len(values) - 1, math.ceil(0.9 * len(values)) - 1)
        return cls(runs=len(values), mean_usd=sum(values) / len(values), p90_usd=values[index])

    def per_experiment(self, cells: int) -> tuple[float, float]:
        return self.mean_usd * cells, self.p90_usd * cells


@dataclass
class PowerPlan:
    cells: int
    cost_mean: float
    cost_p90: float
    detectable_success_change: float
    experiments_affordable: int

    def lines(self) -> list[str]:
        return [
            f"one experiment on the working split: {self.cells} runs, "
            f"~${self.cost_mean:.4f} (p90 ${self.cost_p90:.4f})",
            f"smallest success-rate change this size can show: "
            f"±{self.detectable_success_change:.0%}; target a counted failure "
            "mechanism when the effect you expect is smaller",
            f"experiments the budget still buys at this size: {self.experiments_affordable}",
        ]


def power_plan(
    cost: CostModel, cells: int, success_rate: float, experiments_left_usd: float
) -> PowerPlan:
    mean, p90 = cost.per_experiment(cells)
    if mean > 0:
        affordable = int(experiments_left_usd // mean)
    else:
        affordable = 999  # a free executor (a scripted provider) is not the constraint
    return PowerPlan(
        cells=cells,
        cost_mean=mean,
        cost_p90=p90,
        detectable_success_change=stats.detectable_change(success_rate, cells),
        experiments_affordable=affordable,
    )


def look_sizes(planned: int) -> list[int]:
    """The cumulative number of pairs at each look, ending at ``planned``."""
    sizes: list[int] = []
    for fraction in LOOKS:
        size = max(1, math.ceil(planned * fraction))
        if not sizes or size > sizes[-1]:
            sizes.append(size)
    if sizes[-1] != planned:
        sizes[-1] = planned
    return sizes


def decide_look(
    *,
    success_improved: int,
    success_worsened: int,
    target_toward: int,
    target_against: int,
    remaining: int,
) -> LookDecision:
    """One look at an experiment in progress.

    ``success_*`` count discordant pairs on the guard (the success rate);
    ``target_toward``/``target_against`` count discordant pairs on the
    hypothesis's own target, oriented so "toward" is the predicted direction.
    """
    harm_pairs = success_improved + success_worsened
    if (
        harm_pairs
        and success_worsened > success_improved
        and stats.binomial_two_sided(success_improved, harm_pairs) < ALPHA
    ):
        return "harm"
    if remaining <= 0:
        return "complete"
    # Futility: give every remaining pair to the prediction and ask whether
    # even that could reach significance. If not, spending on the rest is waste.
    best_toward = target_toward + remaining
    best_total = best_toward + target_against
    if best_total == 0 or stats.binomial_two_sided(target_against, best_total) >= ALPHA:
        return "futile"
    return "continue"
