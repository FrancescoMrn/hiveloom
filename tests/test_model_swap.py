"""Mid-run model hot-swap: the router, the two surfaces, and the evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.logging.hive import Hive
from hiveloom.logging.journal import read_events
from hiveloom.loop.control import RunControl
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import ModelConfig
from hiveloom.models.router import ModelRouter, portable_messages
from hiveloom.spec.loader import load_spec

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def _harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 30)
    return target


def _config(model: str = "claude-haiku-4-5", provider: str = "claude") -> ModelConfig:
    return ModelConfig(id=model, provider=provider, max_tokens=1024)


def _router(provider=None, **kw) -> ModelRouter:
    return ModelRouter.create(
        Path("."), _config(), provider or FakeModelProvider([]), **kw
    )


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #
def test_a_fresh_router_reports_the_spec_model():
    router = _router()
    assert router.config.id == "claude-haiku-4-5"
    assert router.path_key() == "claude:claude-haiku-4-5"
    assert not router.swapped()


def test_switching_moves_the_config_and_extends_the_path():
    router = _router()
    switch = router.switch(model="claude-opus-5", turn=3, reason="operator")

    assert switch is not None and switch.key == "claude:claude-opus-5"
    assert router.config.id == "claude-opus-5"
    assert router.path_key() == "claude:claude-haiku-4-5>claude:claude-opus-5"
    assert router.swapped()


def test_switching_to_the_same_model_is_a_no_op():
    router = _router()
    assert router.switch(model="claude-haiku-4-5") is None
    assert not router.swapped()


def test_switching_back_does_not_collapse_the_path():
    """A>B>A is a different experiment from A; the path must say so."""
    router = _router()
    router.switch(model="claude-opus-5")
    router.switch(model="claude-haiku-4-5")
    assert router.path_key() == (
        "claude:claude-haiku-4-5>claude:claude-opus-5>claude:claude-haiku-4-5"
    )


def test_max_tokens_carries_across_a_switch():
    router = _router()
    router.switch(model="claude-opus-5")
    assert router.config.max_tokens == 1024


def test_a_registered_provider_serves_a_cross_provider_switch():
    other = FakeModelProvider([])
    router = _router(providers={"openai": other})
    router.switch(model="gpt-5", provider="openai")
    assert router.provider is other


def test_an_unknown_provider_raises_at_the_switch_not_later():
    router = _router()
    with pytest.raises(Exception, match="unknown model provider"):
        router.switch(model="whatever", provider="not-a-provider")
    # And the run stays on the model it had.
    assert router.config.id == "claude-haiku-4-5"


# --------------------------------------------------------------------------- #
# Context portability
# --------------------------------------------------------------------------- #
def test_thinking_blocks_are_stripped_at_a_swap():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                {"type": "text", "text": "answer"},
            ],
        },
    ]
    out, dropped = portable_messages(messages)
    assert dropped == 1
    assert [b["type"] for b in out[1]["content"]] == ["text"]


def test_any_signed_block_is_stripped_whatever_it_calls_itself():
    messages = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "keep"},
            {"type": "text", "text": "drop", "signature": "s"},
        ]}
    ]
    out, dropped = portable_messages(messages)
    assert dropped == 1 and len(out[0]["content"]) == 1


def test_tool_use_and_results_survive_a_swap():
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]},
    ]
    out, dropped = portable_messages(messages)
    assert dropped == 0 and out == messages


def test_a_turn_stripped_to_nothing_is_dropped_whole():
    """An empty assistant turn breaks role alternation on every provider."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]},
        {"role": "user", "content": "again"},
    ]
    out, _ = portable_messages(messages)
    assert [m["role"] for m in out] == ["user", "user"]


def test_string_content_is_left_alone():
    messages = [{"role": "user", "content": "plain"}]
    assert portable_messages(messages) == (messages, 0)


# --------------------------------------------------------------------------- #
# The imperative surface
# --------------------------------------------------------------------------- #
def test_run_control_queues_and_drains_a_switch():
    control = RunControl()
    control.switch_model("claude-opus-5", reason="going in circles")
    drained = control.drain_model_switches()

    assert drained == [
        {"model": "claude-opus-5", "provider": None, "reason": "going in circles"}
    ]
    assert control.drain_model_switches() == []


def test_run_control_ignores_an_empty_switch():
    control = RunControl()
    control.switch_model()
    assert control.drain_model_switches() == []


def test_an_operator_switch_takes_effect_at_the_next_turn(tmp_path: Path):
    harness = _harness(tmp_path)
    control = RunControl()
    control.switch_model("claude-opus-5", reason="operator")
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(
        harness, "notes.txt", provider=provider, control=control, ingest=False
    )

    assert result.status == "success"
    events = read_events(result.trace_path)
    swap = next(e for e in events if e["type"] == "model_swap")
    assert swap["payload"]["from"] == "claude:claude-haiku-4-5"
    assert swap["payload"]["to"] == "claude:claude-opus-5"
    assert swap["payload"]["source"] == "operator"


def test_a_failed_switch_is_traced_and_the_run_continues(tmp_path: Path):
    harness = _harness(tmp_path)
    control = RunControl()
    control.switch_model("x", provider="no-such-provider")
    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])

    result = runner.run_harness(
        harness, "notes.txt", provider=provider, control=control, ingest=False
    )

    assert result.status == "success"
    events = read_events(result.trace_path)
    failed = next(e for e in events if e["type"] == "model_swap_failed")
    assert "unknown model provider" in failed["payload"]["error"]
    assert not any(e["type"] == "model_swap" for e in events)


