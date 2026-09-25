"""A turn cut off at the output ceiling is feedback, not an answer.

A reasoning model can spend an entire output budget thinking and emit no text,
no answer and no tool call. The loop used to read "no tool calls" as "the model
is signalling completion" and score the partial thought as the final output —
so a run that never actually answered looked like a run that answered badly.
These tests pin the corrected behaviour, and the `model.params` escape hatch
that lets a harness bound the thinking in the first place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.loop.agent_loop import AgentLoop
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.models.provider import ModelConfig, ModelResponse, Usage
from hiveloom.spec.schema import ModelConfig as SpecModelConfig


class RecordingProvider(FakeModelProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.configs = []

    def complete(self, **kwargs):
        self.configs.append(kwargs["config"].model_copy(deep=True))
        return super().complete(**kwargs)


def truncated_response(text: str = "Let me think about this some more...") -> ModelResponse:
    """What a model returns when it thinks past its output ceiling."""
    return ModelResponse(
        text=text,
        tool_calls=[],
        stop_reason="max_tokens",
        usage=Usage(input_tokens=100, output_tokens=16000),
        content_blocks=[{"type": "text", "text": text}],
    )


def _user_messages(provider: FakeModelProvider) -> list[str]:
    return [
        message["content"]
        for message in provider.calls[-1]["messages"]
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]


def test_a_truncated_turn_is_not_accepted_as_the_final_answer(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    provider = FakeModelProvider([truncated_response(), text_response("42")])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    assert result.status == "success"
    # The thought is not the answer: the run's output is what came after it.
    assert result.output == "42"


def test_truncation_comes_back_as_feedback_the_model_can_act_on(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    provider = FakeModelProvider([truncated_response(), text_response("42")])

    runner.run_harness(harness_dir, "go", provider=provider)

    feedback = _user_messages(provider)[-1]
    assert "output limit" in feedback
    assert "no answer and no tool call" in feedback
    # The partial text is in the transcript the model can see, so the message
    # must not claim it was discarded — that would invite a full restatement,
    # which is the very thing that blew the budget.
    assert "still counts" in feedback
    assert "do not restate" in feedback.lower()


def test_repeated_truncation_stops_the_run_instead_of_burning_every_turn(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_field(harness_dir, "loop.max_turns", "40")
    provider = FakeModelProvider([truncated_response() for _ in range(40)])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    assert result.status == "truncated"
    assert "output ceiling" in result.reason
    # Three attempts, not forty: the run stops once escalation is clearly failing.
    assert len(provider.calls) == 3
    # Whatever the last turn managed is still carried out of the run.
    assert result.output == "Let me think about this some more..."


def test_the_second_truncation_says_something_new(harness_dir: Path):
    """A model losing to its own verbosity will re-read the same words the same way."""
    construct.set_field(harness_dir, "loop.require_verification", "false")
    provider = FakeModelProvider([truncated_response(), truncated_response(), text_response("42")])

    runner.run_harness(harness_dir, "go", provider=provider)

    sent = [m for m in _user_messages(provider) if "cut off" in m or "output limit" in m]
    assert len(sent) == 2
    assert sent[0] != sent[1]
    assert "in a row" in sent[1]
    assert "before any explanation" in sent[1]


def test_a_productive_policy_turn_clears_the_streak(harness_dir: Path):
    construct.set_value(harness_dir, "loop.steps", ["Analyze", "Answer"])
    construct.set_value(harness_dir, "loop.policy", "sequential_steps")
    provider = FakeModelProvider([
        truncated_response(), truncated_response(), text_response("analysis complete"),
        truncated_response(), truncated_response(), text_response("42"),
    ])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert result.output == "42"
    assert len(provider.calls) == 6


def test_truncation_is_recorded_in_the_trace(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    provider = FakeModelProvider([truncated_response(), text_response("42")])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    truncations = [e for e in events if e["type"] == "turn_truncated"]
    assert len(truncations) == 1
    assert truncations[0]["payload"]["consecutive"] == 1
    assert truncations[0]["payload"]["output_tokens"] == 16000


# --------------------------------------------------------------------------- #
# model.params: bounding the thinking at the source
# --------------------------------------------------------------------------- #
def test_model_params_reach_the_provider_payload(monkeypatch):
    from hiveloom.models.openai_compat import OpenAICompatProvider

    sent: dict = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    def fake_urlopen(request, timeout=0):
        sent.update(json.loads(request.data.decode()))
        return _Response()

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", fake_urlopen)
    OpenAICompatProvider("http://localhost:9").complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m", params={"reasoning": {"enabled": False}}),
    )

    assert sent["reasoning"] == {"enabled": False}
    assert sent["model"] == "m"


def test_model_params_cannot_restate_what_the_harness_owns():
    for field in ("model", "messages", "tools", "max_tokens", "temperature"):
        with pytest.raises(ValueError, match="model.params cannot set"):
            SpecModelConfig(provider="openai", id="gpt-4o", params={field: "x"})


def test_model_params_default_to_nothing_sent(monkeypatch):
    """Every provider that needs no extra fields must see the payload unchanged."""
    from hiveloom.models.openai_compat import OpenAICompatProvider

    sent: dict = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            ).encode()

    monkeypatch.setattr(
        "hiveloom.models.openai_compat.urlrequest.urlopen",
        lambda request, timeout=0: (sent.update(json.loads(request.data.decode())), _Response())[1],
    )
    OpenAICompatProvider("http://localhost:9").complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m"),
    )

    assert set(sent) == {"model", "max_tokens", "messages"}


def test_a_one_turn_harness_still_submits_what_the_model_produced(harness_dir: Path):
    """Feedback is pointless with no turn left to act on it.

    A single-turn arm (a `raw` eval baseline, say) that truncates must still
    hand over its partial text: the answer may well be in there, and the
    verifiers are what decide. Withholding it scores a produced answer as none.
    """
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_field(harness_dir, "loop.max_turns", "1")
    provider = FakeModelProvider([truncated_response("...working... the answer is 42")])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    assert result.status == "truncated"
    assert result.output == "...working... the answer is 42"
    assert len(provider.calls) == 1


def test_the_last_turn_of_a_longer_loop_also_submits_its_partial_output(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_field(harness_dir, "loop.max_turns", "2")
    provider = FakeModelProvider([truncated_response("first"), truncated_response("second")])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    # Turn 1 had a turn left, so it fed back; turn 2 was the last, so it counts.
    assert result.status == "truncated"
    assert result.output == "second"
    assert len(provider.calls) == 2


def test_giving_up_on_a_truncation_streak_still_lets_the_output_verify(harness_dir: Path):
    """The bound stops the loop spinning; it must not throw away a good answer."""
    construct.set_field(harness_dir, "loop.max_turns", "40")
    provider = FakeModelProvider([truncated_response("partial but valid") for _ in range(40)])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    assert result.output == "partial but valid"
    assert len(provider.calls) == 3


# --------------------------------------------------------------------------- #
# Escalation: give the model the room it asked for before telling it to want less
# --------------------------------------------------------------------------- #
_BIG_MODEL_YAML = """
providers:
  roomy:
    api: openai_compat
    base_url: https://roomy.example.test/v1
    models:
      - id: long-answers
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
        max_output_tokens: 200000
