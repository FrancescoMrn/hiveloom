"""The research charter: what a program may do, owned by the user.

A research program runs many versions of one harness, so its charter lives in
its own file (``research.yaml`` in the program folder), not in any version's
``harness.yaml``. Nothing in it is reachable from evolution or from the
director: the director reads it through its brief and cannot write files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hiveloom.errors import SpecError

ToolMode = Literal["allow", "sandbox", "replay", "deny"]
PROGRAM_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class BudgetSplit(BaseModel):
    """How the budget divides between the program's kinds of spend."""

    model_config = ConfigDict(extra="forbid")

    exploration: float = Field(default=0.25, ge=0, le=1, description="Director calls.")
    experiments: float = Field(
        default=0.60, ge=0, le=1, description="Baseline and candidate evals on the working split."
    )
    confirmation: float = Field(
        default=0.15, ge=0, le=1,
        description="The one sealed confirmation; reserved when the program starts.",
    )
    data: float = Field(
        default=0.0, ge=0, le=1,
        description="Building the evaluation from concepts: the examiner and judge "
        "calibration. Zero for a program with a given eval.",
    )

    @model_validator(mode="after")
    def _sums_to_one(self) -> BudgetSplit:
        total = self.exploration + self.experiments + self.confirmation + self.data
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"budget.split must sum to 1 (got {total:.3f})")
        return self


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usd: float = Field(gt=0, le=1000, description="Everything the program may spend.")
    wall_clock_minutes: int = Field(default=240, ge=1, le=10_080)
    rounds: int = Field(default=6, ge=1, le=50)
    split: BudgetSplit = Field(default_factory=BudgetSplit)


class Guard(BaseModel):
    """Constraints no kept change may break, whatever it improves."""

    model_config = ConfigDict(extra="forbid")

    success_rate: Literal["no_regression"] | None = "no_regression"
    cost_per_run_increase: float | None = Field(
        default=None, ge=0,
        description="Largest allowed relative increase in mean cost per run (0.2 = +20%).",
    )


class StopRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: dict[str, float] = Field(
        default_factory=dict,
        description="Done when every threshold holds on the working split: "
        "'success_rate' or 'metric:<name>' (mean).",
    )
    no_progress_rounds: int = Field(default=3, ge=1, le=20)
    ceiling_content_share: float = Field(
        default=0.7, gt=0, le=1,
        description="Stop when this share of failures is content errors and no "
        "addressable signal remains.",
    )

    @field_validator("goal")
    @classmethod
    def _goal_keys(cls, value: dict[str, float]) -> dict[str, float]:
        for key in value:
            if key != "success_rate" and not key.startswith("metric:"):
                raise ValueError(f"unknown goal '{key}' (use success_rate or metric:<name>)")
        return value


class Execution(BaseModel):
    """How research runs may use the harness's tools (see research.execution)."""

    model_config = ConfigDict(extra="forbid")

    tools: dict[str, ToolMode] = Field(default_factory=dict)
    mcp: dict[str, ToolMode | dict[str, ToolMode]] = Field(default_factory=dict)
    max_cost_per_run: float | None = Field(default=None, gt=0)


class Models(BaseModel):
    model_config = ConfigDict(extra="forbid")

    director: str = Field(description="provider/model-id of the director.")
    examiner: str | None = Field(
        default=None,
        description="provider/model-id that writes the sealed cases from concepts; must "
        "differ from the director (concepts mode).",
    )
    judges: list[str] = Field(
        default_factory=list, max_length=5,
        description="provider/model-ids that score judged criteria; two or more make an "
        "ensemble whose split verdicts count as unmeasured.",
    )

    @field_validator("director", "examiner")
    @classmethod
    def _selector(cls, value: str | None) -> str | None:
        if value is not None and "/" not in value:
            raise ValueError("models must be provider/model-id")
        return value

    @field_validator("judges")
    @classmethod
    def _judges(cls, value: list[str]) -> list[str]:
        for model in value:
            if "/" not in model:
                raise ValueError("models.judges must be provider/model-ids")
        return value


class Human(BaseModel):
    """How much of the user's attention a program may ask for."""

    model_config = ConfigDict(extra="forbid")

    questions: int = Field(default=12, ge=0, le=200, description="Questions over the program.")
    batch: int = Field(default=4, ge=1, le=20, description="Open questions at once.")


class Trust(BaseModel):
    """When a judge may score a criterion: agreement with the user's labels."""

    model_config = ConfigDict(extra="forbid")

    kappa: float = Field(default=0.6, ge=0, le=1)
    agreement: float = Field(default=0.85, ge=0, le=1)
    min_anchors: int = Field(default=8, ge=1, le=100)


class DatasetPlan(BaseModel):
    """How big the program's own evaluation grows (concepts mode)."""

    model_config = ConfigDict(extra="forbid")

    cases_per_criterion: int = Field(default=6, ge=1, le=50)
    sealed_per_criterion: int = Field(default=4, ge=1, le=50)
    max_cases: int = Field(default=80, ge=2, le=500)
    sample_cases: int = Field(default=6, ge=1, le=30,
                              description="Cases shown with the contract for approval.")


class Charter(BaseModel):
    """``research.yaml``."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, description="What the program is for, in prose.")
    eval: str | None = Field(
        default=None,
        description="Eval document inside the harness folder, running the harness "
        "('harness: .'). Defaults to eval.yaml unless the program builds its own "
        "evaluation from `concepts`.",
    )
    concepts: str | None = Field(
        default=None,
        description="What the user cares about, in prose, or a file in the harness folder "
        "holding it. The program drafts an evaluation contract and cases from it.",
    )
    seeds: str | None = Field(
        default=None,
        description="Optional JSONL file in the harness folder of example requests "
        '({"input": ..., "expected": {...}, "notes": ...}).',
    )
    holdout: float = Field(default=0.3, ge=0.0, le=0.8)
    levers: list[str] = Field(min_length=1)
    guard: Guard = Field(default_factory=Guard)
    budget: Budget
    stop: StopRules = Field(default_factory=StopRules)
    execution: Execution = Field(default_factory=Execution)
    models: Models
    experiments_per_round: int = Field(default=2, ge=1, le=5)
    human: Human = Field(default_factory=Human)
    trust: Trust = Field(default_factory=Trust)
    dataset: DatasetPlan = Field(default_factory=DatasetPlan)

    @model_validator(mode="after")
    def _mode(self) -> Charter:
        if self.concepts is None:
            if self.eval is None:
                self.eval = "eval.yaml"
            return self
        if self.eval is not None:
            raise ValueError("a charter takes either an eval or concepts, not both")
        if not self.models.examiner:
            raise ValueError("concepts mode needs models.examiner to write the sealed cases")
        if self.models.examiner == self.models.director:
            raise ValueError("models.examiner must differ from models.director")
        if self.budget.split.data == 0:
            raise ValueError("concepts mode needs a budget.split.data share for the examiner "
                             "and judges (e.g. data 0.2, exploration 0.25, experiments 0.4, "
                             "confirmation 0.15)")
        return self

    @property
    def concepts_mode(self) -> bool:
        return self.concepts is not None


def load_charter(path: str | Path) -> Charter:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot read research charter {path}: {exc}") from exc
    try:
        return Charter.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise SpecError(f"invalid research charter {path}: {problems}") from exc


def check_program_name(name: str) -> str:
    if not PROGRAM_NAME.match(name):
        raise SpecError(f"invalid program name {name!r} (a-z, 0-9, dashes; up to 48)")
    return name
