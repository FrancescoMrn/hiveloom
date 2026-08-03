"""Tests for ``loop.tool_execution: parallel``."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.models.provider import ModelResponse, ToolCall, Usage
from hiveloom.spec.schema import HarnessSpec

# Two tools that rendezvous: each drops a marker file, then polls for the
# other's. Resolvable only when both run concurrently — under sequential
# execution the first side times out and returns "timeout" instead of "ok".
# Filesystem markers (not module-level Events) because each code ref imports
# the file as a separate module instance.
_RENDEZVOUS_TOOLS = """
import time
from pathlib import Path

from hiveloom.tools.decorators import tool

_DIR = Path(__file__).resolve().parent


def _rendezvous(mine: str, theirs: str) -> str:
    (_DIR / mine).write_text("here")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (_DIR / theirs).exists():
            return "ok"
        time.sleep(0.01)
    return "timeout"


@tool(description="side a of the rendezvous")
def side_a(note: str = "") -> str:
    return _rendezvous("a.flag", "b.flag")


@tool(description="side b of the rendezvous")
def side_b(note: str = "") -> str:
    return _rendezvous("b.flag", "a.flag")
"""


def _two_call_response() -> ModelResponse:
    calls = [
        ToolCall(id="c_a", name="side_a", input={}),
        ToolCall(id="c_b", name="side_b", input={}),
    ]
    return ModelResponse(
        text="",
        tool_calls=calls,
        stop_reason="tool_use",
        usage=Usage(input_tokens=50, output_tokens=10),
        content_blocks=[
            {"type": "tool_use", "id": c.id, "name": c.name, "input": {}} for c in calls
        ],
    )


def _parallel_harness(harness_dir: Path) -> None:
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_field(harness_dir, "loop.tool_execution", "parallel")
    (harness_dir / "tools").mkdir(exist_ok=True)
    (harness_dir / "tools" / "rendezvous.py").write_text(_RENDEZVOUS_TOOLS, encoding="utf-8")
    construct.add_tool(harness_dir, code="tools/rendezvous.py:side_a", description="side a")
    construct.add_tool(harness_dir, code="tools/rendezvous.py:side_b", description="side b")


def test_schema_defaults_to_sequential():
    spec = HarnessSpec.model_validate(
        {"name": "t", "description": "d", "system_prompt": "sp"}
    )
    assert spec.loop.tool_execution == "sequential"


def test_parallel_calls_run_concurrently_and_results_keep_source_order(harness_dir: Path):
    _parallel_harness(harness_dir)
    provider = FakeModelProvider([_two_call_response(), text_response("done")])

    result = runner.run_harness(harness_dir, "go", provider=provider)

    assert result.status == "success"
    # The rendezvous only resolves when both tools were in flight at once.
    blocks = [
        b
        for message in provider.calls[-1]["messages"]
        if isinstance(message.get("content"), list)
        for b in message["content"]
        if b.get("type") == "tool_result"
    ]
    assert [b["content"] for b in blocks] == ["ok", "ok"]
    # Results are appended in source order regardless of completion order.
    assert [b["tool_use_id"] for b in blocks] == ["c_a", "c_b"]


def test_parallel_blocked_call_still_runs_the_others(harness_dir: Path):
    _parallel_harness(harness_dir)
    (harness_dir / "hooks").mkdir(exist_ok=True)
    (harness_dir / "hooks" / "block_b.py").write_text(
        "def block_b(event):\n"
        "    if event['name'] == 'side_b':\n"
        "        return {'block': True, 'reason': 'b is audited'}\n",
        encoding="utf-8",
    )
    construct.add_hook(harness_dir, on="before_tool_call", code="hooks/block_b.py:block_b")

    provider = FakeModelProvider([_two_call_response(), text_response("done")])
    result = runner.run_harness(harness_dir, "go", provider=provider)

    assert result.status == "success"
    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    tool_results = [e["payload"] for e in events if e["type"] == "tool_result"]
    # side_a executed alone: with the rendezvous unpaired it times out, which
    # proves side_b truly never ran.
    assert [r["name"] for r in tool_results] == ["side_a"]
    assert tool_results[0]["content"] == "timeout"
    blocked = [
        b
        for message in provider.calls[-1]["messages"]
        if isinstance(message.get("content"), list)
        for b in message["content"]
        if b.get("type") == "tool_result" and b.get("is_error")
    ]
    assert any("b is audited" in b["content"] for b in blocked)


def test_parallel_trace_seq_stays_strictly_increasing(harness_dir: Path):
    _parallel_harness(harness_dir)
    provider = FakeModelProvider([_two_call_response(), text_response("done")])
    result = runner.run_harness(harness_dir, "go", provider=provider)

    seqs = [
        json.loads(line)["seq"] for line in Path(result.trace_path).read_text().splitlines()
    ]
    assert seqs == sorted(set(seqs))
