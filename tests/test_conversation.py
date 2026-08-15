"""Multi-turn conversation input: seeding prior turns into a run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import runner
from hiveloom.models.fake import FakeModelProvider, text_response

THREAD = [
    {"role": "user", "content": "Quanti utenti dormienti abbiamo?"},
    {"role": "assistant", "content": "Ci sono 1.240 utenti dormienti."},
    {"role": "user", "content": "E quanto vale il loro AUM?"},
]


# --------------------------------------------------------------------------- #
# split_conversation
# --------------------------------------------------------------------------- #
def test_split_conversation_returns_history_and_task_statement():
    history, task = runner.split_conversation(THREAD)
    assert task == "E quanto vale il loro AUM?"
    assert history == THREAD[:2]


def test_split_conversation_accepts_a_single_user_turn():
    history, task = runner.split_conversation([{"role": "user", "content": "ciao"}])
    assert history == []
    assert task == "ciao"


def test_split_conversation_copies_rather_than_aliasing_the_caller_list():
    history, _ = runner.split_conversation(THREAD)
    history[0]["content"] = "mutated"
    assert THREAD[0]["content"] == "Quanti utenti dormienti abbiamo?"


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([], "must not be empty"),
        ([{"role": "system", "content": "x"}], "must be 'user' or 'assistant'"),
        ([{"role": "user"}], "has no 'content'"),
        (
            [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
            "repeats the 'user' role",
        ),
        (
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
            "last message must be from the user",
        ),
        ([{"role": "user", "content": ["blocks"]}], "content must be a string"),
    ],
)
def test_split_conversation_rejects_malformed_threads(messages, expected):
    with pytest.raises(ValueError, match=expected):
        runner.split_conversation(messages)


# --------------------------------------------------------------------------- #
# run_harness / dry_run
# --------------------------------------------------------------------------- #
def test_run_seeds_history_before_the_task_statement(harness_dir: Path):
    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(harness_dir, conversation=THREAD, provider=provider)

    assert result.status == "success"
    sent = provider.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "Quanti utenti dormienti abbiamo?"
    assert sent[-1]["content"] == "E quanto vale il loro AUM?"


def test_single_shot_input_still_sends_one_message(harness_dir: Path):
    provider = FakeModelProvider([text_response("done")])
    runner.run_harness(harness_dir, "just this", provider=provider, literal_input=True)

    sent = provider.calls[0]["messages"]
    assert len(sent) == 1
    assert sent[0] == {"role": "user", "content": "just this"}


def test_run_requires_exactly_one_input_form(harness_dir: Path):
    provider = FakeModelProvider([text_response("done")])
    with pytest.raises(ValueError, match="exactly one"):
        runner.run_harness(harness_dir, "x", conversation=THREAD, provider=provider)
    with pytest.raises(ValueError, match="exactly one"):
        runner.run_harness(harness_dir, provider=provider)


def test_trace_records_the_task_statement_and_history_depth(harness_dir: Path):
    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(harness_dir, conversation=THREAD, provider=provider)

    started = next(
        json.loads(line)
        for line in Path(result.trace_path).read_text().splitlines()
        if json.loads(line)["type"] == "run_started"
    )
    assert started["payload"]["input"] == "E quanto vale il loro AUM?"
    assert started["payload"]["history_turns"] == 2


def test_conversation_content_is_never_resolved_as_a_file(harness_dir: Path):
    """A turn that happens to name a real file stays literal text."""
    (harness_dir / "notes.txt").write_text("SECRET FILE BODY")
    provider = FakeModelProvider([text_response("done")])
    runner.run_harness(
        harness_dir,
        conversation=[{"role": "user", "content": "notes.txt"}],
        provider=provider,
    )
    assert provider.calls[0]["messages"][0]["content"] == "notes.txt"


def test_dry_run_assembles_the_whole_thread(harness_dir: Path):
    info = runner.dry_run(harness_dir, conversation=THREAD)
    assert [m["role"] for m in info["messages"]] == ["user", "assistant", "user"]
    assert info["messages"][-1]["content"] == "E quanto vale il loro AUM?"


# --------------------------------------------------------------------------- #
# compaction interaction
# --------------------------------------------------------------------------- #
def test_seeded_history_is_not_pinned_against_compaction(harness_dir: Path):
    """History is the first thing reclaimed; the task statement is newest."""
    from hiveloom.context.manager import ContextManager
    from hiveloom.spec.loader import load_spec

    spec = load_spec(harness_dir / "harness.yaml")
    provider = FakeModelProvider([])
    context = ContextManager(spec, provider)

    context.add_user("single shot")
    assert context.pinned_message_count == 1  # task statement is message[0]

    context.messages.clear()
    context.seed_history(THREAD[:2])
    context.add_user(THREAD[-1]["content"])
    assert context.pinned_message_count == 0
    assert context.messages[-1]["content"] == "E quanto vale il loro AUM?"
