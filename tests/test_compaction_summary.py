"""Summarize-compaction carries the previous summary forward and anchors the newest state."""

from __future__ import annotations

from hiveloom.context.manager import _summary_prompt


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