# --------------------------------------------------------------------------- #
# The declarative surface
# --------------------------------------------------------------------------- #
def _playbook_harness(tmp_path: Path) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="pb", task="Profile then decide.")
    construct.add_playbook(
        directory, name="profile", description="Look around cheaply.", entry=True
    )
    construct.add_playbook(
        directory,
        name="decide",
        description="Commit to an answer.",
        model="claude-opus-5",
    )
    return directory


def test_a_playbook_can_declare_its_own_model(tmp_path: Path):
    spec = load_spec(_playbook_harness(tmp_path) / "harness.yaml")
    by_name = {p.name: p for p in spec.playbooks}
    assert by_name["profile"].model is None
    assert by_name["decide"].model == "claude-opus-5"


def test_entering_a_playbook_moves_the_run_onto_its_model(tmp_path: Path):
    harness = _playbook_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("switch_playbook", {"name": "decide"}, call_id="s1"),
            text_response("done"),
        ]
    )
    result = runner.run_harness(harness, "task", provider=provider, ingest=False)

    events = read_events(result.trace_path)
    swap = next(e for e in events if e["type"] == "model_swap")
    assert swap["payload"]["to"] == "claude:claude-opus-5"
    assert swap["payload"]["source"] == "playbook"
    assert "decide" in swap["payload"]["reason"]


def test_leaving_a_playbook_restores_the_harness_model(tmp_path: Path):
    harness = _playbook_harness(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("switch_playbook", {"name": "decide"}, call_id="s1"),
            tool_response("switch_playbook", {"name": "profile"}, call_id="s2"),
            text_response("done"),
        ]
    )
    result = runner.run_harness(harness, "task", provider=provider, ingest=False)

    swaps = [e["payload"] for e in read_events(result.trace_path) if e["type"] == "model_swap"]
    assert [s["to"] for s in swaps] == ["claude:claude-opus-5", "claude:claude-haiku-4-5"]


def test_a_playbook_model_is_frozen_from_evolution(tmp_path: Path):
    """Evolution must not move a harness onto a pricier model on its own."""
    from hiveloom.evolve.evolver import MutationProposal, YamlChange, gate

    spec = load_spec(_playbook_harness(tmp_path) / "harness.yaml")
    result = gate(
        spec,
        MutationProposal(
            yaml_changes=[YamlChange(path="playbooks.1.model", value="claude-opus-5")]
        ),
    )

    assert result.accepted == []
    assert "frozen from evolution" in result.rejected[0]["reason"]


def test_a_playbook_model_cannot_be_smuggled_via_the_whole_list(tmp_path: Path):
    from hiveloom.evolve.evolver import MutationProposal, YamlChange, gate

    spec = load_spec(_playbook_harness(tmp_path) / "harness.yaml")
    result = gate(
        spec,
        MutationProposal(
            yaml_changes=[
                YamlChange(
                    path="playbooks",
                    value=[{"name": "p", "description": "d", "model": "claude-opus-5"}],
                )
            ]
        ),
    )
    assert result.accepted == []


# --------------------------------------------------------------------------- #
# Evidence integrity
# --------------------------------------------------------------------------- #
def test_a_swapped_run_records_its_model_path(tmp_path: Path):
    harness = _harness(tmp_path)
    control = RunControl()
    control.switch_model("claude-opus-5")
    provider = FakeModelProvider(
        [
            tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
            text_response(_VALID_SUMMARY),
        ]
    )
    result = runner.run_harness(
        harness, "notes.txt", provider=provider, control=control, ingest=False
    )

    finished = read_events(result.trace_path)[-1]["payload"]
    assert finished["model_path"] == "claude:claude-haiku-4-5>claude:claude-opus-5"
    assert [m["reason"] for m in finished["models_used"]] == ["spec", ""]


def test_an_ordinary_run_has_a_single_model_path(tmp_path: Path):
    harness = _harness(tmp_path)
    result = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        ingest=False,
    )
    finished = read_events(result.trace_path)[-1]["payload"]
    assert ">" not in finished["model_path"]


def test_swapped_runs_are_held_out_of_the_fitness_bucket(tmp_path: Path):
    harness = _harness(tmp_path)

    clean = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        ingest=False,
    )
    control = RunControl()
    control.switch_model("claude-opus-5")
    swapped = runner.run_harness(
        harness,
        "notes.txt",
        provider=FakeModelProvider(
            [tool_response("file_read", {"path": "notes.txt"}), text_response("not json")]
        ),
        control=control,
        ingest=False,
    )
    assert swapped.status == "verify_failed"

    with Hive(tmp_path / "hive.db") as hive:
        hive.ingest_trace_file(clean.trace_path)
        hive.ingest_trace_file(swapped.trace_path)

        [bucket] = hive.version_stats("example-summarizer")
        # The failing swapped run must not drag the bucket's success rate down:
        # it did not execute the harness as declared.
        assert bucket["runs"] == 1
        assert bucket["success_rate"] == 1.0
        assert bucket["swapped_runs"] == 1

        # ...but it is not hidden. It is reported on its own path.
        raw = hive.version_stats("example-summarizer", include_swapped=True)
        assert raw[0]["runs"] == 2
        paths = hive.model_path_stats("example-summarizer")
        assert any(">" in row["model_path"] for row in paths)


def test_pre_1_0_runs_are_not_treated_as_swapped(tmp_path: Path):
    """An empty model_path means 'not recorded', never 'changed models'."""
    with Hive(tmp_path / "hive.db") as hive:
        hive._conn.execute(
            "INSERT INTO runs (run_id, harness_name, harness_version_hash, status, "
            "turns, cost_usd, duration_seconds) VALUES ('r1','h','v','success',1,0.1,1.0)"
        )
        hive._conn.commit()
        [bucket] = hive.version_stats("h")
    assert bucket["runs"] == 1 and bucket["swapped_runs"] == 0
