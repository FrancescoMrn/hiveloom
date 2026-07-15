"""Tests for the open loop (M7): events, policies, compaction, tools, skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, ext, runner
from hiveloom.context.manager import CompactionMethod, ContextManager
from hiveloom.errors import SpecError
from hiveloom.events import EventBus, build_event_bus
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import load_spec, validate_harness
from hiveloom.spec.schema import CompactionConfig, HarnessSpec, LoopConfig
from hiveloom.tools.registry import Tool, ToolResult, build_registry


def _events(trace_path: str) -> list[dict]:
    return [json.loads(line) for line in Path(trace_path).read_text().splitlines()]


def _no_verify(harness_dir: Path) -> None:
    construct.set_field(harness_dir, "loop.require_verification", "false")


def _tool_result_blocks(provider: FakeModelProvider) -> list[dict]:
    """All tool_result blocks in the (shared, mutated) message history."""
    blocks: list[dict] = []
    for message in provider.calls[-1]["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            blocks += [b for b in content if b.get("type") == "tool_result"]
    return blocks


# --------------------------------------------------------------------------- #
# Event hooks (spec `hooks:` section)
# --------------------------------------------------------------------------- #
_BLOCK_HOOK = """
def block_writes(event):
    if event["name"] == "file_write":
        return {"block": True, "reason": "writes are audited"}
"""

_PATCH_ARGS_HOOK = """
def add_prefix(event):
    if event["name"] == "file_read":
        return {"input": {"path": "notes.txt"}}
"""

_PATCH_RESULT_HOOK = """
def redact(event):
    return {"content": event["content"].replace("secret", "[X]")}
"""

_RAISING_HOOK = """
def boom(event):
    raise RuntimeError("hook exploded")
"""

_UNFENCE_HOOK = """
def unfence(event):
    output = event["output"].strip()
    if output.startswith("```json") and output.endswith("```"):
        return {"output": output.removeprefix("```json").removesuffix("```").strip()}