"""


def _roomy_harness(harness_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "models.yaml").write_text(_BIG_MODEL_YAML, encoding="utf-8")
    from hiveloom import ext

    ext.reset()
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_model(harness_dir, "roomy/long-answers")
    construct.set_field(harness_dir, "model.max_tokens", "16000")


def test_truncation_keeps_the_operator_configured_budget(
    harness_dir: Path, tmp_path: Path, monkeypatch
):
    _roomy_harness(harness_dir, tmp_path, monkeypatch)
    provider = RecordingProvider([truncated_response(), text_response("42")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert [config.max_tokens for config in provider.configs] == [16000, 16000]
    assert "16000-token output limit" in _user_messages(provider)[-1]


def test_an_undeclared_model_is_never_escalated(harness_dir: Path):
    """Raising blindly trades a truncated turn for a provider 400, which is worse."""
    construct.set_field(harness_dir, "loop.require_verification", "false")
    provider = FakeModelProvider([truncated_response(), text_response("42")])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    assert [e for e in events if e["type"] == "output_budget_raised"] == []
    # It still gets the "be brief" message, which is all that is left to offer.
    assert "output limit" in _user_messages(provider)[-1]
    assert result.status == "success"


def test_truncation_at_the_declared_ceiling_keeps_feedback(
    harness_dir: Path, tmp_path: Path, monkeypatch
):
    _roomy_harness(harness_dir, tmp_path, monkeypatch)
    construct.set_field(harness_dir, "model.max_tokens", "200000")
    provider = FakeModelProvider([truncated_response(), text_response("42")])

    runner.run_harness(harness_dir, "go", provider=provider)

    # Already at the ceiling: nothing to raise, so say the useful thing instead.
    assert "output limit" in _user_messages(provider)[-1]


# --------------------------------------------------------------------------- #
# The model is stateless: continuation only works if the partial work is resent
# --------------------------------------------------------------------------- #
def test_the_truncated_text_is_actually_in_the_next_request(harness_dir: Path):
    """The feedback tells the model its work "is above". That must be true.

    A model carries nothing between calls, so "carry on from where you were cut
    off" is actionable only if the partial turn is in the transcript we resend.
    If it is not, the instruction is a lie and the only thing the model can do
    is start over — which is what exhausted the budget in the first place.
    """
    construct.set_field(harness_dir, "loop.require_verification", "false")
    partial = "I have established the rule is a diagonal flip and was about to"
    provider = FakeModelProvider([truncated_response(partial), text_response("42")])

    runner.run_harness(harness_dir, "go", provider=provider)

    second_request = provider.calls[-1]["messages"]
    flattened = json.dumps(second_request)
    assert partial in flattened, "the model cannot continue work it cannot see"
    # And the nudge to continue arrives after it, not before.
    assert flattened.index(partial) < flattened.rindex("output limit")


def test_a_blank_content_turn_does_not_erase_the_models_work(harness_dir: Path):
    """The upstream that returns content " " beside a full reasoning field.

    Normalizing that to " " wrote a blank assistant turn into the transcript,
    so a stateless model saw no trace of its own thinking and had to restart
    every turn. Pinned here because the loop's continuation strategy rests on it.
    """
    from hiveloom.models.openai_compat import normalize_openai_response

    response = normalize_openai_response(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": " ", "reasoning": "step 1: the grid is cropped"},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 16000},
        },
        estimated_input_tokens=10,
    )

    blocks = json.dumps(AgentLoop.assistant_blocks(response))
    assert "step 1: the grid is cropped" in blocks


def test_a_model_override_keeps_the_declared_provider_params(tmp_path: Path, monkeypatch):
    """An override selects which model runs, not how the harness calls it.

    Every eval cell goes through this path, so dropping `params` here sent a
    pinned run to whichever upstream a router chose — indistinguishable from
    model variance in the results, and invisible in the spec on disk.
    """
    from hiveloom.runner import _apply_runtime_model_overrides
    from hiveloom.spec.schema import HarnessSpec

    spec = HarnessSpec.model_validate(
        {
            "name": "h",
            "description": "d",
            "system_prompt": "s",
            "model": {
                "provider": "openai",
                "id": "gpt-4o",
                "params": {"provider": {"order": ["acme/fp8"], "allow_fallbacks": False}},
            },
        }
    )

    overridden = _apply_runtime_model_overrides(spec, "gpt-4.1", None)

    assert overridden.model.id == "gpt-4.1"
    assert overridden.model.params == {
        "provider": {"order": ["acme/fp8"], "allow_fallbacks": False}
    }


def test_run_passes_provider_params_to_the_real_router(harness_dir: Path):
    construct.set_value(harness_dir, "model.params", {"seed": 7})
    provider = RecordingProvider([text_response("ok")])
    runner.run_harness(harness_dir, "go", provider=provider)
    assert provider.configs[0].params == {"seed": 7}


@pytest.mark.parametrize("field", ["max_completion_tokens", "max_output_tokens", "system",
                                   "input", "functions", "stream", "stream_options", "n"])
def test_provider_aliases_cannot_bypass_harness_request_controls(field):
    for cls in (SpecModelConfig, ModelConfig):
        with pytest.raises(ValueError, match="model.params cannot set"):
            cls(id="claude-haiku-4-5", params={field: 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_provider_params_must_be_json_safe(value):
    with pytest.raises(ValueError, match="JSON-safe"):
        SpecModelConfig(params={"seed": value})


def test_salvaged_output_cannot_skip_required_policy_steps(harness_dir: Path):
    construct.set_value(harness_dir, "loop.steps", ["Analyze", "Answer"])
    construct.set_value(harness_dir, "loop.policy", "sequential_steps")
    construct.set_value(harness_dir, "loop.max_turns", 1)
    result = runner.run_harness(harness_dir, "go", provider=FakeModelProvider([
        truncated_response("passes the non-empty validator"),
    ]))
    assert result.status == "truncated"
    assert "policy requires further work" in result.reason


def test_truncation_friction_does_not_claim_a_failed_run_recovered(harness_dir: Path):
    from hiveloom.logging.hive import Hive
    from hiveloom.spec.loader import load_spec

    construct.set_value(harness_dir, "loop.require_verification", False)
    construct.set_value(harness_dir, "loop.max_turns", 1)
    result = runner.run_harness(harness_dir, "go", provider=FakeModelProvider([
        truncated_response(),
    ]))
    with Hive() as hive:
        rows = hive.list_friction(load_spec(harness_dir).identity, limit=10)
    truncated = [row for row in rows if row["category"] == "output_truncated"]
    assert truncated
    assert all(not row["recovered"] for row in truncated)
    assert result.status == "truncated"
