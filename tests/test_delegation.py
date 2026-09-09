"""Built-in delegation: the directory, the selection, and the hand-off.

Everything here runs on :class:`FakeModelProvider` — the parent's provider is
passed in, and each peer harness is put on a registered fixture provider so the
child builds its own from its own spec, exactly as it does in production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hiveloom import construct, delegation, ext, registry, runner, trust
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import ModelConfig
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import DelegationConfig, HarnessSpec

PEER_ANSWER = "PEER-OK the peer did the work"


def _fixture_provider(scripts: dict[str, list[Any]]) -> None:
    """Register a provider that scripts each harness by folder name."""

    def factory(ctx):
        name = Path(ctx.base).name if ctx.base else ""
        return FakeModelProvider(list(scripts.get(name, [text_response("done")])))

    ext.ExtensionAPI(source="test:delegation").register_provider(
        "fixture", factory, open_catalog=True, label="Fixture"
    )


def _harness(tmp_path: Path, name: str, task: str = "Do a small thing.") -> Path:
    directory = tmp_path / name
    construct.init_harness(directory, name=name, task=task)
    construct.set_model(directory, "fixture/fake-model")
    return directory


def _delegation_config(directory: Path, **fields: Any) -> None:
    construct.set_value(directory, "delegation", {"enabled": True, **fields})


def _events(result) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(result.trace_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _of_type(result, event_type: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in _events(result) if e["type"] == event_type]


def _candidate(name: str = "peer-a", **fields: Any) -> delegation.PeerCandidate:
    base = {
        "name": name,
        "harness_id": f"hl-{name}",
        "description": f"{name} does one thing",
        "path": f"/tmp/{name}",
    }
    base.update(fields)
    return delegation.PeerCandidate(**base)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_delegation_defaults_are_off_and_conservative():
    spec = HarnessSpec(name="x", description="d", system_prompt="p")
    assert spec.delegation.enabled is False
    assert spec.delegation.directory == "local"
    assert spec.delegation.when == ["model_choice"]
    assert spec.delegation.min_peer_success_rate == 0.0
    assert spec.delegation.min_peer_runs == 0
    assert spec.delegation.max_depth == 2
    assert spec.delegation.budget_share == 0.5
    assert spec.delegation.exclude == []


def test_delegation_is_tunable_by_evolution_but_the_model_is_not():
    spec = HarnessSpec(name="x", description="d", system_prompt="p")
    assert "delegation" in spec.evolution.mutable
    from hiveloom.spec.schema import ALWAYS_FROZEN

    assert "model" in ALWAYS_FROZEN
    assert "delegation" not in ALWAYS_FROZEN


@pytest.mark.parametrize(
    "payload",
    [
        {"when": ["whenever"]},
        {"when": ["on_start", "on_start"]},
        {"min_peer_success_rate": 1.5},
        {"max_depth": 0},
        {"max_depth": 6},
        {"budget_share": -0.1},
        {"directory": "remote"},
        {"unknown_field": True},
    ],
)
def test_delegation_rejects_invalid_configuration(payload: dict[str, Any]):
    with pytest.raises(ValidationError):
        DelegationConfig(**payload)


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #
def test_directory_skips_self_excluded_untrusted_and_broken(tmp_path: Path):
    _fixture_provider({})
    parent = _harness(tmp_path, "parent")
    good = _harness(tmp_path, "peer-good")
    excluded = _harness(tmp_path, "peer-excluded")
    untrusted = _harness(tmp_path, "peer-untrusted")
    broken = _harness(tmp_path, "peer-broken")
    for directory in (parent, good, excluded, untrusted, broken):
        registry.register(directory)
    trust.revoke_trust(untrusted)
    (broken / "harness.yaml").write_text("name: [unclosed", encoding="utf-8")

    _delegation_config(parent, exclude=["peer-excluded"])
    spec = load_spec(parent / "harness.yaml")
    found = delegation.discover_candidates(spec, parent)

    assert [c.name for c in found] == ["peer-good"]
    assert found[0].harness_id == load_spec(good / "harness.yaml").id
    assert found[0].total_runs == 0


def test_directory_reports_hive_fitness(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    runner.run_harness(peer, "warm it up", literal_input=True)

    spec = load_spec(parent / "harness.yaml")
    found = delegation.discover_candidates(spec, parent)
    assert found[0].total_runs == 1
    assert found[0].success_rate == 1.0


def test_eligibility_treats_an_unmeasured_peer_as_unmeasured():
    fresh = _candidate("fresh", total_runs=0, success_rate=0.0)
    proven = _candidate("proven", total_runs=10, success_rate=0.9)
    weak = _candidate("weak", total_runs=10, success_rate=0.2)
    config = DelegationConfig(enabled=True, min_peer_success_rate=0.5, min_peer_runs=3)
    assert [c.name for c in delegation.eligible([fresh, proven, weak], config)] == [
        "proven"
    ]
    # With no floors declared, an unmeasured peer is fair game again.
    assert len(delegation.eligible([fresh, proven, weak], DelegationConfig())) == 3


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def _select(reply: str, candidates=None, config: DelegationConfig | None = None):
    provider = FakeModelProvider([text_response(reply)])
    return delegation.select_peer(
        provider,
        ModelConfig(id="fake-model", provider="fixture"),
        "summarize this article",
        candidates if candidates is not None else [_candidate()],
        config or DelegationConfig(enabled=True),
    )


def test_selection_picks_a_named_candidate():
    selection = _select("peer-a")
    assert selection.candidate is not None
    assert selection.candidate.name == "peer-a"
    assert selection.reason == ""


def test_selection_accepts_none():
    selection = _select("none")
    assert selection.candidate is None
    assert selection.reason == delegation.NONE_FIT


def test_selection_refuses_garbage_and_unknown_names():
    for reply in ("I think maybe you should try harness number two?", "peer-z", ""):
        selection = _select(reply)
        assert selection.candidate is None
        assert selection.reason == delegation.UNPARSABLE


def test_selection_never_calls_the_model_when_nothing_is_eligible():
    provider = FakeModelProvider([text_response("peer-a")])
    selection = delegation.select_peer(
        provider,
        ModelConfig(id="fake-model", provider="fixture"),
        "task",
        [_candidate(total_runs=1, success_rate=0.1)],
        DelegationConfig(enabled=True, min_peer_success_rate=0.9),
    )
    assert selection.candidate is None
    assert selection.reason == delegation.BELOW_FITNESS
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# on_start
# --------------------------------------------------------------------------- #
def _on_start_pair(tmp_path: Path, peer_answer: str = PEER_ANSWER):
    _fixture_provider({"peer-a": [text_response(peer_answer)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a", task="Summarize an article.")
    registry.register(peer)
    _delegation_config(parent, when=["on_start"])
    construct.add_validator(parent, builtin="regex_match", pattern="PEER-OK")
    return parent, peer


def test_on_start_delegates_and_reverifies_with_the_parents_validators(tmp_path: Path):
    parent, _peer = _on_start_pair(tmp_path)
    result = runner.run_harness(
        parent,
        "summarize this",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    assert result.status == "success"
    assert result.output == PEER_ANSWER
    assert [v.verifier for v in result.verdicts] == ["regex_match"]
    assert [d.harness for d in result.delegations] == ["peer-a"]
    selected = _of_type(result, "delegation_selected")
    assert selected[0]["harness"] == "peer-a"
    assert selected[0]["mode"] == "on_start"
    assert _of_type(result, "delegation_started")[0]["depth"] == 1
    assert _of_type(result, "delegation_finished")[0]["status"] == "success"


def test_the_parents_validators_still_judge_a_delegated_answer(tmp_path: Path):
    parent, _peer = _on_start_pair(tmp_path, peer_answer="a peer answer that fails")
    result = runner.run_harness(
        parent,
        "summarize this",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    assert result.status == "verify_failed"
    assert result.output == "a peer answer that fails"
    assert result.delegations[0].status == "success"


def test_on_start_falls_back_to_the_run_itself_when_no_peer_fits(tmp_path: Path):
    parent, _peer = _on_start_pair(tmp_path)
    result = runner.run_harness(
        parent,
        "summarize this",
        literal_input=True,
        provider=FakeModelProvider(
            [text_response("none"), text_response("PEER-OK done locally")]
        ),
    )
    assert result.status == "success"
    assert result.delegations == []
    skipped = _of_type(result, "delegation_skipped")
    assert skipped[0]["reason"] == delegation.NONE_FIT
    assert [r["harness"] for r in result.referrals] == ["peer-a"]
    assert result.referrals[0]["reason"] == delegation.NONE_FIT


# --------------------------------------------------------------------------- #
# on_verify_fail
# --------------------------------------------------------------------------- #
def test_on_verify_fail_escalates_once_after_retries_are_exhausted(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    _delegation_config(parent, when=["on_verify_fail"])
    construct.add_validator(parent, builtin="regex_match", pattern="PEER-OK")
    construct.set_value(parent, "verify.on_fail.max_retries", 1)

    provider = FakeModelProvider(
        [
            text_response("wrong once"),
            text_response("wrong twice"),
            text_response("peer-a"),  # the selection call
        ]
    )
    result = runner.run_harness(parent, "do it", literal_input=True, provider=provider)

    assert result.status == "success"
    assert result.output == PEER_ANSWER
    assert _of_type(result, "delegation_started")[0]["mode"] == "on_verify_fail"
    assert len(result.delegations) == 1


def test_on_verify_fail_reports_the_failure_when_no_peer_is_chosen(tmp_path: Path):
    _fixture_provider({})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    _delegation_config(parent, when=["on_verify_fail"])
    construct.add_validator(parent, builtin="regex_match", pattern="PEER-OK")
    construct.set_value(parent, "verify.on_fail.max_retries", 0)

    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider([text_response("nope"), text_response("none")]),
    )
    assert result.status == "verify_failed"
    assert result.output == "nope"
    assert [r["reason"] for r in result.referrals] == [delegation.NONE_FIT]


# --------------------------------------------------------------------------- #
# model_choice
# --------------------------------------------------------------------------- #
def _model_choice_parent(tmp_path: Path, **fields: Any) -> Path:
    parent = _harness(tmp_path, "parent")
    _delegation_config(parent, when=["model_choice"], **fields)
    return parent


def test_model_choice_defers_the_peer_tools_and_keeps_list_peers_active(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _model_choice_parent(tmp_path)
    registry.register(_harness(tmp_path, "peer-a"))

    provider = FakeModelProvider(
        [
            tool_response("search_tools", {"query": "delegate"}),
            tool_response("delegate__peer-a", {"task": "do the thing"}, call_id="c2"),
            text_response("relayed"),
        ]
    )
    result = runner.run_harness(parent, "do it", literal_input=True, provider=provider)

    first_turn_tools = {t["name"] for t in provider.calls[0]["tools"]}
    assert "list_peers" in first_turn_tools
    assert "delegate__peer-a" not in first_turn_tools
    assert "delegate__peer-a" in {t["name"] for t in provider.calls[1]["tools"]}
    assert result.status == "success"
    assert [d.harness for d in result.delegations] == ["peer-a"]
    assert result.delegations[0].output == PEER_ANSWER


def test_list_peers_is_callable_and_produces_referrals(tmp_path: Path):
    _fixture_provider({})
    parent = _model_choice_parent(tmp_path)
    registry.register(_harness(tmp_path, "peer-a"))

    result = runner.run_harness(
        parent,
        "who does this?",
        literal_input=True,
        provider=FakeModelProvider(
            [tool_response("list_peers", {}), text_response("ask peer-a")]
        ),
    )
    assert result.status == "success"
    assert result.delegations == []
    assert result.referrals == [
        {
            "harness": "peer-a",
            "description": "Do a small thing.",
            "success_rate": 0.0,
            "total_runs": 0,
            "reason": delegation.LISTED,
        }
    ]


def test_model_choice_offers_no_peer_tool_below_the_fitness_floor(tmp_path: Path):
    _fixture_provider({})
    parent = _model_choice_parent(tmp_path, min_peer_success_rate=0.9, min_peer_runs=5)
    registry.register(_harness(tmp_path, "peer-a"))

    provider = FakeModelProvider([text_response("done myself")])
    result = runner.run_harness(parent, "do it", literal_input=True, provider=provider)

    assert "delegate__peer-a" not in {t["name"] for t in provider.calls[0]["tools"]}
    assert "list_peers" in {t["name"] for t in provider.calls[0]["tools"]}
    assert _of_type(result, "delegation_skipped")[0]["reason"] == delegation.BELOW_FITNESS


# --------------------------------------------------------------------------- #
# Depth and cycles
# --------------------------------------------------------------------------- #
def _refusal_run(tmp_path: Path, lineage: dict[str, Any], **fields: Any):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    _delegation_config(parent, when=["on_start"], **fields)
    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider(
            [text_response("peer-a"), text_response("did it myself")]
        ),
        lineage=lineage,
    )
    return result, load_spec(peer / "harness.yaml").id


def test_a_hand_off_past_max_depth_is_refused(tmp_path: Path):
    result, _peer_id = _refusal_run(
        tmp_path,
        {"kind": "delegation", "parent_run_id": "run_root", "depth": 2, "chain": ["hl-root"]},
        max_depth=2,
    )
    assert result.status == "success"
    assert result.output == "did it myself"
    assert result.delegations == []
    assert _of_type(result, "delegation_skipped")[0]["reason"] == delegation.DEPTH
    assert [r["reason"] for r in result.referrals] == [delegation.DEPTH]


def test_a_cycle_back_to_an_ancestor_is_refused(tmp_path: Path):
    _fixture_provider({})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    peer_id = load_spec(peer / "harness.yaml").id
    _delegation_config(parent, when=["on_start"])
    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider(
            [text_response("peer-a"), text_response("did it myself")]
        ),
        lineage={
            "kind": "delegation",
            "parent_run_id": "run_root",
            "parent_harness_id": peer_id,
            "depth": 1,
            "chain": [peer_id],
        },
    )
    assert result.delegations == []
    assert _of_type(result, "delegation_skipped")[0]["reason"] == delegation.CYCLE


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
def test_a_child_is_capped_at_a_share_of_the_parents_remaining_budget(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    # A share small enough that the child cannot afford even its first call.
    _delegation_config(parent, when=["on_start"], budget_share=0.0001)

    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    started = _of_type(result, "delegation_started")[0]
    assert 0 < started["cost_cap_usd"] < 0.001
    assert result.delegations[0].status == "guardrail_halt"
    # The parent adopts the child's outcome rather than reporting a success.
    assert result.status == "guardrail_halt"


def test_delegated_cost_counts_toward_the_parents_own_budget(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    _delegation_config(parent, when=["on_start"])

    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    child = result.delegations[0]
    assert child.cost_usd > 0
    assert result.delegated_cost_usd == pytest.approx(child.cost_usd)
    # The total includes both the parent's own selection call and the child.
    assert result.cost_usd > result.delegated_cost_usd
    finished = _of_type(result, "run_finished")[0]
    assert finished["delegated_cost_usd"] == pytest.approx(child.cost_usd)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def test_the_hive_can_tell_a_delegated_child_from_a_fork(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    peer = _harness(tmp_path, "peer-a")
    registry.register(peer)
    _delegation_config(parent, when=["on_start"])

    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    child_run_id = result.delegations[0].run_id
    with Hive() as hive:
        child = hive.get_run(child_run_id)
        children = hive.children(result.run_id)
        delegated = hive.children(result.run_id, kind="delegation")
    assert child["parent_run_id"] == result.run_id
    assert child["lineage_kind"] == "delegation"
    assert [row["run_id"] for row in children] == [child_run_id]
    assert [row["run_id"] for row in delegated] == [child_run_id]


def test_the_run_payload_carries_delegations_and_referrals(tmp_path: Path):
    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    registry.register(_harness(tmp_path, "peer-a"))
    _delegation_config(parent, when=["on_start"])

    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    payload = runner.run_result_payload(result)
    assert payload["delegations"][0]["harness"] == "peer-a"
    assert payload["delegated_cost_usd"] == pytest.approx(result.delegated_cost_usd)
    assert payload["referrals"] == []
    # The payload is what `hiveloom run --json` prints, so it must serialize.
    json.dumps(payload)


def test_lineage_lists_a_delegated_child_next_to_forks(tmp_path: Path):
    from typer.testing import CliRunner

    from hiveloom.cli import app

    _fixture_provider({"peer-a": [text_response(PEER_ANSWER)]})
    parent = _harness(tmp_path, "parent")
    registry.register(_harness(tmp_path, "peer-a"))
    _delegation_config(parent, when=["on_start"])

    result = runner.run_harness(
        parent,
        "do it",
        literal_input=True,
        provider=FakeModelProvider([text_response("peer-a")]),
    )
    invoked = CliRunner().invoke(app, ["lineage", result.run_id, "--json"])
    assert invoked.exit_code == 0
    tree = json.loads(invoked.stdout)
    assert [c["lineage_kind"] for c in tree["children"]] == ["delegation"]
    assert tree["children"] == tree["forks"]

    rendered = CliRunner().invoke(app, ["lineage", result.run_id])
    assert rendered.exit_code == 0
    assert "delegated run(s)" in rendered.stdout


def test_evolution_may_tune_delegation_but_not_the_model(tmp_path: Path):
    from hiveloom.evolve.evolver import MutationProposal, gate

    _fixture_provider({})
    parent = _harness(tmp_path, "parent")
    spec = load_spec(parent / "harness.yaml")

    accepted = gate(
        spec,
        MutationProposal(
            yaml_changes=[{"path": "delegation.min_peer_success_rate", "value": 0.8}]
        ),
    )
    assert accepted.accepted
    refused = gate(
        spec, MutationProposal(yaml_changes=[{"path": "model.id", "value": "big-model"}])
    )
    assert not refused.accepted