"""


def _add_code_hook(harness_dir: Path, on: str, source: str, func: str) -> None:
    (harness_dir / "hooks").mkdir(exist_ok=True)
    (harness_dir / "hooks" / f"{func}.py").write_text(source, encoding="utf-8")
    construct.add_hook(harness_dir, on=on, code=f"hooks/{func}.py:{func}")


def test_hook_blocks_tool_call(harness_dir: Path):
    _no_verify(harness_dir)
    construct.add_tool(harness_dir, builtin="file_write")
    _add_code_hook(harness_dir, "before_tool_call", _BLOCK_HOOK, "block_writes")

    provider = FakeModelProvider(
        [tool_response("file_write", {"path": "x.txt", "content": "hi"}), text_response("done")]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert not (harness_dir / "x.txt").exists()
    types = [e["type"] for e in _events(result.trace_path)]
    assert "hook_triggered" in types
    # The model saw the block as an error tool result.
    blocked = _tool_result_blocks(provider)[0]
    assert blocked["is_error"] is True
    assert "writes are audited" in blocked["content"]


def test_hook_patches_tool_args(harness_dir: Path):
    _no_verify(harness_dir)
    construct.add_tool(harness_dir, builtin="file_read")
    (harness_dir / "notes.txt").write_text("real content", encoding="utf-8")
    _add_code_hook(harness_dir, "before_tool_call", _PATCH_ARGS_HOOK, "add_prefix")

    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "@notes.txt"}), text_response("done")]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert _tool_result_blocks(provider)[0]["content"] == "real content"


def test_hook_patches_tool_result(harness_dir: Path):
    _no_verify(harness_dir)
    construct.add_tool(harness_dir, builtin="file_read")
    (harness_dir / "notes.txt").write_text("a secret value", encoding="utf-8")
    _add_code_hook(harness_dir, "after_tool_call", _PATCH_RESULT_HOOK, "redact")

    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "notes.txt"}), text_response("done")]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert _tool_result_blocks(provider)[0]["content"] == "a [X] value"


def test_raising_hook_is_logged_and_skipped(harness_dir: Path):
    _no_verify(harness_dir)
    _add_code_hook(harness_dir, "run_started", _RAISING_HOOK, "boom")

    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    types = [e["type"] for e in _events(result.trace_path)]
    assert "hook_error" in types


def test_before_verification_hook_normalizes_output(harness_dir: Path):
    _add_code_hook(harness_dir, "before_verification", _UNFENCE_HOOK, "unfence")
    construct.add_validator(harness_dir, builtin="regex_match", pattern='^\\{')
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([text_response('```json\n{"ok": true}\n```')])
    )
    assert result.status == "success"
    assert result.output == '{"ok": true}'
    assert "hook_triggered" in [e["type"] for e in _events(result.trace_path)]


def test_builtin_strip_json_fence_hook_normalizes_output(harness_dir: Path):
    construct.add_hook(
        harness_dir, on="before_verification", builtin="strip_json_fence"
    )
    construct.add_validator(harness_dir, builtin="regex_match", pattern='^\\{')
    result = runner.run_harness(
        harness_dir, "go", provider=FakeModelProvider([text_response('```\n{"ok": true}\n```')])
    )
    assert result.status == "success"
    assert result.output == '{"ok": true}'


def test_invalid_event_name_rejected(harness_dir: Path):
    with pytest.raises(SpecError, match="unknown event"):
        construct.add_hook(harness_dir, on="not_an_event", code="hooks/h.py:h")


def test_context_assemble_hook_transforms_messages():
    bus = EventBus()
    bus.subscribe(
        "context_assemble",
        "upper",
        lambda e: {"messages": e["messages"] + [{"role": "user", "content": "extra"}]},
    )
    spec = HarnessSpec(name="h", description="d", system_prompt="s")
    cm = ContextManager(spec, FakeModelProvider([]), events=bus)
    cm.add_user("hi")
    _, messages = cm.assemble()
    assert messages[-1]["content"] == "extra"


def test_ambient_extension_hook_runs(harness_dir: Path):
    _no_verify(harness_dir)
    seen: list[dict] = []
    api = ext.ExtensionAPI(source="test:ambient")

    @api.on("run_finished")
    def observe(event):
        seen.append(event)

    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert seen and seen[0]["status"] == "success"


def test_registered_builtin_hook_via_spec(harness_dir: Path):
    _no_verify(harness_dir)
    calls: list[str] = []
    api = ext.ExtensionAPI(source="test:hooks")
    api.register_hook(
        "audit_log",
        lambda params, ctx: lambda event: calls.append(event["name"]),
        description="Record every tool call.",
    )
    construct.add_tool(harness_dir, builtin="file_read")
    (harness_dir / "n.txt").write_text("x", encoding="utf-8")
    construct.add_hook(harness_dir, on="before_tool_call", builtin="audit_log")

    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "n.txt"}), text_response("done")]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert calls == ["file_read"]


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #
def test_extension_policy_with_nudge(harness_dir: Path):
    from hiveloom.loop.policies import LoopPolicy

    class ReflexionPolicy(LoopPolicy):
        name = "reflexion"

        def __init__(self):
            self._critiqued = False

        def wants_continue(self, loop, response):
            if not self._critiqued:
                self._critiqued = True
                return "Critique your answer, then give a final version."
            return None

    api = ext.ExtensionAPI(source="test:policy")
    api.register_policy(
        "reflexion", lambda p, c: ReflexionPolicy(), description="One critique pass."
    )

    _no_verify(harness_dir)
    construct.set_field(harness_dir, "loop.policy", "reflexion")
    provider = FakeModelProvider([text_response("draft"), text_response("final")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert result.output == "final"
    assert len(provider.calls) == 2
    assert any(
        isinstance(m.get("content"), str) and "Critique" in m["content"]
        for m in provider.calls[1]["messages"]
    )


def test_unknown_policy_rejected():
    with pytest.raises(ValueError, match="unknown loop policy"):
        LoopConfig(policy="who-dis")


def test_unknown_compaction_method_rejected():
    with pytest.raises(ValueError, match="unknown compaction method"):
        CompactionConfig(method="who-dis")


# --------------------------------------------------------------------------- #
# Compaction
# --------------------------------------------------------------------------- #
def test_extension_compaction_method_used():
    class KeepLastOnly(CompactionMethod):
        name = "keep_last"

        def compact(self, manager, budget):
            manager.messages = [manager.messages[0], manager.messages[-1]]

    api = ext.ExtensionAPI(source="test:compaction")
    api.register_compaction(
        "keep_last", lambda p, c: KeepLastOnly(), description="Drop the middle."
    )

    spec = HarnessSpec(
        name="h", description="d", system_prompt="s",
        context={"max_input_tokens": 50, "compaction": {"trigger_at_pct": 10,
                                                        "method": "keep_last"}},
    )
    cm = ContextManager(spec, FakeModelProvider([]))
    for i in range(6):
        cm.add_user("long message " * 20 + str(i))
    assert cm.maybe_compact()
    assert len(cm.messages) == 2


def test_before_compaction_hook_can_cancel():
    bus = EventBus()
    bus.subscribe("before_compaction", "never", lambda e: {"cancel": True})
    spec = HarnessSpec(
        name="h", description="d", system_prompt="s",
        context={"max_input_tokens": 50, "compaction": {"trigger_at_pct": 10}},
    )
    cm = ContextManager(spec, FakeModelProvider([]), events=bus)
    for i in range(6):
        cm.add_user("long message " * 20 + str(i))
    before = len(cm.messages)
    assert not cm.maybe_compact()
    assert len(cm.messages) == before


def test_before_compaction_hook_custom_summary():
    bus = EventBus()
    bus.subscribe("before_compaction", "mine", lambda e: {"summary": "my custom notes"})
    spec = HarnessSpec(
        name="h", description="d", system_prompt="s",
        context={"max_input_tokens": 50, "compaction": {"trigger_at_pct": 10}},
    )
    provider = FakeModelProvider([])  # never called: the hook supplies the summary
    cm = ContextManager(spec, provider, events=bus)
    for i in range(6):
        cm.add_user("long message " * 20 + str(i))
    assert cm.maybe_compact()
    assert "my custom notes" in cm.messages[1]["content"]
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# Tool ergonomics
# --------------------------------------------------------------------------- #
class _SubmitTool(Tool):
    def __init__(self):
        self.name = "submit"
        self.description = "Submit the final answer."
        self.tags = []
        self.input_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }

    def run(self, answer: str = "", **_):
        return ToolResult(content=answer, terminate=True)


def test_terminate_result_skips_final_model_call(harness_dir: Path):
    _no_verify(harness_dir)
    api = ext.ExtensionAPI(source="test:submit")
    api.register_tool("submit", lambda p, c: _SubmitTool(), description="Submit final answer.")
    construct.add_tool(harness_dir, builtin="submit")

    provider = FakeModelProvider([tool_response("submit", {"answer": "42"})])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert result.output == "42"
    assert len(provider.calls) == 1  # no follow-up model call


def test_deferred_tools_and_search_tools(harness_dir: Path):
    _no_verify(harness_dir)
    construct.add_tool(harness_dir, builtin="file_read")
    construct.set_field(harness_dir, "tools", '[{builtin: file_read, deferred: true}]')
    (harness_dir / "n.txt").write_text("found", encoding="utf-8")

    spec = load_spec(harness_dir)
    registry = build_registry(spec, harness_dir)
    payload_names = [t["name"] for t in registry.anthropic_payload()]
    assert payload_names == ["search_tools"]

    provider = FakeModelProvider(
        [
            tool_response("search_tools", {"query": "read file"}, call_id="c1"),
            tool_response("file_read", {"path": "n.txt"}, call_id="c2"),
            text_response("done"),
        ]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    # After activation the file_read tool is in the payload of the next call.
    assert "file_read" in [t["name"] for t in provider.calls[1]["tools"]]


def test_prepare_normalizes_args():
    class AtStripper(Tool):
        name = "reader"
        description = "d"
        tags: list = []
        input_schema = {"type": "object", "properties": {}}

        def prepare(self, kwargs):
            if isinstance(kwargs.get("path"), str):
                kwargs["path"] = kwargs["path"].lstrip("@")
            return kwargs

        def run(self, path: str = "", **_):
            return path

    from hiveloom.models.provider import ToolCall
    from hiveloom.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(AtStripper())
    result = registry.dispatch(ToolCall(id="c", name="reader", input={"path": "@x.txt"}))
    assert result.content == "x.txt"


def test_streaming_tool_emits_tool_update(harness_dir: Path):
    class Streamer(Tool):
        name = "streamer"
        description = "d"
        tags: list = []
        input_schema = {"type": "object", "properties": {}}
        supports_updates = True

        def run(self, **_):
            return "done"

        def run_with_updates(self, kwargs, on_update):
            on_update("halfway")
            return "done"

    _no_verify(harness_dir)
    api = ext.ExtensionAPI(source="test:streamer")
    api.register_tool("streamer", lambda p, c: Streamer(), description="Streams progress.")
    construct.add_tool(harness_dir, builtin="streamer")

    provider = FakeModelProvider([tool_response("streamer", {}), text_response("ok")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    updates = [e for e in _events(result.trace_path) if e["type"] == "tool_update"]
    assert updates and updates[0]["payload"]["content"] == "halfway"


def test_tool_guidelines_in_system_prompt(tmp_path: Path):
    class Guided(Tool):
        name = "guided"
        description = "d"
        tags: list = []
        input_schema = {"type": "object", "properties": {}}
        guidelines = "Call guided at most once per run."

        def run(self, **_):
            return "x"

    from hiveloom.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(Guided())
    spec = HarnessSpec(name="h", description="d", system_prompt="base prompt")
    cm = ContextManager(spec, FakeModelProvider([]), registry=registry)
    assert "Call guided at most once per run." in cm.system()


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
def test_add_skill_scaffolds_and_validates(harness_dir: Path):
    construct.add_skill(harness_dir, name="pdf-report", description="Make a PDF report.")
    skill_file = harness_dir / "skills" / "pdf-report" / "SKILL.md"
    assert skill_file.exists()
    spec = validate_harness(harness_dir)
    assert spec.skills == ["pdf-report"]


def test_skill_index_in_system_prompt(harness_dir: Path):
    _no_verify(harness_dir)
    construct.add_skill(harness_dir, name="pdf-report", description="Make a PDF report.")
    provider = FakeModelProvider([text_response("done")])
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    system = provider.calls[0]["system"]
    assert "# Skills" in system
    assert "pdf-report" in system and "Make a PDF report." in system


def test_missing_skill_file_is_spec_error(harness_dir: Path):
    construct.add_skill(harness_dir, name="s1", description="D.")
    import shutil

    shutil.rmtree(harness_dir / "skills" / "s1")
    with pytest.raises(SpecError, match="skill 's1' not found"):
        validate_harness(harness_dir)


def test_remove_skill_by_name(harness_dir: Path):
    construct.add_skill(harness_dir, name="s1", description="D.")
    construct.remove_item(harness_dir, "s1")
    assert load_spec(harness_dir).skills == []


def test_build_event_bus_orders_ambient_then_spec(harness_dir: Path):
    api = ext.ExtensionAPI(source="test:order")
    api.on("run_started")(lambda e: None)
    _add_code_hook(harness_dir, "run_started", "def h(event):\n    return None\n", "h")
    spec = load_spec(harness_dir)
    bus = build_event_bus(spec, harness_dir)
    assert bus.has_handlers("run_started")
    names = [s.name for s in bus._subs["run_started"]]
    assert names[0].startswith("test:order") and names[1].startswith("hooks/")
