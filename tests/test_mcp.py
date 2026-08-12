"""Tests for MCP server consumption (WS2a): bridge, adapter, and registry wiring.

The schema's discriminator/uniqueness/frozen-path tests live in
tests/test_schema.py and tests/test_evolve.py, alongside their sibling tests
for the other ref types.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anyio
import mcp
import mcp.types as mcp_types
import pytest

from hiveloom import construct, runner, trust
from hiveloom.errors import McpError, SpecError
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import ToolCall
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import McpHttpServerRef, McpStdioServerRef
from hiveloom.tools.mcp import (
    McpBridge,
    McpToolAdapter,
    _build_adapters,
    _flatten_call_tool_result,
    _list_all_tools,
    _resolve_env,
    _resolve_headers,
    _sanitize,
    connect_mcp_server,
)
from hiveloom.tools.registry import ToolRegistry, ToolResult, build_registry

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")


def _stdio_ref(**overrides) -> McpStdioServerRef:
    data = {
        "name": "echo",
        "command": sys.executable,
        "args": [FIXTURE],
        "timeout_seconds": 10.0,
    }
    data.update(overrides)
    return McpStdioServerRef.model_validate(data)


def _mcp_servers_yaml(servers: list[dict]) -> str:
    """JSON (valid YAML flow syntax) for a `mcp_servers:` value.

    The union dispatches on the literal `transport` tag, so callers that omit
    it get `stdio` filled in here (every entry in this test file is stdio).
    """
    filled = [{"transport": "stdio", **entry} for entry in servers]
    return json.dumps(filled)


# --------------------------------------------------------------------------- #
# Pure unit tests: no process, no network
# --------------------------------------------------------------------------- #
def test_sanitize_keeps_safe_charset_untouched():
    assert _sanitize("already-ok_Name9") == "already-ok_Name9"


def test_sanitize_replaces_unsafe_characters():
    assert _sanitize("weird tool!name") == "weird_tool_name"


def test_sanitize_never_produces_empty_name():
    assert _sanitize("") == "tool"


def test_adapter_name_is_prefixed_and_collision_proof():
    adapter = McpToolAdapter(
        bridge=None,
        session=None,
        server_name="s1",
        remote_name="weird tool!",
        description="d",
        input_schema={},
        transport="stdio",
        timeout=5.0,
    )
    assert adapter.name == "mcp__s1__weird_tool_"


def test_adapter_tags_stdio_is_exec_dangerous():
    adapter = McpToolAdapter(
        bridge=None,
        session=None,
        server_name="s",
        remote_name="t",
        description="d",
        input_schema={},
        transport="stdio",
        timeout=5.0,
    )
    assert adapter.tags == ["mcp", "mcp:s", "exec", "dangerous"]


def test_adapter_tags_http_is_network():
    adapter = McpToolAdapter(
        bridge=None,
        session=None,
        server_name="s",
        remote_name="t",
        description="d",
        input_schema={},
        transport="http",
        timeout=5.0,
    )
    assert adapter.tags == ["mcp", "mcp:s", "network"]


def test_adapter_defaults_to_empty_object_schema():
    adapter = McpToolAdapter(
        bridge=None,
        session=None,
        server_name="s",
        remote_name="t",
        description="d",
        input_schema={},
        transport="http",
        timeout=5.0,
    )
    assert adapter.input_schema == {"type": "object", "properties": {}}


def test_flatten_joins_multiple_text_blocks():
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(type="text", text="a"),
            mcp_types.TextContent(type="text", text="b"),
        ]
    )
    assert _flatten_call_tool_result(result) == "a\nb"


def test_flatten_falls_back_to_structured_content():
    result = mcp_types.CallToolResult(content=[], structuredContent={"x": 1})
    assert _flatten_call_tool_result(result) == json.dumps({"x": 1})


def test_flatten_non_text_content_is_explained_not_silent():
    result = mcp_types.CallToolResult(
        content=[mcp_types.ImageContent(type="image", data="AA==", mimeType="image/png")]
    )
    text = _flatten_call_tool_result(result)
    assert text and "image" in text


def test_flatten_empty_result_is_explained_not_silent():
    result = mcp_types.CallToolResult(content=[])
    text = _flatten_call_tool_result(result)
    assert text  # never a silent empty string


def _tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name, description="d", inputSchema={"type": "object", "properties": {}}
    )


class _FakeBridge:
    """A bridge stand-in whose `.call` runs the callable inline (no portal)."""

    def call(self, fn, *args):
        return fn(*args)


class _FakeSession:
    def __init__(self, pages: list[mcp_types.ListToolsResult]):
        self._pages = iter(pages)
        self.cursors_seen: list[str | None] = []

    def list_tools(
        self, *, params: mcp_types.PaginatedRequestParams | None = None
    ) -> mcp_types.ListToolsResult:
        self.cursors_seen.append(params.cursor if params else None)
        return next(self._pages)


def test_pagination_follows_cursor_to_exhaustion():
    pages = [
        mcp_types.ListToolsResult(tools=[_tool("a")], nextCursor="p2"),
        mcp_types.ListToolsResult(tools=[_tool("b")], nextCursor="p3"),
        mcp_types.ListToolsResult(tools=[_tool("c")], nextCursor=None),
    ]
    session = _FakeSession(pages)
    tools = _list_all_tools(_FakeBridge(), session)
    assert [t.name for t in tools] == ["a", "b", "c"]
    assert session.cursors_seen == [None, "p2", "p3"]


def test_pagination_cap_raises_mcp_error_past_500():
    page1 = mcp_types.ListToolsResult(tools=[_tool(f"t{i}") for i in range(500)], nextCursor="more")
    page2 = mcp_types.ListToolsResult(tools=[_tool("t500")], nextCursor=None)
    with pytest.raises(McpError, match="500"):
        _list_all_tools(_FakeBridge(), _FakeSession([page1, page2]))


def test_pagination_exactly_500_is_fine():
    page = mcp_types.ListToolsResult(tools=[_tool(f"t{i}") for i in range(500)], nextCursor=None)
    tools = _list_all_tools(_FakeBridge(), _FakeSession([page]))
    assert len(tools) == 500


def test_intra_server_sanitize_collision_raises_mcp_error():
    """Two remote tools from the SAME server sanitizing to the same adapter
    name must raise rather than silently overwrite one another in the
    registry (last-registered-wins would make one uncallable with no
    indication anything went wrong)."""
    ref = _stdio_ref()
    remote_tools = [_tool("foo!"), _tool("foo?")]  # both sanitize to "foo_"
    with pytest.raises(McpError, match="foo!.*foo\\?|foo\\?.*foo!"):
        _build_adapters(ref, remote_tools, bridge=None, session=None, transport="stdio")


def test_intra_server_distinct_names_do_not_collide():
    ref = _stdio_ref()
    remote_tools = [_tool("foo"), _tool("bar")]
    adapters = _build_adapters(ref, remote_tools, bridge=None, session=None, transport="stdio")
    assert {a.name for a in adapters} == {"mcp__echo__foo", "mcp__echo__bar"}


def test_resolve_env_merges_literal_and_host_secrets(monkeypatch):
    monkeypatch.setenv("HL_TEST_SECRET", "shh")
    ref = _stdio_ref(env={"FOO": "bar"}, env_from_host_env={"TOKEN": "HL_TEST_SECRET"})
    assert _resolve_env(ref) == {"FOO": "bar", "TOKEN": "shh"}


def test_resolve_env_missing_host_var_raises(monkeypatch):
    monkeypatch.delenv("HL_TEST_MISSING", raising=False)
    ref = _stdio_ref(env_from_host_env={"TOKEN": "HL_TEST_MISSING"})
    with pytest.raises(McpError, match="HL_TEST_MISSING"):
        _resolve_env(ref)


def test_resolve_headers_merges_literal_and_host_secrets(monkeypatch):
    monkeypatch.setenv("HL_TEST_TOKEN", "abc123")
    ref = McpHttpServerRef(
        name="h", url="https://example.invalid/mcp",
        headers={"X-Foo": "bar"}, header_env={"Authorization": "HL_TEST_TOKEN"},
    )
    assert _resolve_headers(ref) == {"X-Foo": "bar", "Authorization": "abc123"}


def test_resolve_headers_missing_host_var_raises(monkeypatch):
    monkeypatch.delenv("HL_TEST_MISSING_HDR", raising=False)
    ref = McpHttpServerRef(
        name="h", url="https://example.invalid/mcp",
        header_env={"Authorization": "HL_TEST_MISSING_HDR"},
    )
    with pytest.raises(McpError, match="HL_TEST_MISSING_HDR"):
        _resolve_headers(ref)


# --------------------------------------------------------------------------- #
# Real stdio subprocess (offline, local)
# --------------------------------------------------------------------------- #
def test_stdio_discovery_dispatch_and_error_never_raises_out_of_dispatch(tmp_path: Path):
    ref = _stdio_ref()
    bridge = McpBridge()
    registry = ToolRegistry()
    registry.add_closer(bridge.close)
    try:
        tools = connect_mcp_server(ref, tmp_path, bridge)
        names = {t.name for t in tools}
        assert {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__boom"} <= names
        for tool in tools:
            registry.register(tool)
            assert tool.tags == ["mcp", "mcp:echo", "exec", "dangerous"]
            assert tool.input_schema  # usable input_schema, not a stub

        echo_result = registry.dispatch(
            ToolCall(id="1", name="mcp__echo__echo", input={"text": "hi"})
        )
        assert isinstance(echo_result, ToolResult)
        assert not echo_result.is_error
        assert "hi" in echo_result.content

        add_result = registry.dispatch(
            ToolCall(id="2", name="mcp__echo__add", input={"a": 2, "b": 3})
        )
        assert not add_result.is_error
        assert "5" in add_result.content

        boom_result = registry.dispatch(ToolCall(id="3", name="mcp__echo__boom", input={}))
        assert boom_result.is_error  # never raises out of dispatch
    finally:
        registry.close()


def test_stdio_tools_allowlist_filters_remote_tools(tmp_path: Path):
    ref = _stdio_ref(tools=["echo"])
    bridge = McpBridge()
    try:
        tools = connect_mcp_server(ref, tmp_path, bridge)
        assert {t.name for t in tools} == {"mcp__echo__echo"}
    finally:
        bridge.close()


def test_partial_mcp_connect_failure_tears_down_earlier_servers(harness_dir: Path):
    """If a later server fails to connect, an earlier server's already-open
    session/subprocess (and the shared bridge/portal) must not leak."""
    construct.set_field(
        harness_dir, "mcp_servers",
        _mcp_servers_yaml(
            [
                {
                    "name": "echo",
                    "command": sys.executable,
                    "args": [FIXTURE],
                    "timeout_seconds": 10.0,
                },
                {
                    "name": "bogus",
                    "command": "definitely-not-a-real-executable-xyz",
                    "timeout_seconds": 2.0,
                },
            ]
        ),
    )
    before = threading.active_count()
    spec = load_spec(harness_dir)
    with pytest.raises(McpError):
        build_registry(spec, harness_dir)
    assert threading.active_count() == before


def test_initialize_timeout_raises_mcp_error_and_tears_down(harness_dir: Path, monkeypatch):
    """A hung session.initialize() must not block build_registry forever.

    Exercises the actual cancellation-scope logic (anyio.fail_after inside
    McpBridge._initialize, run through the real portal via `portal.call`) by
    monkeypatching ClientSession.initialize to hang, rather than asserting
    only that the code reads correctly.
    """

    async def _hang(self) -> None:
        await anyio.sleep(8)

    monkeypatch.setattr(mcp.ClientSession, "initialize", _hang)

    construct.set_field(
        harness_dir, "mcp_servers",
        _mcp_servers_yaml(
            [
                {
                    "name": "echo",
                    "command": sys.executable,
                    "args": [FIXTURE],
                    "timeout_seconds": 0.2,
                }
            ]
        ),
    )
    spec = load_spec(harness_dir)

    before = threading.active_count()
    start = time.monotonic()
    with pytest.raises(McpError, match="did not initialize"):
        build_registry(spec, harness_dir)
    elapsed = time.monotonic() - start

    # Honored roughly the configured 0.2s timeout -- not a silently-ignored
    # default (e.g. the 30s spec default) and not an unbounded hang.
    assert elapsed < 5.0
    assert threading.active_count() == before


def test_registry_close_tears_down_cleanly_across_two_build_cycles(harness_dir: Path):
    """A leaked portal thread or un-torn-down subprocess must not survive a
    second build/close cycle in the same test session."""
    construct.set_field(
        harness_dir, "mcp_servers",
        _mcp_servers_yaml(
            [
                {
                    "name": "echo",
                    "command": sys.executable,
                    "args": [FIXTURE],
                    "timeout_seconds": 10.0,
                }
            ]
        ),
    )
    before = threading.active_count()
    for i in range(2):
        spec = load_spec(harness_dir)
        registry = build_registry(spec, harness_dir)
        assert "mcp__echo__echo" in registry.names()
        result = registry.dispatch(
            ToolCall(id="1", name="mcp__echo__echo", input={"text": f"cycle{i}"})
        )
        assert not result.is_error
        assert f"cycle{i}" in result.content
        registry.close()
    assert threading.active_count() == before


def test_deferred_mcp_tools_activate_via_search_tools(harness_dir: Path):
    construct.set_field(harness_dir, "loop.require_verification", "false")
    construct.set_field(
        harness_dir, "mcp_servers",
        _mcp_servers_yaml(
            [
                {
                    "name": "echo", "command": sys.executable, "args": [FIXTURE],
                    "deferred": True, "timeout_seconds": 10.0,
                }
            ]
        ),
    )

    spec = load_spec(harness_dir)
    registry = build_registry(spec, harness_dir)
    try:
        payload_names = [t["name"] for t in registry.anthropic_payload()]
        assert payload_names == ["search_tools"]
        assert "mcp__echo__echo" in registry.inactive_names()
    finally:
        registry.close()

    provider = FakeModelProvider(
        [
            tool_response("search_tools", {"query": "echo"}, call_id="c1"),
            tool_response("mcp__echo__echo", {"text": "hi"}, call_id="c2"),
            text_response("done"),
        ]
    )
    result = runner.run_harness(harness_dir, "go", provider=provider)
    assert result.status == "success"
    assert "mcp__echo__echo" in [t["name"] for t in provider.calls[1]["tools"]]


def test_mcp_trust_gate_blocks_before_any_subprocess_spawns(harness_dir: Path, monkeypatch):
    construct.set_field(
        harness_dir, "mcp_servers",
        _mcp_servers_yaml([{"name": "echo", "command": sys.executable, "args": [FIXTURE]}]),
    )
    monkeypatch.setenv("HIVELOOM_TRUST", "never")
    trust.revoke_trust(harness_dir)

    def _fail_if_called(self, *args, **kwargs):
        pytest.fail("McpBridge.connect_stdio must not be called before trust is established")

    monkeypatch.setattr(McpBridge, "connect_stdio", _fail_if_called)

    with pytest.raises(SpecError, match="not trusted"):
        runner.run_harness(harness_dir, "go", provider=FakeModelProvider([]))
    with pytest.raises(SpecError, match="not trusted"):
        runner.dry_run(harness_dir, "go")


def test_dry_run_discovers_mcp_tools_eagerly(harness_dir: Path):
    """USER-APPROVED deviation: dry-run does real I/O when mcp_servers is set."""
    construct.set_field(
        harness_dir, "mcp_servers",
        _mcp_servers_yaml(
            [
                {
                    "name": "echo",
                    "command": sys.executable,
                    "args": [FIXTURE],
                    "timeout_seconds": 10.0,
                }
            ]
        ),
    )
    payload = runner.dry_run(harness_dir, "go")
    assert "mcp__echo__echo" in [t["name"] for t in payload["tools"]]


# --------------------------------------------------------------------------- #
# HTTP transport (one test, real loopback server -- exercises streamablehttp_client
# for real rather than bypassing it with in-process memory streams).
# --------------------------------------------------------------------------- #
@contextmanager
def _serve_http(app) -> Iterator[str]:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test http server did not start in time")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def test_http_transport_round_trip(tmp_path: Path):
    from mcp.server import MCPServer

    app_mcp = MCPServer("echo-http")

    @app_mcp.tool()
    def ping() -> str:
        return "pong"

    with _serve_http(app_mcp.streamable_http_app()) as url:
        ref = McpHttpServerRef(name="httpsrv", url=url, timeout_seconds=5.0)
        bridge = McpBridge()
        try:
            tools = connect_mcp_server(ref, tmp_path, bridge)
            assert {t.name for t in tools} == {"mcp__httpsrv__ping"}
            tool = tools[0]
            assert tool.tags == ["mcp", "mcp:httpsrv", "network"]

            registry = ToolRegistry()
            registry.register(tool)
            result = registry.dispatch(ToolCall(id="1", name="mcp__httpsrv__ping", input={}))
            assert not result.is_error
            assert "pong" in result.content
        finally:
            bridge.close()


# --------------------------------------------------------------------------- #
# Structured artifacts over MCP
# --------------------------------------------------------------------------- #
def test_mcp_tool_can_return_caller_artifacts(tmp_path: Path):
    """A tool behind MCP keeps the artifact channel a local code tool has."""
    from hiveloom.tools.registry import Artifact

    bridge = McpBridge()
    registry = ToolRegistry()
    registry.add_closer(bridge.close)
    try:
        for tool in connect_mcp_server(_stdio_ref(), tmp_path, bridge):
            registry.register(tool)
        result = registry.dispatch(
            ToolCall(id="c1", name="mcp__echo__chart", input={"title": "AUM"})
        )
    finally:
        registry.close()

    assert result.artifacts == [Artifact(kind="chart", data={"title": "AUM"})]
    # The envelope is for the caller; the model must not be billed for it.
    assert "_hiveloom" not in result.content
    assert "chart AUM registered" in result.content
