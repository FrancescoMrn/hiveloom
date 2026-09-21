"""The executor's write path into durable memory: `propose_memory`.

The tool queues a reviewable proposal and nothing else, so these tests assert
both halves of that — what reaches the queue, and that `harness.yaml` never
moves until a human applies it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveloom import catalog, construct, runner
from hiveloom.errors import ProposalQueueError
from hiveloom.evolve.proposals import (
    apply_proposal_by_id,
    create_memory_proposal,
    list_proposals,
)
from hiveloom.logging.hive import Hive
from hiveloom.logging.journal import read_events
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import MemoryEntry
from hiveloom.tools.builtin import ProposeMemoryTool

_LESSON = {
    "kind": "rule",
    "title": "Dates in ISO 8601",
    "content": "Emit dates as YYYY-MM-DD; the validators reject locale formats.",
    "evidence": "the date_format validator rejected this run twice",
}


def _harness(tmp_path: Path, name: str = "proposer", **params: object) -> Path:
    directory = tmp_path / name
    construct.init_harness(directory, name=name, task="Summarize the input.")
    construct.set_field(directory, "loop.require_verification", "false")
    construct.add_tool(directory, builtin="propose_memory", **params)
    return directory


def _run(harness: Path, *lessons: dict, **kwargs) -> tuple[object, FakeModelProvider]:
    """Run the harness once, calling `propose_memory` with each lesson in turn."""
    responses = [
        tool_response("propose_memory", dict(lesson), call_id=f"call_{index}")
        for index, lesson in enumerate(lessons)
    ]
    provider = FakeModelProvider([*responses, text_response("DONE")])
    result = runner.run_harness(
        harness, "task", provider=provider, literal_input=True, **kwargs
    )
    return result, provider


def _tool_texts(provider: FakeModelProvider) -> list[str]:
    """Every tool result this run handed back to the model."""
    texts: list[str] = []
    for call in provider.calls:
        for block in call["messages"][-1]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                texts.append(block["content"])
    return texts


def _pending(harness: Path) -> list:
    with Hive() as hive:
        return list_proposals(hive, load_spec(harness).identity, status="pending")


# --------------------------------------------------------------------------- #
# The catalog entry
# --------------------------------------------------------------------------- #
def test_propose_memory_is_an_opt_in_catalog_tool_with_a_bounded_cap():
    entry = catalog.BUILTIN_TOOLS["propose_memory"]

    assert {p.name for p in entry.params} == {"max_per_run"}
    assert catalog.validate_builtin_params(entry, {"max_per_run": 2}) == []
    assert catalog.validate_builtin_params(entry, {"max_per_run": "lots"})
    # The spec's own limit is itself capped: every queued row costs a review.
    assert ProposeMemoryTool(max_per_run=500).max_per_run == 10
    assert ProposeMemoryTool(max_per_run=0).max_per_run == 1


def test_a_harness_without_the_tool_cannot_reach_the_queue(tmp_path: Path):
    directory = tmp_path / "plain"
    construct.init_harness(directory, name="plain", task="Summarize.")
    assert "propose_memory" not in [t.builtin for t in load_spec(directory).tools]


def test_the_tool_is_unusable_without_a_run(tmp_path: Path):
    """`run --dry-run` and an SDK caller with no loop get a tool error rather
    than a tool that invents a harness to propose against."""
    with pytest.raises(Exception, match="not available in this context"):
        ProposeMemoryTool().run(**_LESSON, run_context={})


# --------------------------------------------------------------------------- #
# The happy path: queue, review, apply
# --------------------------------------------------------------------------- #
def test_a_proposed_lesson_is_queued_as_a_pending_executor_proposal(tmp_path: Path):
    harness = _harness(tmp_path)
    before = (harness / "harness.yaml").read_text()

    result, provider = _run(harness, _LESSON)

    assert result.status == "success"
    (record,) = _pending(harness)
    assert record.trigger == "executor"
    (change,) = record.proposal.yaml_changes
    assert change.path == "memory.entries.+"
    assert change.value["id"] == "dates-in-iso-8601"
    assert change.value["kind"] == "rule"
    assert change.value["source"] == f"executor:{result.run_id}"
    assert change.value["evidence"] == _LESSON["evidence"]
    assert record.evidence == {
        "run_id": result.run_id,
        "entry_id": "dates-in-iso-8601",
        "kind": "rule",
    }
    # The whole point: nothing was written.
    assert (harness / "harness.yaml").read_text() == before
    assert load_spec(harness).memory.entries == []
    assert f"queued as proposal {record.id}" in _tool_texts(provider)[0]


def test_the_queued_lesson_lands_only_when_a_human_applies_it(tmp_path: Path):
    harness = _harness(tmp_path)
    _run(harness, _LESSON)
    (record,) = _pending(harness)

    with Hive() as hive:
        applied = apply_proposal_by_id(hive, harness, record.id, apply_yaml=True)

    assert applied.changed is True
    (entry,) = load_spec(harness).memory.entries
    assert (entry.id, entry.kind) == ("dates-in-iso-8601", "rule")
    assert entry.content == _LESSON["content"]
    with Hive() as hive:
        assert list_proposals(hive, status="applied")[0].id == record.id


def test_a_second_lesson_appends_after_the_entries_already_stored(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_memory_entry(harness, kind="fact", title="Nulls", content="Null is null.")

    _run(harness, _LESSON)

    (record,) = _pending(harness)
    assert record.proposal.yaml_changes[0].path == "memory.entries.+"
    with Hive() as hive:
        apply_proposal_by_id(hive, harness, record.id, apply_yaml=True)
    assert [e.id for e in load_spec(harness).memory.entries] == [
        "nulls",
        "dates-in-iso-8601",
    ]


def test_both_lessons_from_one_run_can_be_applied(tmp_path: Path):
    """`max_per_run` lets a run offer several lessons, so several must be able
    to land: applying the first moves the spec version, and an append-only
    proposal is allowed to apply against the newer one. Otherwise every lesson
    but the first was permanently unappliable, whatever the cap said."""
    harness = _harness(tmp_path, max_per_run=2)

    _run(
        harness,
        _LESSON,
        {**_LESSON, "title": "Trim whitespace", "content": "Strip trailing whitespace."},
    )

    queued = _pending(harness)
    assert len(queued) == 2
    assert {r.proposal.yaml_changes[0].path for r in queued} == {"memory.entries.+"}
    with Hive() as hive:
        for record in queued:
            assert apply_proposal_by_id(hive, harness, record.id, apply_yaml=True).changed
        assert _pending(harness) == []

    assert sorted(e.id for e in load_spec(harness).memory.entries) == [
        "dates-in-iso-8601",
        "trim-whitespace",
    ]


def test_an_identical_lesson_reuses_the_pending_row(tmp_path: Path):
    """Deduped on the content digest, so a run that rediscovers what it already
    proposed — differently worded evidence and all — does not queue it twice."""
    harness = _harness(tmp_path)

    _, provider = _run(
        harness, _LESSON, {**_LESSON, "title": "Use ISO dates", "evidence": "again"}
    )

    (record,) = _pending(harness)
    assert f"already queued as proposal {record.id}" in _tool_texts(provider)[1]


def test_a_later_run_finds_its_own_lesson_waiting(tmp_path: Path):
    """Dedup spans runs, and the result says so rather than claiming a new row:
    the receipt names the run that filed it."""
    harness = _harness(tmp_path)
    _run(harness, _LESSON)

    _, provider = _run(harness, _LESSON)

    (record,) = _pending(harness)
    assert f"already queued as proposal {record.id}" in _tool_texts(provider)[0]


# --------------------------------------------------------------------------- #
# Bounds and refusals — explanations, not errors
# --------------------------------------------------------------------------- #
def test_the_per_run_cap_stops_queueing_and_says_so(tmp_path: Path):
    harness = _harness(tmp_path, max_per_run=1)

    _, provider = _run(
        harness,
        _LESSON,
        {**_LESSON, "title": "Trim whitespace", "content": "Strip trailing whitespace."},
    )

    assert len(_pending(harness)) == 1
    assert "its limit of 1" in _tool_texts(provider)[1]


def test_memory_turned_off_explains_itself_instead_of_erroring(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(harness, "memory.enabled", "false")

    result, provider = _run(harness, _LESSON)

    assert result.status == "success"
    assert _pending(harness) == []
    assert "memory turned off" in _tool_texts(provider)[0]


def test_a_lesson_the_harness_already_remembers_is_not_requeued(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_memory_entry(
        harness, kind="rule", title="Dates", content=_LESSON["content"]
    )

    _, provider = _run(harness, _LESSON)

    assert _pending(harness) == []
    assert "already remembers that lesson" in _tool_texts(provider)[0]


def test_an_unreachable_queue_explains_itself_instead_of_erroring(tmp_path: Path):
    harness = _harness(tmp_path)
    # A directory where the Hive expects to open a database file.
    blocked = tmp_path / "blocked.db"
    blocked.mkdir()

    result, provider = _run(harness, _LESSON, hive_path=blocked, ingest=False)

    assert result.status == "success"
    assert "review queue is unavailable" in _tool_texts(provider)[0]


def test_an_over_long_lesson_is_a_tool_error_naming_the_limit(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(harness, "memory.max_entry_chars", "80")

    _, provider = _run(harness, {**_LESSON, "content": "x" * 200})

    assert _pending(harness) == []
    assert "200 characters; this harness allows 80" in _tool_texts(provider)[0]


@pytest.mark.parametrize(
    ("lesson", "expected"),
    [
        ({**_LESSON, "kind": "hunch"}, "kind must be"),
        ({**_LESSON, "content": "   "}, "non-empty title and content"),
        ({**_LESSON, "title": "   "}, "non-empty title and content"),
        ({**_LESSON, "title": "!!!"}, "could not derive a memory id"),
    ],
)
def test_a_malformed_lesson_is_a_tool_error(tmp_path: Path, lesson: dict, expected: str):
    harness = _harness(tmp_path)

    _, provider = _run(harness, lesson)

    assert _pending(harness) == []
    assert expected in _tool_texts(provider)[0]


def test_an_over_budget_lesson_is_never_queued_and_writes_nothing(tmp_path: Path):
    """The gate runs before the row is inserted, so a lesson that would break a
    budget cannot become a pending proposal no one can ever apply."""
    harness = _harness(tmp_path)
    construct.set_field(harness, "memory.max_entries", "1")
    construct.add_memory_entry(harness, kind="fact", title="Nulls", content="Null is null.")
    before = (harness / "harness.yaml").read_text()
    spec = load_spec(harness)
    entry = MemoryEntry(id="iso-dates", kind="rule", title="ISO", content="Use ISO dates.")

    with Hive() as hive:
        with pytest.raises(ProposalQueueError, match="refused by the gate"):
            create_memory_proposal(hive, spec, harness, entry, run_id="run_x")
        assert list_proposals(hive, spec.identity) == []
    assert (harness / "harness.yaml").read_text() == before


def test_a_narrower_mutable_set_refuses_the_lesson_in_the_tool(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(harness, "evolution.mutable", '["system_prompt"]')

    _, provider = _run(harness, _LESSON)

    assert _pending(harness) == []
    assert "not in the mutable set" in _tool_texts(provider)[0]


# --------------------------------------------------------------------------- #
# Redaction, journal, and eval batches
# --------------------------------------------------------------------------- #
def test_the_lesson_is_redacted_before_it_reaches_the_queue(tmp_path: Path):
    """A lesson is the one thing a run can write that every later run reads, so
    it goes through `logging.redact` exactly like a journaled or spilled one."""
    harness = _harness(tmp_path)
    construct.set_field(harness, "logging.redact", '{"patterns": ["sk-[a-z0-9]+"]}')

    _run(harness, {**_LESSON, "content": "Authenticate with sk-abc123 before reading."})

    (record,) = _pending(harness)
    content = record.proposal.yaml_changes[0].value["content"]
    assert "sk-abc123" not in content
    assert "Authenticate with" in content


def test_every_proposal_is_journaled_whether_or_not_it_is_queued(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.set_field(harness, "memory.enabled", "false")

    result, _ = _run(harness, _LESSON)

    events = [e for e in read_events(result.trace_path) if e["type"] == "memory_proposed"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["outcome"] == "memory_disabled"
    assert payload["proposal_id"] == ""
    assert payload["content"] == _LESSON["content"]
    assert payload["kind"] == "rule"


def test_an_eval_cell_records_the_lesson_but_does_not_queue_it(tmp_path: Path):
    """One finding restated by 200 cases would be 200 rows for a human to read.
    The marker comes from the eval runner's caller context, not from the model.
    """
    harness = _harness(tmp_path)

    result, provider = _run(harness, _LESSON, context={"eval_run_id": "eval_abc"})

    assert _pending(harness) == []
    assert "part of eval batch eval_abc" in _tool_texts(provider)[0]
    events = [e for e in read_events(result.trace_path) if e["type"] == "memory_proposed"]
    assert events[0]["payload"]["outcome"] == "eval_run"


def test_the_eval_runner_marks_its_cells(tmp_path: Path, monkeypatch):
    """The marker the test above relies on is actually set by `eval_runner`."""
    from types import SimpleNamespace

    from hiveloom import eval_runner

    seen: dict = {}
    monkeypatch.setattr(eval_runner, "case_model_input", lambda case, dataset: "task")
    monkeypatch.setattr(
        eval_runner.runner, "run_harness", lambda *a, **kw: seen.update(kw) or None
    )
    manifest = SimpleNamespace(
        harness_path=str(tmp_path),
        trace_root=str(tmp_path),
        requested_model="m",
        requested_provider="p",
        eval_run_id="eval_abc",
    )
    cell = SimpleNamespace(run_id="eval_abc_0", cell_id="cell_0")

    eval_runner._default_execute(
        manifest=manifest, cell=cell, case=None, spec=SimpleNamespace(dataset=None)
    )

    assert seen["context"] == {"eval_run_id": "eval_abc", "eval_cell_id": "cell_0"}
