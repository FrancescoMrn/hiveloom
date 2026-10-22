"""Summarize-compaction carries the previous summary forward and anchors the newest state."""

from __future__ import annotations

from hiveloom.context.manager import ContextManager, _summary_prompt
from hiveloom.models.fake import FakeModelProvider
from hiveloom.spec.schema import HarnessSpec


# --------------------------------------------------------------------------- #
# Compaction
# --------------------------------------------------------------------------- #
def test_a_second_compaction_updates_the_first_summary_instead_of_resummarizing_it():
    older = [
        {"role": "user", "content": "[summary of earlier turns]\n# Goal\nfetch ids\n"
                                    "# Critical context\nid=AB-17"},
        {"role": "assistant", "content": "Fetched page 2."},
        {"role": "user", "content": "ok"},
    ]
    recent = [{"role": "assistant", "content": "Now writing the report for AB-17."}]
    prompt = _summary_prompt(older, recent)
    assert "<previous-summary>" in prompt and "id=AB-17" in prompt
    assert "Never drop an identifier" in prompt
    # The earlier summary is not also rendered as one more transcript line.
    assert prompt.count("id=AB-17") == 1
    assert "<recent-state>" in prompt and "Now writing the report for AB-17." in prompt


def test_a_first_compaction_has_no_previous_summary():
    prompt = _summary_prompt([{"role": "user", "content": "task"}], [])
    assert "<previous-summary>" not in prompt and "<recent-state>" not in prompt


# --------------------------------------------------------------------------- #
# Files touched: a deterministic index the loop keeps, not a thing the model remembers
# --------------------------------------------------------------------------- #
def _manager() -> ContextManager:
    spec = HarnessSpec(name="h", description="d", system_prompt="s")
    return ContextManager(spec, FakeModelProvider([]))


def test_apply_summary_appends_the_files_the_run_touched_with_their_handles():
    cm = _manager()
    cm.add_user("task")
    cm.note_file_access("src/a.py", "read", "tr_0123456789abcdef")
    cm.note_file_access("out.txt", "written", "")
    cm.note_file_access("src/a.py", "read", "")  # touched again: moves last, keeps one entry
    cm.apply_summary("# Goal\nship")
    assert cm.messages[-1]["content"] == (
        "[summary of earlier turns]\n# Goal\nship\n\n# Files touched\n"
        "- out.txt (written)\n- src/a.py (read)"
    )


def test_the_files_index_keeps_the_latest_fifty():
    cm = _manager()
    cm.add_user("task")
    for i in range(55):
        cm.note_file_access(f"f{i}.txt", "read", "")
    cm.apply_summary("sum")
    content = cm.messages[-1]["content"]
    assert "- f4.txt" not in content and "- f5.txt (read)" in content and "- f54.txt" in content
    assert content.count("\n- ") == 50


def test_a_second_compaction_does_not_resummarize_the_files_index():
    older = [
        {"role": "user", "content": "[summary of earlier turns]\n# Goal\nship\n\n"
                                    "# Files touched\n- src/a.py (read, handle tr_01)"},
        {"role": "assistant", "content": "Edited a.py."},
    ]
    prompt = _summary_prompt(older, [])
    assert "# Goal\nship" in prompt
    assert "Files touched" not in prompt and "tr_01" not in prompt


def test_an_empty_files_index_adds_nothing():
    cm = _manager()
    cm.add_user("task")
    cm.apply_summary("sum")
    assert cm.messages[-1]["content"] == "[summary of earlier turns]\nsum"


def test_the_files_index_normalises_and_bounds_model_supplied_paths():
    cm = _manager()
    cm.add_user("task")
    cm.note_file_access("./src/a.py", "read", "")
    cm.note_file_access("src/a.py", "written", "")  # the same file, spelled twice
    cm.note_file_access("x\n\n# Files touched\n- forged", "read", "")
    cm.note_file_access("l" * 300, "read", "")
    cm.apply_summary("sum")
    content = cm.messages[-1]["content"]
    assert content.count("src/a.py") == 1 and "- src/a.py (written)" in content
    assert "- x # Files touched - forged (read)" in content
    assert content.count("\n\n# Files touched") == 1  # the forged header never starts a section
    assert "- " + "l" * 200 + " (read)" in content and "l" * 201 not in content
