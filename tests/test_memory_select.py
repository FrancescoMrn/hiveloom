"""Relevance-selected durable memory: ranking, selection, search_memory, journaling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hiveloom import construct, runner
from hiveloom.context.manager import ContextManager
from hiveloom.context.memory_select import (
    SEARCH_MEMORY_TOOL,
    SearchMemoryTool,
    rank_entries,
    select_entries,
)
from hiveloom.evolve.evolver import MutationProposal, gate
from hiveloom.evolve.signal import locate_signal
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import HarnessSpec
from hiveloom.tools.registry import build_registry

ENTRIES = [
    {"id": "iso-dates", "kind": "rule", "title": "Dates in ISO 8601",
     "content": "Emit invoice dates as YYYY-MM-DD."},
    {"id": "cite-sources", "kind": "rule", "title": "Cite every source",
     "content": "Every claim about a vendor needs its source URL.", "pinned": True},
    {"id": "currency", "kind": "fact", "title": "Invoice currency",
     "content": "Invoices from the EU branch are in EUR, never USD."},
    {"id": "tone", "kind": "example", "title": "Report tone",
     "content": "Write the incident report in plain past tense."},
]


def _spec(selection: str = "relevant", max_selected: int = 3, **memory) -> HarnessSpec:
    return HarnessSpec.model_validate(
        {
            "name": "t", "description": "d", "system_prompt": "sp",
            "memory": {"selection": selection, "max_selected": max_selected,
                       "entries": ENTRIES, **memory},
        }
    )


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_ranking_matches_task_words_and_weights_titles():
    ranked = rank_entries(_spec().memory.entries, "Check the invoice dates for the EU branch")
    assert [entry.id for entry, _ in ranked][:2] == ["iso-dates", "currency"]
    # Common words say nothing about which lesson applies.
    assert rank_entries(_spec().memory.entries, "the and for with") == []


def test_selection_keeps_pinned_then_best_matches_in_declaration_order():
    selected, scores = select_entries(_spec().memory, "invoice dates in EUR")
    assert [entry.id for entry in selected] == ["iso-dates", "cite-sources", "currency"]
    assert set(scores) == {"iso-dates", "currency"}
    # A task unlike anything learned gets the pinned rule only.
    selected, _ = select_entries(_spec().memory, "summarize the weather")
    assert [entry.id for entry in selected] == ["cite-sources"]
    # max_selected bounds the section, pinned included.
    selected, _ = select_entries(_spec(max_selected=2).memory, "invoice dates EUR report")
    # The pinned rule takes one slot; the single best match takes the other.
    assert [entry.id for entry in selected] == ["iso-dates", "cite-sources"]


def test_the_rendered_selection_says_what_else_is_stored():
    memory = _spec().memory
    selected, _ = select_entries(memory, "invoice dates")
    text = memory.render(selected)
    assert "Dates in ISO 8601" in text and "Report tone" not in text
    assert "more lesson(s) are stored" in text and "search_memory" in text
    assert "more lesson(s)" not in memory.render()


def test_more_pinned_entries_than_the_selection_allows_is_invalid():
    with pytest.raises(ValidationError, match="pinned"):
        _spec(max_selected=1, entries=[{**e, "pinned": True} for e in ENTRIES[:2]])


def test_selection_settings_are_frozen_from_evolution():
    spec = _spec()
    for path, value in (("memory.selection", "all"), ("memory.max_selected", 24)):
        result = gate(spec, MutationProposal(yaml_changes=[{"path": path, "value": value}]))
        assert not result.accepted and result.rejected, path


def test_search_memory_is_registered_only_for_relevant_selection(tmp_path):
    assert SEARCH_MEMORY_TOOL in build_registry(_spec(), tmp_path).names()
    assert SEARCH_MEMORY_TOOL not in build_registry(_spec(selection="all"), tmp_path).names()
    tool = SearchMemoryTool(_spec().memory)
    found = tool.run(query="incident report tone").content
    assert "Report tone" in found
    assert "no stored lesson" in tool.run(query="kubernetes").content


def test_context_manager_narrows_the_section_once_per_run():
    manager = ContextManager(_spec(), FakeModelProvider([]))
    assert "Report tone" in manager.system()  # before selection: the whole store
    ids, _ = manager.select_memory("date formats")
    assert ids == ["iso-dates", "cite-sources"]
    assert "Report tone" not in manager.system()
    assert ContextManager(_spec(selection="all"), FakeModelProvider([])).select_memory("x") is None


def test_a_run_journals_its_selection_and_the_locator_can_contrast_it(harness_dir: Path):
    for entry in ENTRIES:
        construct.add_memory_entry(
            harness_dir, kind=entry["kind"], title=entry["title"], content=entry["content"],
            entry_id=entry["id"],
        )
    construct.set_field(harness_dir, "memory.selection", "relevant")
    result = runner.run_harness(
        harness_dir, "fix the date formats",
        provider=FakeModelProvider([text_response("done")]),
    )
    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    selected = next(e for e in events if e["type"] == "memory_selected")["payload"]
    assert selected["ids"] == ["iso-dates"] and selected["stored"] == 4
    system = next(e for e in events if e["type"] == "context_system")["payload"]["system"]
    assert "Dates in ISO 8601" in system and "Report tone" not in system
    with Hive() as hive:
        [run] = hive.feature_population(load_spec(harness_dir).identity)
    assert "memory:iso-dates" in run["features"]
    with Hive() as hive:
        assert locate_signal(hive, load_spec(harness_dir).identity).verdict == "no_failures"
