"""best_of_n: independent attempts, consensus selection.

Every other loop policy shapes a single line of reasoning. This one changes how
many there are — the only lever a harness has against a model that is simply
wrong, which on the ARC-AGI-2 suite is ~53% of the loss and the part no prompt,
tool or limit change has ever moved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.schema import LoopConfig


def _events(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _harness(tmp_path: Path, attempts: int = 3) -> Path:
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="best-of-n", task="Answer the question.")
    construct.set_value(harness, "loop.require_verification", False)
    construct.set_value(harness, "loop.max_turns", 12)
    construct.set_value(harness, "loop.attempts", attempts)
    construct.set_value(harness, "loop.policy", "best_of_n")
    return harness


def test_the_majority_answer_wins_over_the_last_one(tmp_path: Path):
    """The point of the vote: the run does not submit whatever it said last.

    Two attempts agree on "42" and the third says "7". A single-sample policy
    submits "7" one time in three; this one submits "42" every time.
    """
    harness = _harness(tmp_path, attempts=3)
    provider = FakeModelProvider(
        [text_response("42"), text_response("42"), text_response("7")]
    )

    result = runner.run_harness(harness, "what is it?", provider=provider)

    assert result.status == "success"
    assert result.output == "42"
    assert len(provider.calls) == 3


def test_each_attempt_starts_without_seeing_the_previous_one(tmp_path: Path):
    """Independence is the mechanism; losing it silently turns N samples into 1.

    An attempt that can read its predecessor anchors on the answer it can see,
    so the votes stop being independent and the consensus stops being evidence.
    Every attempt must therefore be handed the same prompt the first one got.
    """
    harness = _harness(tmp_path, attempts=3)
    provider = FakeModelProvider(
        [text_response("alpha"), text_response("beta"), text_response("alpha")]
    )

    runner.run_harness(harness, "what is it?", provider=provider)

    # No attempt can see any earlier answer: that is what keeps the votes
    # independent and the consensus meaningful.
    rendered = json.dumps([call["messages"] for call in provider.calls])
    assert "alpha" not in rendered
    assert "beta" not in rendered
    # The task statement survives every rewind, and the only thing added is one
    # restart instruction that names neither the attempt nor the budget.
    for call in provider.calls:
        assert call["messages"][0] == {"role": "user", "content": "what is it?"}
        assert len(call["messages"]) <= 2
    restart = provider.calls[1]["messages"][1]["content"]
    assert "2" not in restart and "3" not in restart


def test_three_different_answers_fall_back_to_the_first(tmp_path: Path):
    """With no agreement the samples are interchangeable.

    The first is the only one not chosen after seeing the others, so picking it
    adds no selection bias — and the trace says the vote was 1, so nobody reads
    the answer as agreed-on.
    """
    harness = _harness(tmp_path, attempts=3)
    provider = FakeModelProvider(
        [text_response("a"), text_response("b"), text_response("c")]
    )

    result = runner.run_harness(harness, "what is it?", provider=provider)

    assert result.output == "a"
    selected = [e for e in _events(result.trace_path) if e["type"] == "attempts_selected"]
    assert selected[0]["payload"]["votes"] == 1
    assert selected[0]["payload"]["distinct"] == 3
    assert selected[0]["payload"]["unanimous"] is False


def test_answers_agree_on_content_not_on_whitespace(tmp_path: Path):
    harness = _harness(tmp_path, attempts=3)
    provider = FakeModelProvider(
        [text_response("the  answer"), text_response("the answer\n"), text_response("no")]
    )

    result = runner.run_harness(harness, "what is it?", provider=provider)

    assert result.output == "the  answer"
    selected = [e for e in _events(result.trace_path) if e["type"] == "attempts_selected"]
    assert selected[0]["payload"]["votes"] == 2


def test_the_trace_counts_every_attempt_and_the_vote(tmp_path: Path):
    """The mechanism has to be countable, not inferred from the score.

    Every real finding in the ARC suite came from counting something. A policy
    whose whole claim is "more samples agree" that does not record how many
    agreed cannot support the claim.
    """
    harness = _harness(tmp_path, attempts=3)
    provider = FakeModelProvider(
        [text_response("x"), text_response("x"), text_response("x")]
    )

    result = runner.run_harness(harness, "q", provider=provider)
    events = _events(result.trace_path)

    recorded = [e for e in events if e["type"] == "attempt_recorded"]
    assert [e["payload"]["attempt"] for e in recorded] == [1, 2, 3]
    # Identical answers share a fingerprint, so the vote can be rebuilt from
    # the trace: a length alone cannot tell two same-sized answers apart.
    prints = {e["payload"]["fingerprint"] for e in recorded}
    assert len(prints) == 1
    assert [e for e in events if e["type"] == "attempts_selected"][0]["payload"][
        "winner"
    ] == prints.pop()
    selected = [e for e in events if e["type"] == "attempts_selected"]
    assert selected[0]["payload"]["unanimous"] is True
    assert selected[0]["payload"]["votes"] == 3
    assert [e["type"] for e in events].count("context_rewound") == 2


def test_attempts_one_is_a_control_arm_not_an_error(tmp_path: Path):
    """An A/B where only `attempts` moves needs a legal N=1."""
    harness = _harness(tmp_path, attempts=1)
    provider = FakeModelProvider([text_response("only")])

    result = runner.run_harness(harness, "q", provider=provider)

    assert result.output == "only"
    assert len(provider.calls) == 1


def test_the_spec_bounds_attempts():
    with pytest.raises(ValueError):
        LoopConfig(policy="best_of_n", attempts=0)
    with pytest.raises(ValueError):
        LoopConfig(policy="best_of_n", attempts=17)
    assert LoopConfig(policy="best_of_n").attempts == 3


def test_a_terminating_tool_answer_is_voted_on_like_any_other(tmp_path: Path):
    """The path that matters for a harness that hands its answer over as a tool.

    `submit_answer`-style tools end the run from inside a tool result, skipping
    the completion turn entirely. A policy that only banked candidates on the
    no-tool-call path would silently degrade to one attempt on exactly the
    harnesses built to avoid transcription errors — and the score would look
    like the policy simply did not help.
    """
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="best-of-n-tool", task="Answer it.")
    construct.set_value(harness, "loop.require_verification", False)
    construct.set_value(harness, "loop.max_turns", 12)
    construct.set_value(harness, "loop.attempts", 3)
    construct.set_value(harness, "loop.policy", "best_of_n")
    (harness / "tools").mkdir(exist_ok=True)
    (harness / "tools" / "submit.py").write_text(
        '"""A terminating submit tool."""\n'
        "from hiveloom.tools import tool\n"
        "from hiveloom.tools.registry import ToolResult\n"
        "\n"
        "@tool(description='Submit the final answer.')\n"
        "def submit(answer: str) -> ToolResult:\n"
        "    return ToolResult(content=answer, terminate=True)\n",
        encoding="utf-8",
    )
    construct.add_tool(
        harness, code="tools/submit.py:submit", description="Submit the final answer."
    )

    provider = FakeModelProvider(
        [
            tool_response("submit", {"answer": "42"}, call_id="c1"),
            tool_response("submit", {"answer": "7"}, call_id="c2"),
            tool_response("submit", {"answer": "42"}, call_id="c3"),
        ]
    )
    result = runner.run_harness(harness, "q", provider=provider)

    assert result.status == "success"
    assert result.output == "42"
    events = _events(result.trace_path)
    selected = [e for e in events if e["type"] == "attempts_selected"]
    assert selected[0]["payload"]["attempts"] == 3
    assert selected[0]["payload"]["votes"] == 2


def test_verification_runs_on_the_consensus_not_on_the_first_attempt(tmp_path: Path):
    """A passing first attempt must not end the run before the vote happens.

    Verification returns from the loop the moment an output passes, so a policy
    whose restart decision came *after* it would be silently degraded to one
    attempt on exactly the harnesses that validate their answers — and the
    score would read as "best_of_n did not help" rather than "best_of_n never
    ran".
    """
    harness = tmp_path / "harness"
    construct.init_harness(harness, name="best-of-n-verified", task="Answer it.")
    construct.set_value(harness, "loop.max_turns", 12)
    construct.set_value(harness, "loop.attempts", 3)
    construct.set_value(harness, "loop.policy", "best_of_n")
    construct.set_value(harness, "loop.require_verification", True)

    provider = FakeModelProvider(
        [text_response("7"), text_response("42"), text_response("42")]
    )
    result = runner.run_harness(harness, "q", provider=provider)

    assert result.status == "success"
    # Not "7": the first attempt verified, but the vote still decided.
    assert result.output == "42"
    assert len(provider.calls) == 3


def test_running_out_of_turns_submits_the_best_completed_attempt(tmp_path: Path):
    """Two finished answers beat the partial text the loop happened to hold.

    Exhausting the budget mid-sweep used to discard every completed attempt and
    submit `state.output`, which at that point is usually empty — turning a
    budget mistake into a zero on a task the model had already answered.
    """
    harness = _harness(tmp_path, attempts=5)
    construct.set_value(harness, "loop.max_turns", 3)
    provider = FakeModelProvider(
        [text_response("42"), text_response("42"), text_response("9")]
    )

    result = runner.run_harness(harness, "q", provider=provider)

    assert result.status == "max_turns"
    assert result.output == "42"
    events = _events(result.trace_path)
    truncated = [e for e in events if e["type"] == "attempts_truncated"]
    assert truncated[0]["payload"] == {
        "policy": "best_of_n",
        "attempts": 3,
        "target": 5,
        "votes": 2,
    }


def test_default_sampling_setting_preserves_other_policy_serialization():
    from hiveloom.spec.loader import spec_to_dict
    from hiveloom.spec.schema import HarnessSpec

    spec = HarnessSpec(name="h", description="d", system_prompt="s")
    assert "attempts" not in spec_to_dict(spec)["loop"]
    spec.loop.policy = "best_of_n"
    assert spec_to_dict(spec)["loop"]["attempts"] == 3


def test_sampling_keeps_an_explicit_count_configured_before_selecting_policy(tmp_path: Path):
    from hiveloom.spec.loader import load_spec

    harness = _harness(tmp_path, attempts=5)
    assert load_spec(harness).loop.attempts == 5
