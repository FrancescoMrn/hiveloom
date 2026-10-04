"""Questions to the user, and the labels (anchors) their answers become.

The user's attention is the scarcest thing a program spends, so questions are
typed, batched, and capped by the charter (``human.questions`` over the
program, ``human.batch`` open at once):

* ``label`` — does this output meet this criterion? (pass / fail)
* ``audit`` — the same, sampled from the current incumbent each round, so a
  judge that stops agreeing with the user loses its trust mid-program;
* ``disambiguate`` / ``confirm`` — a question about a criterion's meaning or
  a boundary, answered in words or by picking an option.

A ``label`` or ``audit`` answer becomes an **anchor**: a human-labelled output
that judges are measured against. Every other answer is recorded for the
director's next brief. Questions never block the program except the contract
approval, which is not a question but a gate.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

QuestionKind = Literal["label", "audit", "disambiguate", "confirm"]
LABEL_KINDS = ("label", "audit")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: QuestionKind
    text: str
    criterion: str | None = None
    options: list[str] = Field(default_factory=list)
    run_id: str | None = None
    case_id: str | None = None
    request: str | None = None
    output: str | None = None
    reference: Any = None
    judges: dict[str, str | None] = Field(default_factory=dict)
    asked_by: Literal["engine", "director"] = "engine"
    status: Literal["open", "answered", "withdrawn"] = "open"
    answer: str | None = None
    asked_at: str
    answered_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class QuestionBox:
    """Questions and anchors of one program, as two JSONL files."""

    def __init__(self, root: Path, *, total: int, batch: int):
        self.root = root
        self.total = total
        self.batch = batch
        self.path = root / "questions.jsonl"
        self.anchors_path = root / "anchors.jsonl"

    # -- questions ------------------------------------------------------------ #
    def all(self) -> list[Question]:
        if not self.path.exists():
            return []
        latest: dict[str, Question] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                question = Question.model_validate_json(line)
                latest[question.id] = question
        return list(latest.values())

    def open(self) -> list[Question]:
        return [q for q in self.all() if q.status == "open"]

    def _append(self, question: Question) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(question.model_dump_json() + "\n")

    def room(self) -> int:
        """How many more questions may be asked right now."""
        asked = [q for q in self.all() if q.status != "withdrawn"]
        return max(0, min(self.total - len(asked), self.batch - len(self.open())))

    def ask(self, kind: QuestionKind, text: str, **fields: Any) -> Question:
        if self.room() <= 0:
            raise ValueError(
                f"no room for another question ({len(self.open())} open of {self.batch}; "
                f"{self.total} over the program)"
            )
        for existing in self.all():
            same_output = (kind in LABEL_KINDS and existing.kind in LABEL_KINDS
                           and existing.criterion == fields.get("criterion")
                           and existing.output == fields.get("output")
                           and existing.request == fields.get("request"))
            # Label questions share their wording; they differ by the output asked about.
            same_text = (kind not in LABEL_KINDS
                         and existing.text.strip().lower() == text.strip().lower())
            if existing.status != "withdrawn" and (same_output or same_text):
                raise ValueError(f"this was already asked as {existing.id}")
        question = Question(id=f"q_{uuid.uuid4().hex[:8]}", kind=kind, text=text.strip()[:1000],
                            asked_at=_now(), **fields)
        self._append(question)
        return question

    def answer(self, question_id: str, answer: str) -> Question:
        question = next((q for q in self.all() if q.id == question_id), None)
        if question is None:
            raise KeyError(f"no question {question_id}")
        if question.status != "open":
            raise ValueError(f"question {question_id} is {question.status}")
        value = answer.strip()
        if question.kind in LABEL_KINDS:
            value = value.lower()
            if value not in ("pass", "fail"):
                raise ValueError("a label is 'pass' or 'fail'")
        elif question.options and value not in question.options and not value:
            raise ValueError("pick one of the options or answer in words")
        answered = question.model_copy(update={"status": "answered", "answer": value,
                                               "answered_at": _now()})
        self._append(answered)
        if answered.kind in LABEL_KINDS and answered.criterion:
            self.add_anchor(criterion=answered.criterion, request=answered.request or "",
                            output=answered.output or "", label=value,
                            source=answered.kind, reference=answered.reference,
                            question=answered.id, run_id=answered.run_id)
        return answered

    def withdraw_open(self) -> int:
        """Close what nobody needs answered any more (the program ended)."""
        withdrawn = 0
        for question in self.open():
            self._append(question.model_copy(update={"status": "withdrawn"}))
            withdrawn += 1
        return withdrawn

    # -- anchors -------------------------------------------------------------- #
    def anchors(self) -> list[dict[str, Any]]:
        if not self.anchors_path.exists():
            return []
        return [json.loads(line) for line in
                self.anchors_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def add_anchor(self, *, criterion: str, request: str, output: str, label: str,
                   source: str, **extra: Any) -> None:
        row = {"criterion": criterion, "input": request, "output": output, "label": label,
               "source": source, "at": _now(), **extra}
        self.anchors_path.parent.mkdir(parents=True, exist_ok=True)
        with self.anchors_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
