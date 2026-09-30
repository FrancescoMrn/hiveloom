"""The evaluation contract: what "better" means, in criteria the user approved.

A program that builds its own evaluation from concepts measures against a
contract, never against the director's say-so. Each criterion states one thing
the user cares about and how it is checked:

* **deterministic** checks (``contains``, ``not_contains``, ``exact``,
  ``regex``, ``json_field``, ``json_present``) read the output and, where a
  check needs one, the value the case expects for that criterion — no model,
  no trust required;
* **judge** checks score the output against a rubric with a model ensemble,
  and count only while the judges agree with the user's own labels (see
  :mod:`hiveloom.research.judges`).

The director drafts a contract; the user approves it (with sample cases) before
any money is spent on changes. An approved contract is frozen as
``contract/v<n>.yaml``; a changed one is a new version.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CheckKind = Literal["judge", "contains", "not_contains", "exact", "regex", "json_field",
                    "json_present"]
#: Checks that need the case's expected value for this criterion.
NEEDS_EXPECTED = {"contains", "not_contains", "exact", "json_field"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CheckKind
    field: str | None = Field(default=None, description="JSON field (json_field, json_present).")
    pattern: str | None = Field(default=None, description="Regular expression (regex).")
    rubric: str | None = Field(default=None, description="What a judge looks for (judge).")

    @model_validator(mode="after")
    def _complete(self) -> Check:
        if self.kind in ("json_field", "json_present"):
            if not self.field:
                raise ValueError(f"a {self.kind} check needs a field")
            if not re.fullmatch(r"[A-Za-z_][\w.-]*", self.field):
                raise ValueError(f"field {self.field!r} must name one JSON field; write one "
                                 "criterion per field")
        if self.kind == "regex":
            if not self.pattern:
                raise ValueError("a regex check needs a pattern")
            re.compile(self.pattern)
        if self.kind == "judge" and not (self.rubric or "").strip():
            raise ValueError("a judge check needs a rubric")
        return self

    @property
    def deterministic(self) -> bool:
        return self.kind != "judge"


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    says: str = Field(min_length=3, max_length=400)
    check: Check
    weight: float = Field(default=1.0, gt=0, le=10)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"criterion id {value!r} must be a-z, 0-9 and dashes")
        return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[Criterion] = Field(min_length=1, max_length=12)
    goal_thresholds: dict[str, float] = Field(
        default_factory=dict, description="criterion id -> pass rate that counts as done."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Contract:
        ids = [criterion.id for criterion in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion ids must be unique")
        for key, value in self.goal_thresholds.items():
            if key not in ids:
                raise ValueError(f"goal threshold for unknown criterion {key!r}")
            if not 0 <= value <= 1:
                raise ValueError(f"goal threshold for {key!r} must be between 0 and 1")
        return self

    def get(self, criterion_id: str) -> Criterion:
        for criterion in self.criteria:
            if criterion.id == criterion_id:
                return criterion
        raise KeyError(criterion_id)

    def judged(self) -> list[Criterion]:
        return [c for c in self.criteria if not c.check.deterministic]


# --------------------------------------------------------------------------- #
# Checking one output
# --------------------------------------------------------------------------- #
def applies(criterion: Criterion, expected: dict[str, Any]) -> bool:
    """A criterion that needs an expected value applies only to cases that give one."""
    return criterion.check.kind not in NEEDS_EXPECTED or criterion.id in expected


def _json(output: str) -> Any:
    text = output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    return json.loads(text)


def _equal(actual: Any, wanted: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(wanted, (int, float)):
        return abs(float(actual) - float(wanted)) <= 1e-6 + 1e-9 * abs(float(wanted))
    if isinstance(actual, str) and isinstance(wanted, str):
        return actual.strip().lower() == wanted.strip().lower()
    return actual == wanted


def check_deterministic(criterion: Criterion, output: str, expected: dict[str, Any]) -> bool:
    """Pass or fail for one output; only for deterministic checks that apply."""
    check, wanted = criterion.check, expected.get(criterion.id)
    text = output or ""
    if check.kind == "contains":
        return str(wanted).lower() in text.lower()
    if check.kind == "not_contains":
        return str(wanted).lower() not in text.lower()
    if check.kind == "exact":
        return text.strip() == str(wanted).strip()
    if check.kind == "regex":
        return re.search(check.pattern or "", text) is not None
    try:
        data = _json(text)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if check.kind == "json_present":
        return data.get(check.field) not in (None, "", [], {})
    if check.kind == "json_field":
        return check.field in data and _equal(data[check.field], wanted)
    raise ValueError(f"{check.kind} is not a deterministic check")


# --------------------------------------------------------------------------- #
# Versions on disk
# --------------------------------------------------------------------------- #
def contract_path(root: Path, version: int) -> Path:
    return root / "contract" / f"v{version}.yaml"


def save_contract(root: Path, version: int, contract: Contract) -> Path:
    path = contract_path(root, version)
    if path.exists():
        raise ValueError(f"contract v{version} already exists; a change is a new version")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False),
                    encoding="utf-8")
    return path


def load_contract(root: Path, version: int) -> Contract:
    return Contract.model_validate(
        yaml.safe_load(contract_path(root, version).read_text(encoding="utf-8")))
