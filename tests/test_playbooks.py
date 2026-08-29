"""Playbooks: named modes, their gates, their evidence, and their freeze."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom.errors import SpecError
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.playbooks import PlaybookManager, load_playbooks
from hiveloom.spec.loader import load_spec
from hiveloom.tools.registry import build_registry

READ_TOOL = '''
from hiveloom.tools import tool


@tool(description="Read the warehouse.")
def read_data(query: str) -> str:
    return "rows"
'''

WRITE_TOOL = '''
from hiveloom.tools import tool


@tool(description="Propose a decision.")
def propose(what: str) -> str:
    return f"proposed {what}"
'''

ENTER_HOOK = '''
def on_enter(event):
    return {"context": f"warehouse refreshed for {event['playbook']}"}
'''

BLOCKING_ENTER = '''
def on_enter(event):
    return {"block": True, "reason": "data is stale, refresh first"}
'''

BLOCKING_EXIT = '''
def on_exit(event):
    return {"block": True, "reason": "you entered targeting and proposed nothing"}
'''

RAISING_HOOK = '''
def on_enter(event):
    raise RuntimeError("hook is broken")
'''

PLAYBOOK_VALIDATOR = '''
def check(run_output, run_context):
    ok = "proposta" in run_output
    return {"passed": ok, "feedback": "a targeting answer must mention a proposta"}
'''


def _base_harness(tmp_path: Path) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="pb-harness", task="Profile the user base.")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / "read.py").write_text(READ_TOOL)
    (directory / "tools" / "write.py").write_text(WRITE_TOOL)
    construct.add_tool(directory, code="tools/read.py:read_data", description="Read.")
    construct.add_tool(directory, code="tools/write.py:propose", description="Propose.")
    return directory


def _two_playbooks(tmp_path: Path) -> Path:
    directory = _base_harness(tmp_path)
    construct.add_playbook(
        directory,
        name="overview",
        description="Read the segment landscape. No actions.",
        tools=["read_data"],
        entry=True,
    )
    construct.add_playbook(
        directory,
        name="targeting",
        description="Turn a cohort into a confirmable proposal.",
        tools=["read_data", "propose"],
    )
    return directory


def _manager(directory: Path) -> PlaybookManager:
    spec = load_spec(directory / "harness.yaml")
    registry = build_registry(spec, directory)
    return PlaybookManager(load_playbooks(spec, directory), registry)


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def test_duplicate_playbook_names_are_rejected(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    with pytest.raises(SpecError, match="already listed"):
        construct.add_playbook(directory, name="overview", description="dup")


def test_two_entry_playbooks_are_rejected(tmp_path: Path):
    directory = _base_harness(tmp_path)
    construct.add_playbook(directory, name="a", description="A", entry=True)
    with pytest.raises(SpecError, match="at most one playbook"):
        construct.add_playbook(directory, name="b", description="B", entry=True)


def test_unknown_tool_in_a_subset_is_rejected(tmp_path: Path):
    directory = _base_harness(tmp_path)
    with pytest.raises(SpecError, match="unknown tool"):
        construct.add_playbook(
            directory, name="typo", description="T", tools=["read_dat"]
        )


def test_malformed_hook_ref_is_rejected(tmp_path: Path):
    directory = _base_harness(tmp_path)
    with pytest.raises(SpecError, match="path.py:function_name"):
        construct.add_playbook(
            directory, name="bad", description="B", on_enter="hooks/enter.py"
        )


def test_missing_prompt_file_fails_at_load(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    (directory / "playbooks" / "overview.md").unlink()
    spec = load_spec(directory / "harness.yaml")
    with pytest.raises(SpecError, match="prompt not found"):
        load_playbooks(spec, directory)


# --------------------------------------------------------------------------- #
# Manager behaviour
# --------------------------------------------------------------------------- #
def test_entry_playbook_is_the_declared_one(tmp_path: Path):
    manager = _manager(_two_playbooks(tmp_path))
    manager.enter_initial()
    assert manager.current_name == "overview"


def test_entry_defaults_to_the_first_when_none_declared(tmp_path: Path):
    directory = _base_harness(tmp_path)
    construct.add_playbook(directory, name="first", description="F")
    construct.add_playbook(directory, name="second", description="S")
    manager = _manager(directory)
    manager.enter_initial()
    assert manager.current_name == "first"


def test_switching_narrows_and_widens_the_active_tools(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    spec = load_spec(directory / "harness.yaml")
    registry = build_registry(spec, directory)
    manager = PlaybookManager(load_playbooks(spec, directory), registry)

    manager.enter_initial()
    assert sorted(registry.active_names()) == ["read_data", "switch_playbook"]

    assert manager.switch("targeting").ok
    assert sorted(registry.active_names()) == [
        "propose",
        "read_data",
        "switch_playbook",
    ]


def test_switch_playbook_survives_every_narrowing(tmp_path: Path):
    """A mode the model cannot leave is a trap, not a mode."""
    directory = _base_harness(tmp_path)
    construct.add_playbook(
        directory, name="locked", description="L", tools=["read_data"], entry=True
    )
    construct.add_playbook(directory, name="other", description="O", tools=["propose"])
    spec = load_spec(directory / "harness.yaml")
    registry = build_registry(spec, directory)
    manager = PlaybookManager(load_playbooks(spec, directory), registry)
    manager.enter_initial()
    assert "switch_playbook" in registry.active_names()


def test_unknown_and_repeated_switches_are_refused(tmp_path: Path):
    manager = _manager(_two_playbooks(tmp_path))
    manager.enter_initial()
    assert not manager.switch("nope").ok
    assert "unknown playbook" in manager.switch("nope").reason
    assert not manager.switch("overview").ok


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #
def _with_hook(tmp_path: Path, filename: str, source: str, **kwargs) -> Path:
    directory = _base_harness(tmp_path)
    (directory / "hooks").mkdir(exist_ok=True)
    (directory / "hooks" / filename).write_text(source)
    construct.add_playbook(directory, name="start", description="S", entry=True)
    construct.add_playbook(directory, name="gated", description="G", **kwargs)
    return directory


def test_on_enter_can_inject_a_note(tmp_path: Path):
    directory = _with_hook(
        tmp_path, "enter.py", ENTER_HOOK, on_enter="hooks/enter.py:on_enter"
    )
    manager = _manager(directory)
    manager.enter_initial()
    outcome = manager.switch("gated")
    assert outcome.ok
    assert outcome.notes == ["warehouse refreshed for gated"]


def test_on_enter_can_refuse_entry(tmp_path: Path):
    directory = _with_hook(
        tmp_path, "enter.py", BLOCKING_ENTER, on_enter="hooks/enter.py:on_enter"
    )
    manager = _manager(directory)
    manager.enter_initial()
    outcome = manager.switch("gated")
    assert not outcome.ok
    assert "stale" in outcome.reason
    assert manager.current_name == "start"


def test_on_exit_gates_leaving_a_mode(tmp_path: Path):
    directory = _base_harness(tmp_path)
    (directory / "hooks").mkdir(exist_ok=True)
    (directory / "hooks" / "exit.py").write_text(BLOCKING_EXIT)
    construct.add_playbook(
        directory,
        name="targeting",
        description="T",
        entry=True,
        on_exit="hooks/exit.py:on_exit",
    )
    construct.add_playbook(directory, name="other", description="O")

    manager = _manager(directory)
    manager.enter_initial()
    outcome = manager.switch("other")
    assert not outcome.ok
    assert "proposed nothing" in outcome.reason
    assert manager.current_name == "targeting"


def test_a_stuck_exit_gate_is_force_released(tmp_path: Path):
    """A badly written gate must not trap the run forever."""
    directory = _base_harness(tmp_path)
    (directory / "hooks").mkdir(exist_ok=True)
    (directory / "hooks" / "exit.py").write_text(BLOCKING_EXIT)
    construct.add_playbook(
        directory,
        name="targeting",
        description="T",
        entry=True,
        on_exit="hooks/exit.py:on_exit",
    )
    construct.add_playbook(directory, name="other", description="O")

    spec = load_spec(directory / "harness.yaml")
    manager = PlaybookManager(
        load_playbooks(spec, directory), None, max_blocked_exits=3
    )
    manager.enter_initial()
    assert not manager.switch("other").ok
    assert not manager.switch("other").ok
    released = manager.switch("other")
    assert released.ok
    assert any("force-released" in note for note in released.notes)
    assert manager.current_name == "other"


def test_a_raising_hook_is_reported_not_fatal(tmp_path: Path):
    directory = _with_hook(
        tmp_path, "enter.py", RAISING_HOOK, on_enter="hooks/enter.py:on_enter"
    )
    manager = _manager(directory)
    manager.enter_initial()
    errors = []
    outcome = manager.switch(
        "gated", on_hook_error=lambda p, k, e: errors.append((p, k, str(e)))
    )
    assert outcome.ok  # a broken hook does not block the switch
    assert errors and errors[0][0] == "gated"


# --------------------------------------------------------------------------- #
# Loop integration
# --------------------------------------------------------------------------- #
def test_system_prompt_carries_the_index_and_the_current_fragment(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    (directory / "playbooks" / "overview.md").write_text("Start from segment KPIs.")
    info = runner.dry_run(directory, "go")

    assert "# Playbooks" in info["system"]
    assert "overview (current)" in info["system"]
    assert "Start from segment KPIs." in info["system"]
    # The entry playbook's narrowing is visible before any model call.
    assert sorted(t["name"] for t in info["tools"]) == ["read_data", "switch_playbook"]


def test_a_run_can_switch_mode_and_use_the_newly_active_tool(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response(
                "switch_playbook",
                {"name": "targeting", "reason": "the user asked to act"},
                call_id="c1",
            ),
            tool_response("propose", {"what": "win-back"}, call_id="c2"),
            text_response("Proposta pronta."),
        ]
    )
    result = runner.run_harness(directory, "act", provider=provider, literal_input=True)
    assert result.status == "success"

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    switches = [e for e in events if e["type"] == "playbook_switch"]
    assert [s["payload"]["to"] for s in switches] == ["overview", "targeting"]
    assert switches[1]["payload"]["ok"] is True


def test_a_tool_outside_the_current_mode_is_inactive(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    provider = FakeModelProvider(
        [
            tool_response("propose", {"what": "too soon"}, call_id="c1"),
            text_response("ok"),
        ]
    )
    result = runner.run_harness(directory, "act", provider=provider, literal_input=True)
    delivered = provider.calls[1]["messages"][-1]["content"][0]
    assert delivered["is_error"] is True
    assert "inactive" in delivered["content"]
    assert result.status == "success"


def test_a_refused_switch_reaches_the_model_as_a_tool_error(tmp_path: Path):
    directory = _with_hook(
        tmp_path, "enter.py", BLOCKING_ENTER, on_enter="hooks/enter.py:on_enter"
    )
    provider = FakeModelProvider(
        [
            tool_response("switch_playbook", {"name": "gated"}, call_id="c1"),
            text_response("resto qui"),
        ]
    )
    result = runner.run_harness(directory, "go", provider=provider, literal_input=True)
    delivered = provider.calls[1]["messages"][-1]["content"][0]
    assert delivered["is_error"] is True
    assert "stale" in delivered["content"]
    assert result.status == "success"


def test_playbook_validators_are_additive(tmp_path: Path):
    directory = _base_harness(tmp_path)
    (directory / "validators").mkdir(exist_ok=True)
    (directory / "validators" / "pb.py").write_text(PLAYBOOK_VALIDATOR)
    construct.add_playbook(directory, name="overview", description="O", entry=True)
    construct.add_playbook(
        directory,
        name="targeting",
        description="T",
        validators=[{"code": "validators/pb.py:check"}],
    )

    # In overview the mode validator does not apply.
    provider = FakeModelProvider([text_response("nessuna proposta qui")])
    assert runner.run_harness(
        directory, "go", provider=provider, literal_input=True
    ).status == "success"

    # After switching to targeting it does, and it fails a bad answer.
    provider = FakeModelProvider(
        [
            tool_response("switch_playbook", {"name": "targeting"}, call_id="c1"),
            text_response("niente"),
            text_response("ancora niente"),
            text_response("ancora niente di nuovo"),
        ]
    )
    result = runner.run_harness(directory, "go", provider=provider, literal_input=True)
    assert result.status == "verify_failed"
    assert any("proposta" in v.feedback for v in result.verdicts)


# --------------------------------------------------------------------------- #
# Evidence: the Hive measures per playbook
# --------------------------------------------------------------------------- #
def test_hive_reports_success_per_playbook(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    hive_path = tmp_path / "hive.db"

    # One run that stays in overview and succeeds.
    runner.run_harness(
        directory,
        "look",
        provider=FakeModelProvider([text_response("ok")]),
        literal_input=True,
        hive_path=hive_path,
    )
    # One run that switches to targeting and stalls out.
    construct.set_value(directory, "loop.max_turns", 2)
    runner.run_harness(
        directory,
        "act",
        provider=FakeModelProvider(
            [
                tool_response("switch_playbook", {"name": "targeting"}, call_id="c1"),
                tool_response("propose", {"what": "x"}, call_id="c2"),
            ]
        ),
        literal_input=True,
        hive_path=hive_path,
    )

    with Hive(hive_path) as hive:
        key = load_spec(directory / "harness.yaml").identity
        stats = {row["playbook"]: row for row in hive.playbook_stats(key)}

    assert stats["overview"]["runs"] == 2  # both runs started here
    assert stats["targeting"]["runs"] == 1
    assert stats["targeting"]["success_rate"] == 0.0
    assert stats["overview"]["success_rate"] == 0.5


def test_refused_switches_are_counted(tmp_path: Path):
    directory = _with_hook(
        tmp_path, "enter.py", BLOCKING_ENTER, on_enter="hooks/enter.py:on_enter"
    )
    hive_path = tmp_path / "hive.db"
    runner.run_harness(
        directory,
        "go",
        provider=FakeModelProvider(
            [
                tool_response("switch_playbook", {"name": "gated"}, call_id="c1"),
                text_response("ok"),
            ]
        ),
        literal_input=True,
        hive_path=hive_path,
    )
    with Hive(hive_path) as hive:
        key = load_spec(directory / "harness.yaml").identity
        stats = {row["playbook"]: row for row in hive.playbook_stats(key)}
    assert stats["gated"]["refusals"] == 1
    assert stats["gated"]["runs"] == 0


# --------------------------------------------------------------------------- #
# The freeze boundary
# --------------------------------------------------------------------------- #
def _gate(directory: Path, path: str, value):
    from hiveloom.evolve.evolver import MutationProposal, YamlChange, gate

    spec = load_spec(directory / "harness.yaml")
    proposal = MutationProposal(
        rationale="r", yaml_changes=[YamlChange(path=path, value=value)]
    )
    return gate(spec, proposal)


def test_playbook_prompts_are_evolvable(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    construct.set_value(directory, "evolution.mutable", ["playbooks", "system_prompt"])
    result = _gate(directory, "playbooks.0.prompt", "playbooks/overview.md")
    assert [c.path for c in result.accepted] == ["playbooks.0.prompt"]


def test_playbook_code_hooks_are_never_evolvable(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    construct.set_value(directory, "evolution.mutable", ["playbooks"])
    result = _gate(directory, "playbooks.0.on_enter", "hooks/evil.py:run")
    assert not result.accepted
    assert "frozen from evolution" in result.rejected[0]["reason"]


def test_a_hook_cannot_be_smuggled_in_by_rewriting_the_list(tmp_path: Path):
    directory = _two_playbooks(tmp_path)
    construct.set_value(directory, "evolution.mutable", ["playbooks"])
    result = _gate(
        directory,
        "playbooks",
        [{"name": "overview", "description": "O", "on_enter": "hooks/evil.py:run"}],
    )
    assert not result.accepted
    assert "frozen from evolution" in result.rejected[0]["reason"]


# --------------------------------------------------------------------------- #
# Operator-driven playbook switch
# --------------------------------------------------------------------------- #
def test_an_operator_can_switch_the_playbook_mid_run(tmp_path: Path):
    from hiveloom.loop.control import RunControl

    directory = _two_playbooks(tmp_path)
    control = RunControl()
    control.switch_playbook("targeting", reason="operator knows better")

    provider = FakeModelProvider(
        [tool_response("read_data", {"query": "q"}, call_id="c1"), text_response("done")]
    )
    result = runner.run_harness(directory, "task", provider=provider, control=control)

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    switch = next(
        e for e in events
        if e["type"] == "playbook_switch" and e["payload"].get("source") == "operator"
    )
    assert switch["payload"]["from"] == "overview"
    assert switch["payload"]["to"] == "targeting"
    assert switch["payload"]["ok"] is True


def test_the_model_is_told_when_an_operator_moves_it(tmp_path: Path):
    """A mode change the model cannot see is a mode change it will misread."""
    from hiveloom.loop.control import RunControl

    directory = _two_playbooks(tmp_path)
    control = RunControl()
    control.switch_playbook("targeting")

    provider = FakeModelProvider(
        [tool_response("read_data", {"query": "q"}, call_id="c1"), text_response("done")]
    )
    runner.run_harness(directory, "task", provider=provider, control=control, ingest=False)

    sent = json.dumps(provider.calls[-1]["messages"])
    assert "Operator switched you into playbook 'targeting'" in sent


def test_an_exit_gate_refuses_an_operator_switch_too(tmp_path: Path):
    """An operator switch is a request through the same door, not a way around it."""
    from hiveloom.loop.control import RunControl

    directory = _base_harness(tmp_path)
    (directory / "hooks").mkdir(exist_ok=True)
    (directory / "hooks" / "exit.py").write_text(BLOCKING_EXIT)
    # The gated playbook is the entry, so *leaving* it is what gets refused.
    construct.add_playbook(
        directory,
        name="gated",
        description="G",
        on_exit="hooks/exit.py:on_exit",
        entry=True,
    )
    construct.add_playbook(directory, name="start", description="S")

    control = RunControl()
    control.switch_playbook("start")
    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(directory, "task", provider=provider, control=control)

    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    refused = next(
        e for e in events
        if e["type"] == "playbook_switch" and e["payload"].get("source") == "operator"
    )
    assert refused["payload"]["ok"] is False
    assert "proposed nothing" in refused["payload"]["refused_reason"]


def test_an_operator_switch_on_a_harness_without_playbooks_is_traced(tmp_path: Path):
    from hiveloom.loop.control import RunControl

    directory = _base_harness(tmp_path)
    control = RunControl()
    control.switch_playbook("nope")

    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(directory, "task", provider=provider, ingest=False)
    assert result.status == "success"

    control2 = RunControl()
    control2.switch_playbook("nope")
    result2 = runner.run_harness(
        directory,
        "task",
        provider=FakeModelProvider([text_response("done")]),
        control=control2,
        ingest=False,
    )
    events = [json.loads(line) for line in Path(result2.trace_path).read_text().splitlines()]
    failed = next(e for e in events if e["type"] == "playbook_switch_failed")
    assert "declares no playbooks" in failed["payload"]["refused_reason"]
