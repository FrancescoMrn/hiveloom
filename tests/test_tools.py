"""Tests for the tool registry and builtin tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from typing_extensions import TypedDict

from hiveloom.models.provider import ToolCall
from hiveloom.spec.schema import BuiltinToolRef, ConfinementConfig, HarnessSpec
from hiveloom.tools.builtin import (
    FileReadTool,
    FileWriteTool,
    HttpGetTool,
    ShellTool,
    _validate_public_http_url,
    safe_path,
)
from hiveloom.tools.registry import (
    FunctionTool,
    ToolError,
    ToolRegistry,
    build_registry,
    schema_from_function,
)


def test_file_write_then_read(tmp_path: Path):
    ToolWrite = FileWriteTool(tmp_path)
    ToolRead = FileReadTool(tmp_path)
    ToolWrite.run(path="a.txt", content="hello")
    assert ToolRead.run(path="a.txt") == "hello"


def test_file_read_rejects_path_traversal(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(FileReadTool(tmp_path))
    result = registry.dispatch(ToolCall(id="1", name="file_read", input={"path": "../secret"}))
    assert result.is_error
    assert "escapes" in result.content


# --------------------------------------------------------------------------- #
# file_read/file_write refuse harness-sensitive paths (auth store, .env)
# --------------------------------------------------------------------------- #
def test_file_read_refuses_authorized_keys_json(tmp_path: Path):
    (tmp_path / ".hiveloom").mkdir()
    (tmp_path / ".hiveloom" / "authorized_keys.json").write_text('{"keys": []}')
    with pytest.raises(ToolError):
        FileReadTool(tmp_path).run(path=".hiveloom/authorized_keys.json")


def test_file_read_refuses_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=super-secret-value\n")
    with pytest.raises(ToolError):
        FileReadTool(tmp_path).run(path=".env")


def test_file_write_refuses_authorized_keys_json(tmp_path: Path):
    """The write-side risk: a model must not be able to clobber (or plant
    keys into) the auth store either.
    """
    (tmp_path / ".hiveloom").mkdir()
    (tmp_path / ".hiveloom" / "authorized_keys.json").write_text('{"keys": []}')
    with pytest.raises(ToolError):
        FileWriteTool(tmp_path).run(path=".hiveloom/authorized_keys.json", content="{}")


def test_file_read_refuses_case_variant_dotenv(tmp_path: Path):
    """Case-insensitive filesystems (macOS APFS, most Windows filesystems)
    don't correct a caller's casing to the on-disk name — `.ENV` must be
    treated identically to `.env`.
    """
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=super-secret-value\n")
    with pytest.raises(ToolError):
        FileReadTool(tmp_path).run(path=".ENV")


def test_file_read_refuses_case_variant_hiveloom_dir(tmp_path: Path):
    (tmp_path / ".hiveloom").mkdir()
    (tmp_path / ".hiveloom" / "authorized_keys.json").write_text('{"keys": []}')
    with pytest.raises(ToolError):
        FileReadTool(tmp_path).run(path=".HIVELOOM/authorized_keys.json")


def test_file_read_still_allows_env_example(tmp_path: Path):
    """The checked-in template is explicitly exempt — only real `.env*`
    credential files are refused.
    """
    (tmp_path / ".env.example").write_text("ANTHROPIC_API_KEY=\n")
    assert FileReadTool(tmp_path).run(path=".env.example") == "ANTHROPIC_API_KEY=\n"


def test_file_read_still_allows_ordinary_harness_files(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("ordinary data")
    assert FileReadTool(tmp_path).run(path="notes.txt") == "ordinary data"


def test_file_read_refuses_configured_trace_dir(tmp_path: Path):
    """Fix-round-3 regression: a reconfigured (non-default) trace directory
    must be refused via file_read too, not just via the HTTP control
    plane's input_file.
    """
    (tmp_path / "run_logs").mkdir()
    (tmp_path / "run_logs" / "run_x.jsonl").write_text('{"type": "run_started"}\n')
    tool = FileReadTool(tmp_path, trace_dir=Path("run_logs"))
    with pytest.raises(ToolError):
        tool.run(path="run_logs/run_x.jsonl")


def test_file_write_refuses_configured_trace_dir(tmp_path: Path):
    (tmp_path / "run_logs").mkdir()
    tool = FileWriteTool(tmp_path, trace_dir=Path("run_logs"))
    with pytest.raises(ToolError):
        tool.run(path="run_logs/run_x.jsonl", content="forged trace entry")


def test_file_tools_consume_the_runtime_private_path_resolver(
    tmp_path: Path, monkeypatch
):
    runtime_home = tmp_path / "nonstandard-runtime-home"
    runtime_home.mkdir()
    (runtime_home / "models.yaml").write_text("private", encoding="utf-8")
    monkeypatch.setenv("HIVELOOM_HOME", str(runtime_home))
    spec = HarnessSpec(
        name="private-paths",
        description="test",
        system_prompt="test",
        tools=[BuiltinToolRef(builtin="file_read"), BuiltinToolRef(builtin="file_write")],
    )
    registry = build_registry(spec, tmp_path)

    with pytest.raises(ToolError, match="protected harness state"):
        registry.get("file_read").run(path="nonstandard-runtime-home/models.yaml")
    with pytest.raises(ToolError, match="protected harness state"):
        registry.get("file_write").run(
            path="nonstandard-runtime-home/new-secret", content="private"
        )


def test_safe_path_refuses_configured_trace_dir_case_insensitively(tmp_path: Path):
    """`trace_dir` is opt-in (only the HTTP control plane currently has a
    spec loaded to supply it — file_read/file_write don't pass one, so they
    fall back to the `.hiveloom/`-only coverage above), but `safe_path`
    itself must honor it correctly, case-insensitively, when given one.
    """
    (tmp_path / "MyLogs").mkdir()
    (tmp_path / "MyLogs" / "run_x.jsonl").write_text('{"type": "run_started"}\n')
    with pytest.raises(ToolError):
        safe_path(tmp_path, "mylogs/run_x.jsonl", trace_dir=Path("MyLogs"))


def test_shell_disabled_without_allowlist(tmp_path: Path):
    tool = ShellTool(tmp_path, allowed=[])
    registry = ToolRegistry()
    registry.register(tool)
    result = registry.dispatch(ToolCall(id="1", name="shell", input={"command": "ls"}))
    assert result.is_error
    assert "disabled" in result.content


def test_shell_allowlist_blocks_unlisted(tmp_path: Path):
    tool = ShellTool(tmp_path, allowed=[{"argv": ["echo"], "allow_extra_args": True}])
    result = tool.run(command="echo hi")
    assert "exit=0" in result and "hi" in result
    registry = ToolRegistry()
    registry.register(tool)
    blocked = registry.dispatch(ToolCall(id="1", name="shell", input={"command": "rm -rf /"}))
    assert blocked.is_error and "allowlist" in blocked.content


def test_shell_blocks_interpreters_and_dangerous_arguments(tmp_path: Path):
    tool = ShellTool(tmp_path, allowed=["python", "find ."])
    registry = ToolRegistry()
    registry.register(tool)

    interpreter = registry.dispatch(
        ToolCall(id="1", name="shell", input={"command": "python -c pass"})
    )
    assert interpreter.is_error
    assert registry.dispatch(
        ToolCall(id="2", name="shell", input={"command": "find . -exec echo {} ;"})
    ).is_error


def test_shell_legacy_rules_are_exact_and_wildcards_are_limited(tmp_path: Path):
    exact = ShellTool(tmp_path, allowed=["git status"])
    assert "exit=" in exact.run(command="git status")
    with pytest.raises(ToolError, match="allowlist"):
        exact.run(command="git status --short")
    with pytest.raises(ToolError, match="cannot allow arbitrary"):
        ShellTool(tmp_path, allowed=[{"argv": ["git", "status"], "allow_extra_args": True}])


def test_exact_shell_argv_remains_usable_without_os_isolation(tmp_path: Path):
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
    exact = ShellTool(
        tmp_path,
        allowed=["ls -1 ."],
        confinement=ConfinementConfig(mode="off"),
    )

    assert "visible.txt" in exact.run(command="ls -1 .")


def test_exact_recursive_shell_argv_cannot_walk_runtime_state_without_isolation(
    tmp_path: Path,
):
    private = tmp_path / ".hiveloom"
    private.mkdir()
    (private / "secret.txt").write_text("needle", encoding="utf-8")
    shell = ShellTool(
        tmp_path,
        allowed=["grep -r needle ."],
        confinement=ConfinementConfig(mode="off"),
        private_paths=[private],
    )

    with pytest.raises(ToolError, match="recursively traverse"):
        shell.run(command="grep -r needle .")


def test_http_get_rejects_private_addresses(tmp_path: Path):
    _ = HttpGetTool(tmp_path)
    with pytest.raises(ToolError, match="non-public"):
        _validate_public_http_url("http://127.0.0.1/latest/meta-data")


def test_dispatch_unknown_tool_is_error():
    registry = ToolRegistry()
    result = registry.dispatch(ToolCall(id="1", name="nope", input={}))
    assert result.is_error and "unknown tool" in result.content


def test_dispatch_inactive_tool_is_error(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(FileReadTool(tmp_path), active=False)

    result = registry.dispatch(ToolCall(id="1", name="file_read", input={"path": "x.txt"}))

    assert result.is_error and "inactive" in result.content


def test_schema_from_function_derives_properties():
    def fetch(po_number: str, limit: int = 10) -> str:
        return ""

    schema = schema_from_function(fetch)
    assert schema["type"] == "object"
    assert "po_number" in schema["properties"]
    assert "po_number" in schema["required"]
    assert "limit" not in schema.get("required", [])


class _ChartSeries(TypedDict):
    name: str
    values: list[float]


def test_schema_from_function_resolves_postponed_structured_annotations():
    def render(series: list[_ChartSeries]) -> str:
        return ""

    # This test module uses ``from __future__ import annotations``, so the raw
    # signature contains the string ``list[_ChartSeries]``.  The emitted tool
    # schema must nevertheless tell the model the exact object keys.
    schema = schema_from_function(render)
    item_ref = schema["properties"]["series"]["items"]["$ref"]
    item_schema = schema["$defs"][item_ref.rsplit("/", 1)[-1]]
    assert item_schema["required"] == ["name", "values"]
    assert item_schema["properties"]["values"]["items"]["type"] == "number"


def test_function_tool_stringifies_result():
    def go(x: str) -> dict:
        return {"x": x}

    tool = FunctionTool(go, name="go", description="d", tags=[])
    assert tool.run(x="hi") == "{'x': 'hi'}"


def test_build_registry_from_spec(tmp_path: Path):
    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "tools": [{"builtin": "file_read"}, {"builtin": "http_get"}],
        }
    )
    registry = build_registry(spec, tmp_path)
    assert set(registry.active_names()) == {"file_read", "http_get"}
    # The spill readers are registered but inactive: they cost nothing in the
    # tool payload until a result actually spills.
    assert set(registry.names()) == {
        "file_read",
        "http_get",
        "read_tool_result",
        "search_tool_result",
        "transform_result",
    }
    payload = registry.anthropic_payload()
    assert all("input_schema" in t for t in payload)


def test_build_registry_wires_trace_dir_into_file_read(tmp_path: Path):
    """End-to-end: `build_registry` threads the spec's configured
    `logging.trace_dir` through to `file_read` (not just the tool classes
    directly), closing the gap where a reconfigured trace directory was
    readable through a real, spec-driven tool call.
    """
    (tmp_path / "run_logs").mkdir()
    (tmp_path / "run_logs" / "run_x.jsonl").write_text(
        '{"type": "run_finished", "payload": {"output": "distinctive-secret"}}\n'
    )
    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "tools": [{"builtin": "file_read"}],
            "logging": {"trace_dir": "run_logs"},
        }
    )
    registry = build_registry(spec, tmp_path)
    result = registry.dispatch(
        ToolCall(id="1", name="file_read", input={"path": "run_logs/run_x.jsonl"})
    )
    assert result.is_error
    assert "distinctive-secret" not in result.content


def test_no_network_write_tag_present_on_builtins(tmp_path: Path):
    spec = HarnessSpec.model_validate(
        {
            "name": "t",
            "description": "d",
            "system_prompt": "sp",
            "tools": [{"builtin": "file_write"}],
        }
    )
    registry = build_registry(spec, tmp_path)
    assert "write" in registry.get("file_write").tags


def test_builtin_tool_ref_direct():
    ref = BuiltinToolRef(builtin="file_read")
    assert ref.params() == {}


# --------------------------------------------------------------------------- #
# Handle-typed tool parameters
# --------------------------------------------------------------------------- #
_HANDLE = "tr_0123456789abcdef"


def _handle_registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FileWriteTool(tmp_path))
    return registry


def _resolver(text: str = "the whole stored object"):
    def resolve(handle: str, max_bytes: int) -> str:
        assert handle == _HANDLE
        if len(text.encode("utf-8")) > max_bytes:
            raise ToolError(f"'{handle}' is too large to expand")
        return text

    return resolve


def test_file_write_declares_content_as_handle_capable(tmp_path: Path):
    assert FileWriteTool(tmp_path).handle_params == ("content",)


def test_a_handle_argument_is_expanded_from_private_storage(tmp_path: Path):
    registry = _handle_registry(tmp_path)
    result = registry.dispatch(
        ToolCall(id="1", name="file_write", input={"path": "out.txt", "content": _HANDLE}),
        resolve_handle=_resolver(),
    )

    assert not result.is_error
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "the whole stored object"


def test_a_value_that_is_not_a_handle_is_written_literally(tmp_path: Path):
    registry = _handle_registry(tmp_path)
    registry.dispatch(
        ToolCall(
            id="1",
            name="file_write",
            input={"path": "out.txt", "content": "tr_notahandle and prose"},
        ),
        resolve_handle=_resolver(),
    )

    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "tr_notahandle and prose"


def test_an_unresolvable_handle_is_an_error_not_a_literal(tmp_path: Path):
    # Writing the token itself because the object behind it could not be read
    # is the one outcome indistinguishable from success.
    def refuse(handle: str, _max_bytes: int) -> str:
        raise ToolError(f"unknown handle '{handle}'")

    registry = _handle_registry(tmp_path)
    result = registry.dispatch(
        ToolCall(id="1", name="file_write", input={"path": "out.txt", "content": _HANDLE}),
        resolve_handle=refuse,
    )

    assert result.is_error
    assert "unknown handle" in result.content
    assert not (tmp_path / "out.txt").exists()


def test_a_handle_without_a_store_is_refused(tmp_path: Path):
    registry = _handle_registry(tmp_path)
    result = registry.dispatch(
        ToolCall(id="1", name="file_write", input={"path": "out.txt", "content": _HANDLE})
    )

    assert result.is_error
    assert "no stored results" in result.content


def test_an_expansion_is_capped(tmp_path: Path):
    from hiveloom.tools.registry import MAX_HANDLE_ARG_BYTES

    registry = _handle_registry(tmp_path)
    result = registry.dispatch(
        ToolCall(id="1", name="file_write", input={"path": "out.txt", "content": _HANDLE}),
        resolve_handle=_resolver("x" * (MAX_HANDLE_ARG_BYTES + 1)),
    )

    assert result.is_error
    assert "too large to expand" in result.content


def test_a_code_tool_declares_handle_parameters_on_the_decorator():
    from hiveloom.tools import tool

    @tool(description="Index a document.", handles=["text", "missing"])
    def index(text: str, label: str = "") -> str:
        return f"{label}:{len(text)}"

    wrapped = FunctionTool(index, name="index", description="d", tags=[])
    # Only declared parameters that actually exist become handle-capable.
    assert wrapped.handle_params == ("text",)

    registry = ToolRegistry()
    registry.register(wrapped)
    result = registry.dispatch(
        ToolCall(id="1", name="index", input={"text": _HANDLE, "label": _HANDLE}),
        resolve_handle=_resolver("abcde"),
    )
    # ...and an undeclared parameter keeps the literal it was given.
    assert result.content == f"{_HANDLE}:5"


def test_a_tool_without_handle_parameters_is_untouched(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(FileReadTool(tmp_path))

    def explode(_handle: str, _max_bytes: int) -> str:
        raise AssertionError("no parameter declares a handle")

    result = registry.dispatch(
        ToolCall(id="1", name="file_read", input={"path": _HANDLE}), resolve_handle=explode
    )
    assert result.is_error  # no such file — but the resolver was never consulted
