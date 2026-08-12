"""Per-run context injection into code tools and validators."""

from __future__ import annotations

import json
from pathlib import Path

from hiveloom import construct, runner
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response

CONTEXT_TOOL = '''
import json

from hiveloom.tools import tool


@tool(description="Report what the runtime injected.")
def peek(label: str, run_context) -> str:
    return json.dumps({
        "label": label,
        "dsn": run_context["context"].get("database_url"),
        "keys": sorted(run_context),
        "input": run_context["input"],
    })
'''

ACCUMULATOR_TOOL = '''
from hiveloom.tools import tool


@tool(description="Record a value into run-scoped state.")
def record(value: str, run_context) -> str:
    run_context["context"].setdefault("seen", []).append(value)
    return f"recorded {value}"
'''

PLAIN_TOOL = '''
from hiveloom.tools import tool


@tool(description="Needs nothing from the runtime.")
def plain(value: str) -> str:
    return f"got {value}"
'''

VALIDATOR = '''
def check(run_output, run_context):
    dsn = run_context["context"].get("database_url", "")
    return {"passed": dsn.startswith("postgresql://"), "feedback": f"dsn was {dsn!r}"}
'''


def _harness(tmp_path: Path, filename: str, source: str, func: str) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="ctx-harness", task="Use injected context.")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / filename).write_text(source)
    construct.add_tool(
        directory, code=f"tools/{filename}:{func}", description=f"The {func} tool."
    )
    return directory


# --------------------------------------------------------------------------- #
# Injection
# --------------------------------------------------------------------------- #
def test_tool_receives_caller_context(tmp_path: Path):
    harness = _harness(tmp_path, "peek.py", CONTEXT_TOOL, "peek")
    provider = FakeModelProvider(
        [tool_response("peek", {"label": "a"}, call_id="c1"), text_response("ok")]
    )
    runner.run_harness(
        harness,
        "go",
        provider=provider,
        literal_input=True,
        context={"database_url": "postgresql://agent@lake/gimme5"},
    )

    reported = json.loads(
        json.loads(json.dumps(provider.calls[1]["messages"][-1]["content"]))[0]["content"]
    )
    assert reported["dsn"] == "postgresql://agent@lake/gimme5"
    assert reported["input"] == "go"
    assert set(reported["keys"]) == {
        "input",
        "harness_dir",
        "run_id",
        "context",
        "artifacts",
    }


def test_run_context_is_hidden_from_the_model_schema(tmp_path: Path):
    harness = _harness(tmp_path, "peek.py", CONTEXT_TOOL, "peek")
    info = runner.dry_run(harness, "go")
    schema = next(t for t in info["tools"] if t["name"] == "peek")["input_schema"]
    assert "label" in schema["properties"]
    assert "run_context" not in schema["properties"]


def test_model_cannot_forge_the_run_context(tmp_path: Path):
    """A model-supplied key of the same name is replaced, not honored."""
    harness = _harness(tmp_path, "peek.py", CONTEXT_TOOL, "peek")
    provider = FakeModelProvider(
        [
            tool_response(
                "peek",
                {"label": "a", "run_context": {"context": {"database_url": "EVIL"}}},
                call_id="c1",
            ),
            text_response("ok"),
        ]
    )
    runner.run_harness(
        harness,
        "go",
        provider=provider,
        literal_input=True,
        context={"database_url": "postgresql://real"},
    )
    reported = json.loads(
        json.loads(json.dumps(provider.calls[1]["messages"][-1]["content"]))[0]["content"]
    )
    assert reported["dsn"] == "postgresql://real"


def test_tools_that_do_not_ask_for_context_are_unaffected(tmp_path: Path):
    harness = _harness(tmp_path, "plain.py", PLAIN_TOOL, "plain")
    provider = FakeModelProvider(
        [tool_response("plain", {"value": "x"}, call_id="c1"), text_response("ok")]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, context={"a": 1}
    )
    assert result.status == "success"


def test_context_defaults_to_an_empty_dict(tmp_path: Path):
    harness = _harness(tmp_path, "peek.py", CONTEXT_TOOL, "peek")
    provider = FakeModelProvider(
        [tool_response("peek", {"label": "a"}, call_id="c1"), text_response("ok")]
    )
    result = runner.run_harness(harness, "go", provider=provider, literal_input=True)
    assert result.status == "success"


# --------------------------------------------------------------------------- #
# Shared-by-reference semantics
# --------------------------------------------------------------------------- #
def test_context_is_shared_so_tools_can_accumulate_run_state(tmp_path: Path):
    harness = _harness(tmp_path, "rec.py", ACCUMULATOR_TOOL, "record")
    provider = FakeModelProvider(
        [
            tool_response("record", {"value": "uno"}, call_id="c1"),
            tool_response("record", {"value": "due"}, call_id="c2"),
            text_response("ok"),
        ]
    )
    context: dict = {}
    runner.run_harness(
        harness, "go", provider=provider, literal_input=True, context=context
    )
    assert context["seen"] == ["uno", "due"]


def test_context_is_not_written_to_the_trace(tmp_path: Path):
    """Secrets passed as context must not land in persisted run memory."""
    harness = _harness(tmp_path, "plain.py", PLAIN_TOOL, "plain")
    provider = FakeModelProvider(
        [tool_response("plain", {"value": "x"}, call_id="c1"), text_response("ok")]
    )
    result = runner.run_harness(
        harness,
        "go",
        provider=provider,
        literal_input=True,
        context={"database_url": "postgresql://user:hunter2@lake/db"},
    )
    assert "hunter2" not in Path(result.trace_path).read_text()


# --------------------------------------------------------------------------- #
# Validators see the same context
# --------------------------------------------------------------------------- #
def test_validators_receive_the_caller_context(tmp_path: Path):
    harness = _harness(tmp_path, "plain.py", PLAIN_TOOL, "plain")
    (harness / "validators").mkdir(exist_ok=True)
    (harness / "validators" / "dsn.py").write_text(VALIDATOR)
    construct.add_validator(harness, code="validators/dsn.py:check")

    provider = FakeModelProvider([text_response("done")])
    ok = runner.run_harness(
        harness,
        "go",
        provider=provider,
        literal_input=True,
        context={"database_url": "postgresql://real"},
    )
    assert ok.status == "success"

    provider = FakeModelProvider([text_response("done"), text_response("done")])
    bad = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, context={}
    )
    assert bad.status == "verify_failed"
