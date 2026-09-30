"""Research program building blocks: charter, ledger, budget pools, planning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hiveloom.errors import SpecError
from hiveloom.research.budget import Budget
from hiveloom.research.charter import Charter, check_program_name, load_charter
from hiveloom.research.ledger import Ledger
from hiveloom.research.planning import CostModel, decide_look, look_sizes, power_plan

CHARTER = {
    "goal": "Answer invoice questions exactly.",
    "levers": ["system_prompt", "memory.entries"],
    "budget": {"usd": 10.0},
    "models": {"director": "openrouter/anthropic/claude-sonnet-5"},
}


def _charter(**overrides) -> Charter:
    return Charter.model_validate({**CHARTER, **overrides})


# --------------------------------------------------------------------------- #
# Charter
# --------------------------------------------------------------------------- #
def test_a_minimal_charter_takes_safe_defaults(tmp_path: Path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump(CHARTER))
    charter = load_charter(path)
    assert charter.holdout == 0.3 and charter.eval == "eval.yaml"
    assert charter.guard.success_rate == "no_regression"
    split = charter.budget.split
    assert split.exploration + split.experiments + split.confirmation == pytest.approx(1)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"budget": {"usd": 10, "split": {"exploration": 0.5, "experiments": 0.5,
                                          "confirmation": 0.5}}}, "sum to 1"),
        ({"models": {"director": "claude-sonnet-5"}}, "provider/model"),
        ({"stop": {"goal": {"accuracy": 0.9}}}, "unknown goal"),
        ({"execution": {"tools": {"http_get": "maybe"}}}, "execution.tools"),
        ({"levers": []}, "levers"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_an_invalid_charter_is_refused_with_its_reason(tmp_path: Path, overrides, message):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({**CHARTER, **overrides}))
    with pytest.raises(SpecError, match=message):
        load_charter(path)


def test_program_names_are_slugs():
    assert check_program_name("invoices-2") == "invoices-2"
    for bad in ("", "Invoices", "../x", "a" * 49):
        with pytest.raises(SpecError):
            check_program_name(bad)


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def test_the_ledger_chains_and_detects_tampering(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append("program_started", name="p")
    ledger.append("debit", pool="experiments", usd=0.5)
    ledger.append("verdict", verdict="confirmed")
    assert ledger.verify() == {"ok": True, "checked": 3, "broken_at": None}
    lines = ledger.path.read_text().splitlines()
    event = json.loads(lines[1])
    event["data"]["usd"] = 0.0
    lines[1] = json.dumps(event)
    ledger.path.write_text("\n".join(lines) + "\n")
    assert ledger.verify()["broken_at"] == 1


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
def test_pools_are_computed_from_the_ledger(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    budget = Budget(_charter(), ledger)
    pools = budget.pools()
    assert pools["confirmation"].size == pytest.approx(1.5)
    budget.debit("experiments", 2.0, unit="baseline")
    assert budget.left("experiments") == pytest.approx(4.0)
    assert budget.spent() == pytest.approx(2.0)
    assert not budget.can_afford("experiments", 4.5)


def test_moves_are_bounded_and_never_touch_the_confirmation_reserve(tmp_path: Path):
    budget = Budget(_charter(), Ledger(tmp_path / "ledger.jsonl"))
    assert budget.move("experiments", "exploration", 1.0) is None
    assert budget.left("exploration") == pytest.approx(3.5)
    assert "reserved" in budget.move("confirmation", "exploration", 0.1)
    assert "limited" in budget.move("experiments", "exploration", 2.0)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def test_the_cost_model_and_power_plan_say_what_the_budget_buys():
    cost = CostModel.from_costs([0.01] * 9 + [0.05])
    assert cost.mean_usd == pytest.approx(0.014) and cost.p90_usd == pytest.approx(0.01)
    plan = power_plan(cost, cells=20, success_rate=0.5, experiments_left_usd=1.0)
    assert plan.experiments_affordable == 3
    assert plan.detectable_success_change > 0.4
    assert any("counted failure mechanism" in line for line in plan.lines())


def test_looks_end_at_the_planned_size():
    assert look_sizes(12) == [4, 8, 12]
    assert look_sizes(2) == [1, 2]
    assert look_sizes(1) == [1]


def test_sequential_rules_stop_for_harm_and_futility_but_not_for_benefit():
    # A clear regression stops at any look.
    assert decide_look(success_improved=0, success_worsened=6, target_toward=0,
                       target_against=0, remaining=10) == "harm"
    # No movement and too little left to ever reach significance: futile.
    assert decide_look(success_improved=0, success_worsened=0, target_toward=0,
                       target_against=4, remaining=2) == "futile"
    # A strong early lead is not a declared benefit: keep going to full size.
    assert decide_look(success_improved=6, success_worsened=0, target_toward=6,
                       target_against=0, remaining=6) == "continue"
    assert decide_look(success_improved=6, success_worsened=0, target_toward=6,
                       target_against=0, remaining=0) == "complete"
