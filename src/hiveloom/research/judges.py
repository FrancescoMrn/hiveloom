"""Judges score what code cannot, and count only while they agree with the user.

A judged criterion is scored by an ensemble of models against its rubric. The
ensemble's verdict on an output is unanimous or it is nothing: a split is
recorded, never averaged, and the output counts as unmeasured for that
criterion.

A judge is **trusted** for a criterion only once the user's own labels (the
*anchors*) back it: at least ``trust.min_anchors`` labelled outputs, Cohen's κ
and raw agreement at or above the charter's floors. Until then the criterion is
*unmeasured* — it cannot pass or fail a run, confirm or refute anything — and
the program spends questions on labels instead. Trust is recomputed whenever a
label arrives, so audit labels on the incumbent's outputs can take it away
mid-program: the tripwire for a director that learned to please the judge
rather than the user.

Verdicts are cached by content (criterion, rubric, input, output, model), so an
output is judged once however often it is read.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .contract import Criterion

Label = Literal["pass", "fail"]

JUDGE_SYSTEM = """\
You are a strict evaluator. You judge ONE criterion of ONE answer, and nothing
else: not style, not other criteria, not whether you would have answered
differently. Read the request, the answer, and the criterion's rubric. Decide
whether the answer meets the rubric.

Reply with a single JSON object and nothing else:
{"verdict": "pass" | "fail", "reason": "<one short sentence>"}
"""


def _key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    """Agreement beyond chance between two raters' binary labels."""
    if not pairs:
        return 0.0
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left = sum(a == "pass" for a, _ in pairs) / n
    right = sum(b == "pass" for _, b in pairs) / n
    expected = left * right + (1 - left) * (1 - right)
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def parse_verdict(text: str) -> Label | None:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict in ("pass", "fail"):
        return verdict  # type: ignore[return-value]
    lowered = (text or "").strip().lower()
    if lowered in ("pass", "fail"):
        return lowered  # type: ignore[return-value]
    return None


@dataclass
class TrustState:
    criterion: str
    anchors: int
    compared: int
    agreement: float | None
    kappa: float | None
    trusted: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion, "anchors": self.anchors, "compared": self.compared,
            "agreement": None if self.agreement is None else round(self.agreement, 3),
            "kappa": None if self.kappa is None else round(self.kappa, 3),
            "trusted": self.trusted, "reason": self.reason,
        }


class JudgePanel:
    """The judges of one program, with their verdict cache and spend."""

    def __init__(self, root: Path, models: list[str], *,
                 strong_models: dict[str, Any] | None = None, base: Path | None = None):
        self.root = root
        self.models = list(models)
        self._strong = dict(strong_models or {})
        self._base = base
        self._lock = threading.Lock()
        self._cache_path = root / "judgments.jsonl"
        self._cache: dict[str, dict[str, Any]] | None = None

    # -- models -------------------------------------------------------------- #
    def _model(self, selector: str):
        if selector not in self._strong:
            from hiveloom.generate.llm import build_strong_model

            self._strong[selector] = build_strong_model(selector, self._base)
        return self._strong[selector]

    @property
    def spent_usd(self) -> float:
        return sum(float(getattr(model, "spent_usd", 0.0) or 0.0)
                   for model in self._strong.values())

    # -- cache --------------------------------------------------------------- #
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._cache is None:
            self._cache = {}
            if self._cache_path.exists():
                for line in self._cache_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        self._cache[row["key"]] = row
        return self._cache

    def _remember(self, row: dict[str, Any]) -> None:
        self._load()[row["key"]] = row
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- judging ------------------------------------------------------------- #
    def votes(self, criterion: Criterion, request: str, output: str,
              reference: Any = None) -> dict[str, Label | None]:
        """Each judge's verdict on one output for one criterion (cached)."""
        rubric = criterion.check.rubric or criterion.says
        votes: dict[str, Label | None] = {}
        for selector in self.models:
            key = _key(criterion.id, rubric, request, output, selector,
                       json.dumps(reference, sort_keys=True, default=str))
            with self._lock:
                cached = self._load().get(key)
            if cached is not None:
                votes[selector] = cached["verdict"]
                continue
            user = (
                f"<criterion>\n{criterion.says}\n</criterion>\n"
                f"<rubric>\n{rubric}\n</rubric>\n"
                + (f"<reference>\n{json.dumps(reference, default=str)}\n</reference>\n"
                   if reference not in (None, "", {}) else "")
                + f"<request>\n{request[:4000]}\n</request>\n"
                f"<answer>\n{output[:6000]}\n</answer>"
            )
            try:
                text = self._model(selector).generate(system=JUDGE_SYSTEM, user=user,
                                                      max_tokens=2000)
                verdict = parse_verdict(text)
            except Exception:  # noqa: BLE001 - a failed call is an abstention, not cached
                votes[selector] = None
                continue
            votes[selector] = verdict
            if verdict is None:
                continue  # an unreadable reply is asked again next time, not remembered
            with self._lock:
                self._remember({"key": key, "criterion": criterion.id, "model": selector,
                                "verdict": verdict, "raw": text[:400],
                                "at": datetime.now(UTC).isoformat()})
        return votes

    @staticmethod
    def verdict(votes: dict[str, Label | None]) -> Label | None:
        """Unanimous among all the judges, or nothing: an abstention is not a vote."""
        if not votes or any(vote is None for vote in votes.values()):
            return None
        answered = set(votes.values())
        return answered.pop() if len(answered) == 1 else None

    # -- trust --------------------------------------------------------------- #
    def trust(self, criterion: Criterion, anchors: list[dict[str, Any]], *, kappa: float,
              agreement: float, min_anchors: int) -> TrustState:
        mine = [anchor for anchor in anchors if anchor["criterion"] == criterion.id]
        pairs: list[tuple[str, str]] = []
        for anchor in mine:
            judged = self.verdict(self.votes(criterion, anchor["input"], anchor["output"],
                                             anchor.get("reference")))
            if judged is not None:
                pairs.append((judged, anchor["label"]))
        if len(mine) < min_anchors:
            return TrustState(criterion.id, len(mine), len(pairs), None, None, False,
                              f"{len(mine)} of {min_anchors} labels needed")
        if not pairs:
            return TrustState(criterion.id, len(mine), 0, None, None, False,
                              "the judges agreed on none of the labelled outputs")
        agree = sum(a == b for a, b in pairs) / len(pairs)
        k = cohen_kappa(pairs)
        trusted = len(pairs) >= min_anchors and agree >= agreement and k >= kappa
        reason = ("trusted" if trusted else
                  f"agreement {agree:.2f} (≥{agreement}) and κ {k:.2f} (≥{kappa}) "
                  f"on {len(pairs)} of {min_anchors} needed")
        return TrustState(criterion.id, len(mine), len(pairs), agree, k, trusted, reason)
