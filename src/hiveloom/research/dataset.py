"""The program's own cases, kept in two sets that never mix.

* **Working cases** come from the user's seeds and the director. The director
  sees them, and the program improves against them.
* **Sealed cases** are written by the examiner from the concepts and the
  approved contract only, stored where no director context reads them, and
  read once, at the end, to confirm.

A working case that nearly copies a sealed one is dropped, so the working split
cannot converge on the test by accident. The similarity is the same
deterministic tf-idf that memory selection uses; the director is told only how
many cases were dropped, never which.

Both sets are written into one ``cases.jsonl`` that the program's eval loads.
The working/sealed split is carried by case id, so the engine's split, pairing
and one-time confirmation work exactly as for a given eval.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hiveloom.context.memory_select import _terms

Provenance = Literal["seed", "director", "examiner"]
#: Cosine similarity at or above which a working case copies a sealed one.
NEAR_DUPLICATE = 0.8


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    input: str = Field(min_length=1, max_length=8000)
    expected: dict[str, Any] = Field(default_factory=dict,
                                     description="criterion id -> the value its check expects")
    criterion: str | None = Field(default=None, description="The criterion it was written for.")
    provenance: Provenance
    derived_from: str | None = None
    notes: str = ""


def _vectors(texts: list[str]) -> list[dict[str, float]]:
    documents = [Counter(_terms(text)) for text in texts]
    frequency = Counter(term for document in documents for term in document)
    count = len(documents)
    vectors = []
    for document in documents:
        vector = {term: tf * (math.log((1 + count) / (1 + frequency[term])) + 1)
                  for term, tf in document.items()}
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        vectors.append({term: value / norm for term, value in vector.items()})
    return vectors


def similarity(a: str, b: str) -> float:
    left, right = _vectors([a, b])
    return sum(value * right.get(term, 0.0) for term, value in left.items())


def drop_near_duplicates(working: list[Case], sealed: list[Case]) -> tuple[list[Case], int]:
    """Working cases minus those that nearly copy a sealed case; and how many went."""
    if not sealed:
        return working, 0
    vectors = _vectors([case.input for case in [*working, *sealed]])
    working_vectors, sealed_vectors = vectors[: len(working)], vectors[len(working):]
    kept = []
    for case, vector in zip(working, working_vectors, strict=True):
        closest = max(
            sum(value * other.get(term, 0.0) for term, value in vector.items())
            for other in sealed_vectors
        )
        if closest < NEAR_DUPLICATE:
            kept.append(case)
    return kept, len(working) - len(kept)


def normalize_input(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


class CaseStore:
    """Working and sealed cases of one program, each in its own file."""

    def __init__(self, root: Path):
        self.root = root
        self.working_path = root / "dataset" / "working.jsonl"
        self.sealed_path = root / "sealed" / "cases.jsonl"

    @staticmethod
    def _read(path: Path) -> list[Case]:
        if not path.exists():
            return []
        return [Case.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _write(path: Path, cases: list[Case]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(case.model_dump_json() + "\n" for case in cases),
                        encoding="utf-8")

    def working(self) -> list[Case]:
        return self._read(self.working_path)

    def sealed(self) -> list[Case]:
        return self._read(self.sealed_path)

    def add_working(self, cases: list[Case], *, limit: int,
                    prefix: str | None = None) -> tuple[list[Case], list[str]]:
        """Append new working cases; returns those added and the reasons others were not.

        With ``prefix``, ids are assigned here, after duplicates are dropped and
        numbered past the highest existing one, so no two cases share an id.
        """
        current = self.working()
        seen = {normalize_input(case.input) for case in current}
        taken = {case.id for case in current}
        number = max((int(m.group(1)) for case in current
                      if prefix and (m := re.fullmatch(rf"{re.escape(prefix)}-(\d+)", case.id))),
                     default=0)
        added, refused = [], []
        for case in cases:
            key = normalize_input(case.input)
            if key in seen:
                refused.append(f"duplicate of an existing case: {case.input[:60]!r}")
                continue
            if len(current) + len(added) >= limit:
                refused.append(f"the dataset is full ({limit} cases)")
                break
            if prefix:
                number += 1
                case = case.model_copy(update={"id": f"{prefix}-{number}"})
            if case.id in taken:
                refused.append(f"duplicate case id {case.id}")
                continue
            seen.add(key)
            taken.add(case.id)
            added.append(case)
        self._write(self.working_path, [*current, *added])
        return added, refused

    def set_working(self, cases: list[Case]) -> None:
        self._write(self.working_path, cases)

    def set_sealed(self, cases: list[Case]) -> None:
        self._write(self.sealed_path, cases)

    def write_eval_cases(self, path: Path) -> None:
        """Both sets as one eval dataset (``{id, input, expected}`` rows)."""
        rows = [
            {"id": case.id, "input": case.input,
             "expected": case.expected, "metadata": {"provenance": case.provenance,
                                                      "criterion": case.criterion}}
            for case in [*self.working(), *self.sealed()]
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
