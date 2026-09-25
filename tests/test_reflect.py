"""Post-run reflection: lessons drafted into the review queue, guarded and cooled down."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import construct, runner
from hiveloom.evolve.assess import attempt_history
from hiveloom.evolve.evolver import MutationProposal, gate
from hiveloom.evolve.reflect import maybe_reflect
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.spec.loader import load_spec


# --------------------------------------------------------------------------- #
# Reflection
# --------------------------------------------------------------------------- #
def _failing_run(harness_dir: Path, *, reflect: bool = True, on: str = "failure", **kw):
    if reflect:
        construct.set_field(harness_dir, "evolution.reflect.enabled", "true")
        construct.set_field(harness_dir, "evolution.reflect.on", on)
    construct.add_validator(harness_dir, "regex_match", pattern=r"^\{")
    return runner.run_harness(
        harness_dir, "extract the invoice", provider=FakeModelProvider(
            [text_response("not json"), text_response("still not json"),
             text_response("nope"), text_response("no")]
        ), **kw,
    )


LESSON = json.dumps(
    {"lessons": [{"kind": "rule", "title": "Answer with one JSON object",
                  "content": "Return a single JSON object and nothing else.",
                  "evidence": "the json_schema validator rejected prose"}]}
)


def test_a_failed_run_is_reflected_into_a_queued_lesson(harness_dir: Path):
    model = FakeStrongModel([LESSON])
    result = _failing_run(harness_dir, strong_model=model)
    assert result.status == "verify_failed"
    spec = load_spec(harness_dir)
    with Hive() as hive:
        rows = hive.list_proposals(spec.identity)
    [row] = [r for r in rows if r["status"] == "pending"]
    assert row["trigger"] == "reflect"
    change = json.loads(row["proposal_json"])["yaml_changes"][0]
    assert change["path"] == "memory.entries.+"
    assert change["value"]["source"].startswith("reflect:")
    # The prompt showed the run, as untrusted data, and the lessons already held.
    assert "<run>" in model.prompts[0]["user"] and "json" in model.prompts[0]["user"].lower()
    # Queued, never applied.
    assert not load_spec(harness_dir).memory.entries


def test_reflection_is_skipped_for_eval_runs_success_and_when_off(harness_dir: Path):
    spec = load_spec(harness_dir)
    model = FakeStrongModel([])
    assert maybe_reflect(spec, harness_dir, "r1", "verify_failed", None,
                         strong_model=model) == []  # off by default
    construct.set_field(harness_dir, "evolution.reflect.enabled", "true")
    spec = load_spec(harness_dir)
    assert maybe_reflect(spec, harness_dir, "r1", "success", None, strong_model=model) == []
    assert maybe_reflect(spec, harness_dir, "r1", "verify_failed", None,
                         context={"eval_run_id": "e1"}, strong_model=model) == []
    assert model.prompts == []


def test_an_empty_reflection_still_starts_the_cooldown(harness_dir: Path):
    model = FakeStrongModel([json.dumps({"lessons": []})])
    _failing_run(harness_dir, strong_model=model)
    spec = load_spec(harness_dir)
    with Hive() as hive:
        [marker] = hive.list_proposals(spec.identity)
        history = attempt_history(hive, spec.identity)
    assert marker["status"] == "rejected" and marker["trigger"] == "reflect"
    # A marker tried no mutation, so it is not search memory.
    assert history == []
    # Inside the cooldown a second failing run pays for no model call.
    second = FakeStrongModel([])
    maybe_reflect(spec, harness_dir, "r2", "verify_failed", None, strong_model=second)
    assert second.prompts == []


def test_reflection_is_frozen_from_evolution(harness_dir: Path):
    spec = load_spec(harness_dir)
    result = gate(
        spec, MutationProposal(yaml_changes=[{"path": "evolution.reflect.enabled", "value": True}])
    )
    assert not result.accepted and result.rejected
