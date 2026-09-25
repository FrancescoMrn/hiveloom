"""Tests for `hiveloom mcp serve`: harnesses exposed as MCP tools."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context

from hiveloom import construct, runner, trust
from hiveloom.errors import SpecError
from hiveloom.logging.hive import Hive
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.serve.mcp import _sanitize, build_mcp_server
from hiveloom.spec.loader import harness_path, load_spec

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"
PEER_SERVER = str(Path(__file__).parent / "fixtures" / "mcp_peer_harness_server.py")

_SOURCE = "The quick brown fox jumps over the lazy dog. " * 20
_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def _make_harness(tmp_path: Path, name: str = "summarizer") -> Path:
    target = tmp_path / name
    shutil.copytree(EXAMPLE_HARNESS, target)
    return target


def _wire_context(server, lineage: dict | None) -> Context:
    """A Context shaped like one the SDK builds for a real tools/call.

    The delegation contract lives in the request `_meta`, so a test that never
    goes through a request context would assert nothing about it.
    """
    request_context = ServerRequestContext(
        session=None,  # type: ignore[arg-type] - unused by the run tool
        lifespan_context={},
        protocol_version="2026-07-28",
        method="tools/call",
        request_id=1,
        meta={"hiveloom": lineage} if lineage is not None else None,
    )
    return Context(request_context=request_context, mcp_server=server)


def _lineage(*, depth: int, chain: list[str], parent_run_id: str = "run_parent") -> dict:
    return {
        "kind": "delegation",
        "parent_run_id": parent_run_id,
        "parent_harness_id": chain[-1] if chain else "",
        "depth": depth,
        "chain": chain,
    }


def test_sanitize_maps_to_mcp_tool_charset():
    assert _sanitize("example-summarizer") == "example-summarizer"
    assert _sanitize("weird name!") == "weird_name_"
    assert _sanitize("") == "harness"


def test_server_lists_one_run_tool_per_harness(tmp_path: Path):
    harness = _make_harness(tmp_path)
    server = build_mcp_server([harness])

    tools = anyio.run(server.list_tools)

    assert [t.name for t in tools] == ["run_example-summarizer", "list_harnesses"]
    # The harness description is the tool description an agent selects by.
    assert "Summarize a text file" in tools[0].description


def test_run_tool_returns_structured_verified_result(tmp_path: Path):
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )

    result = anyio.run(
        server.call_tool, "run_example-summarizer", {"input": _SOURCE}
    )

    structured = result.structured_content
    assert structured["status"] == "success"
    assert structured["output"] == _VALID_SUMMARY
    assert structured["run_id"].startswith("run_")
    assert all(v["passed"] for v in structured["verdicts"])


def test_run_tool_reports_failure_as_data_not_error(tmp_path: Path):
    """A verify-failed run is a structured result the calling agent can read,
    not a protocol-level tool error."""
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response("not json")]),
    )

    result = anyio.run(
        server.call_tool, "run_example-summarizer", {"input": _SOURCE}
    )

    structured = result.structured_content
    assert structured["status"] == "verify_failed"
    assert any(not v["passed"] for v in structured["verdicts"])


def test_input_is_literal_never_a_server_file_read(tmp_path: Path):
    """Naming a server-side file as input must pass the name through as text,
    mirroring the HTTP servers (an MCP caller is a remote caller)."""
    harness = _make_harness(tmp_path)
    (harness / "notes.txt").write_text(_SOURCE)
    captured: list[FakeModelProvider] = []

    def factory() -> FakeModelProvider:
        provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
        captured.append(provider)
        return provider

    server = build_mcp_server([harness], provider_factory=factory)
    anyio.run(server.call_tool, "run_example-summarizer", {"input": "notes.txt"})

    first_user = captured[0].calls[0]["messages"][0]
    assert first_user["content"] == "notes.txt"  # not the file's contents


def test_two_harnesses_with_same_name_collide_at_startup(tmp_path: Path):
    first = _make_harness(tmp_path / "a")
    second = _make_harness(tmp_path / "b")
    with pytest.raises(SpecError, match="collision"):
        build_mcp_server([first, second])


def test_list_harnesses_tool_reports_catalog_and_fitness(tmp_path: Path):
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )

    # One real run so the Hive has fitness evidence for this harness.
    anyio.run(server.call_tool, "run_example-summarizer", {"input": _SOURCE})
    result = anyio.run(server.call_tool, "list_harnesses", {})

    listed = result.structured_content["harnesses"]
    assert len(listed) == 1
    entry = listed[0]
    assert entry["tool"] == "run_example-summarizer"
    assert entry["name"] == "example-summarizer"
    assert entry["total_runs"] >= 1
    assert entry["success_rate"] == 1.0


# --------------------------------------------------------------------------- #
# HTTP transport: bearer gate and bind policy
# --------------------------------------------------------------------------- #
def _http_scope(headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


def _call_asgi(app, scope) -> tuple[int | None, bool]:
    """Drive one request through the gate; returns (status, anything sent)."""
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    anyio.run(app, scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, bool(sent)


def test_http_gate_rejects_missing_and_wrong_keys(tmp_path: Path):
    from hiveloom.serve.mcp import build_http_app

    harness = _make_harness(tmp_path)
    server = build_mcp_server([harness])
    app = build_http_app(server, api_key="sekret")

    status, _ = _call_asgi(app, _http_scope({}))
    assert status == 401
    status, _ = _call_asgi(app, _http_scope({"authorization": "Bearer wrong"}))
    assert status == 401
    status, _ = _call_asgi(app, _http_scope({"x-api-key": "wrong"}))
    assert status == 401


def test_http_gate_absent_key_serves_open(tmp_path: Path):
    from hiveloom.serve.mcp import build_http_app

    harness = _make_harness(tmp_path)
    server = build_mcp_server([harness])
    app = build_http_app(server, api_key=None)
    # Without a key the app is the SDK's own (no wrapper in between).
    assert not callable(getattr(app, "__wrapped__", None))


def test_check_http_bind_refuses_open_network_bind():
    from hiveloom.serve.mcp import check_http_bind

    check_http_bind("127.0.0.1", None)  # loopback without key: fine
    check_http_bind("0.0.0.0", "sekret")  # network with key: fine
    with pytest.raises(SpecError, match="refusing to bind"):
        check_http_bind("0.0.0.0", None)


# --------------------------------------------------------------------------- #
# Errors as data: a caller on the far end of a pipe cannot read a traceback
# --------------------------------------------------------------------------- #
def test_run_tool_returns_any_exception_as_a_structured_error(tmp_path: Path):
    """A run that cannot even start (no credentials, unreadable spec) must come
    back as a readable result, not as the SDK's opaque "Error executing tool"."""
    harness = _make_harness(tmp_path)

    def explode() -> FakeModelProvider:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    server = build_mcp_server([harness], provider_factory=explode)

    result = anyio.run(server.call_tool, "run_example-summarizer", {"input": _SOURCE})

    assert not result.is_error  # data, not a protocol-level failure
    assert result.structured_content == {
        "status": "error",
        "output": "",
        "reason": "RuntimeError: ANTHROPIC_API_KEY is not set",
        "turns": 0,
        "cost_usd": 0.0,
        "run_id": "",
        "verdicts": [],
    }


def test_run_tool_error_keeps_the_success_result_shape(tmp_path: Path):
    """One reader parses both outcomes: same keys, whatever happened."""
    harness = _make_harness(tmp_path)
    ok = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )
    def boom() -> FakeModelProvider:
        raise ValueError("no credentials")

    broken = build_mcp_server([harness], provider_factory=boom)

    good = anyio.run(ok.call_tool, "run_example-summarizer", {"input": _SOURCE})
    bad = anyio.run(broken.call_tool, "run_example-summarizer", {"input": _SOURCE})

    assert set(good.structured_content) == set(bad.structured_content)


# --------------------------------------------------------------------------- #
# Artifacts across the wire
# --------------------------------------------------------------------------- #
_CHART_TOOL = '''
from hiveloom.tools import Artifact, ToolResult, tool


@tool(description="Render a chart for the caller's UI.")
def render_chart(title: str) -> ToolResult:
    return ToolResult(
        content=f"Chart {title!r} registered.",
        artifacts=[Artifact(kind="chart", data={"title": title})],
    )
'''


def _artifact_harness(tmp_path: Path) -> Path:
    directory = tmp_path / "charts"
    construct.init_harness(directory, name="chart-harness", task="Chart things.")
    construct.set_field(directory, "loop.require_verification", "false")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / "chart.py").write_text(_CHART_TOOL)
    construct.add_tool(directory, code="tools/chart.py:render_chart", description="Chart.")
    return directory


def test_run_tool_carries_artifacts_in_the_envelope(tmp_path: Path):
    """A harness behind MCP keeps the caller channel a local run has: without
    this, moving a harness behind `mcp serve` silently drops its artifacts."""
    harness = _artifact_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider(
            [
                tool_response("render_chart", {"title": "AUM"}, call_id="c1"),
                text_response("charted"),
            ]
        ),
    )

    result = anyio.run(server.call_tool, "run_chart-harness", {"input": "chart it"})

    structured = result.structured_content
    assert structured["status"] == "success"
    assert structured["_hiveloom"] == {
        "artifacts": [{"kind": "chart", "data": {"title": "AUM"}}]
    }


def test_run_tool_without_artifacts_omits_the_envelope(tmp_path: Path):
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )

    result = anyio.run(server.call_tool, "run_example-summarizer", {"input": _SOURCE})

    assert "_hiveloom" not in result.structured_content


# --------------------------------------------------------------------------- #
# Delegation: lineage, depth bound, cycle guard
# --------------------------------------------------------------------------- #
def test_delegated_run_is_linked_to_its_parent_in_the_hive(tmp_path: Path):
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )
    ctx = _wire_context(server, _lineage(depth=1, chain=["hl-caller"]))

    result = anyio.run(
        server.call_tool, "run_example-summarizer", {"input": _SOURCE}, ctx
    )

    child_run_id = result.structured_content["run_id"]
    with Hive() as hive:
        child = hive.get_run(child_run_id)
    assert child is not None
    assert child["parent_run_id"] == "run_parent"


def test_delegation_deeper_than_max_depth_is_refused_as_data(tmp_path: Path):
    """Refused before the first paid turn, and refused as a result the caller
    can read — a runaway delegation graph spends real money at every hop."""
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: pytest.fail("the run must never start"),
        max_depth=2,
    )
    ctx = _wire_context(server, _lineage(depth=3, chain=["a", "b", "c"]))

    result = anyio.run(
        server.call_tool, "run_example-summarizer", {"input": _SOURCE}, ctx
    )

    structured = result.structured_content
    assert structured["status"] == "error"
    assert "max-depth 2" in structured["reason"]
    assert structured["run_id"] == ""


def test_delegation_cycle_is_refused_as_data(tmp_path: Path):
    harness = _make_harness(tmp_path)
    # The chain is written in Hive keys (`spec.identity`), which is also what
    # list_harnesses reports — so a cycle is detectable across machines.
    identity = load_spec(harness_path(harness)).identity
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: pytest.fail("the run must never start"),
    )
    ctx = _wire_context(server, _lineage(depth=2, chain=["hl-root", identity]))

    result = anyio.run(
        server.call_tool, "run_example-summarizer", {"input": _SOURCE}, ctx
    )

    structured = result.structured_content
    assert structured["status"] == "error"
    assert "cycle" in structured["reason"]


def test_a_call_without_lineage_still_runs(tmp_path: Path):
    """An ordinary MCP client (Claude Code, an SDK script) sends no `_meta`."""
    harness = _make_harness(tmp_path)
    server = build_mcp_server(
        [harness],
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )
    ctx = _wire_context(server, None)

    result = anyio.run(
        server.call_tool, "run_example-summarizer", {"input": _SOURCE}, ctx
    )

    assert result.structured_content["status"] == "success"


def test_max_depth_and_concurrency_are_validated(tmp_path: Path):
    harness = _make_harness(tmp_path)
    with pytest.raises(SpecError, match="max-depth"):
        build_mcp_server([harness], max_depth=0)
    with pytest.raises(SpecError, match="concurrency"):
        build_mcp_server([harness], concurrency=0)


# --------------------------------------------------------------------------- #
# Concurrency bound
# --------------------------------------------------------------------------- #
class _TrackingProvider(FakeModelProvider):
    """Counts how many runs are inside the model call at the same moment."""

    state = {"live": 0, "peak": 0}
    lock = threading.Lock()
    barrier: threading.Barrier | None = None

    def complete(self, **kwargs):
        with _TrackingProvider.lock:
            _TrackingProvider.state["live"] += 1
            _TrackingProvider.state["peak"] = max(
                _TrackingProvider.state["peak"], _TrackingProvider.state["live"]
            )
        try:
            if _TrackingProvider.barrier is not None:
                # Deterministic proof of overlap: unless both runs are inside
                # the model call at once, this times out and the test fails.
                _TrackingProvider.barrier.wait(timeout=10)
            else:
                time.sleep(0.2)
        finally:
            with _TrackingProvider.lock:
                _TrackingProvider.state["live"] -= 1
        return super().complete(**kwargs)


def _call_twice(server) -> None:
    async def both() -> None:
        async with anyio.create_task_group() as tg:
            for _ in range(2):
                tg.start_soon(
                    lambda: server.call_tool(
                        "run_example-summarizer", {"input": _SOURCE}
                    )
                )

    anyio.run(both)


def test_concurrency_bound_serializes_runs(tmp_path: Path):
    harness = _make_harness(tmp_path)
    _TrackingProvider.state = {"live": 0, "peak": 0}
    _TrackingProvider.barrier = None
    bounded = build_mcp_server(
        [harness],
        provider_factory=lambda: _TrackingProvider([text_response(_VALID_SUMMARY)]),
        concurrency=1,
    )

    _call_twice(bounded)

    assert _TrackingProvider.state["peak"] == 1

    # ...and without the bound the same two calls really do overlap, so it is
    # the semaphore that serialized them and not the transport.
    _TrackingProvider.state = {"live": 0, "peak": 0}
    _TrackingProvider.barrier = threading.Barrier(2)
    unbounded = build_mcp_server(
        [harness],
        provider_factory=lambda: _TrackingProvider([text_response(_VALID_SUMMARY)]),
    )
    try:
        _call_twice(unbounded)
    finally:
        _TrackingProvider.barrier = None
    assert _TrackingProvider.state["peak"] == 2


# --------------------------------------------------------------------------- #
# End to end: harness A delegates to harness B over a real stdio wire
# --------------------------------------------------------------------------- #
def _peer_server_yaml(peer: Path, output: str) -> str:
    return json.dumps(
        [
            {
                "transport": "stdio",
                "name": "peer",
                "command": sys.executable,
                "args": [PEER_SERVER, str(peer), output],
                # A peer harness runs a whole agent loop: the caller's timeout
                # must cover the callee's wall clock, not a tool call's.
                "timeout_seconds": 120.0,
            }
        ]
    )


def _delegating_provider() -> FakeModelProvider:
    return FakeModelProvider(
        [
            tool_response(
                "mcp__peer__run_peer-harness", {"input": "do the thing"}, call_id="c1"
            ),
            text_response("relayed"),
        ]
    )


def test_harness_to_harness_delegation_over_stdio(tmp_path: Path):
    """The whole wire, in one test: A runs, calls B through `mcp serve`, gets
    B's structured result, and the Hive records B's run as A's child — then a
    chain that is already too deep is refused as data before B runs at all."""
    peer = tmp_path / "peer"
    construct.init_harness(peer, name="peer-harness", task="Answer the question.")
    construct.set_field(peer, "loop.require_verification", "false")
    # The child process shares this test's $HIVELOOM_HOME (forwarded by
    # _resolve_env), so trusting the folder here is what the child reads.
    trust.record_trust(peer)

    caller = tmp_path / "caller"
    construct.init_harness(caller, name="caller-harness", task="Delegate, then answer.")
    construct.set_field(caller, "loop.require_verification", "false")
    construct.set_field(caller, "mcp_servers", _peer_server_yaml(peer, "peer answered"))

    provider = _delegating_provider()
    result = runner.run_harness(caller, "go", provider=provider, literal_input=True)

    assert result.status == "success"
    # A read B's structured result, not a stringified error.
    relayed = json.dumps(provider.calls[1]["messages"][-1])
    assert "peer answered" in relayed
    assert "success" in relayed

    peer_identity = load_spec(harness_path(peer)).identity
    with Hive() as hive:
        children = hive.lineage(result.run_id)["forks"]
    assert [child["harness_key"] for child in children] == [peer_identity]

    # Same wire, one hop too far: this run enters already three deep, so the
    # peer refuses at depth 4 — as a readable result, and without running.
    deep_provider = _delegating_provider()
    deep = runner.run_harness(
        caller,
        "go",
        provider=deep_provider,
        literal_input=True,
        lineage={
            "kind": "delegation",
            "parent_run_id": "run_ancestor",
            "parent_harness_id": "hl-root",
            "depth": 3,
            "chain": ["hl-root", "hl-mid", "hl-near"],
        },
    )

    refused = json.dumps(deep_provider.calls[1]["messages"][-1])
    assert "delegation refused" in refused
    assert "max-depth 3" in refused
    with Hive() as hive:
        assert hive.lineage(deep.run_id)["forks"] == []


# --------------------------------------------------------------------------- #
# One process, many harnesses: credentials must not bleed between them
# --------------------------------------------------------------------------- #
def _harness_with_key(tmp_path: Path, name: str, variable: str, key: str) -> Path:
    directory = tmp_path / name
    construct.init_harness(directory, name=name, task="Do a thing.")
    (directory / ".env").write_text(f"{variable}={key}\n")
    return directory


def test_each_harness_gets_its_own_env_key_in_one_process(tmp_path: Path, monkeypatch):
    """`mcp serve --registered` hosts many harnesses in ONE process.

    `load_dotenv` adopted the first harness's key into `os.environ`, so every
    harness built afterwards silently ran on it — the second harness's own
    `.env` never got a look in, and its runs were billed to the first one's
    account.
    """
    from hiveloom import ext
    from hiveloom.models import claude as claude_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen: list[str | None] = []
    monkeypatch.setattr(
        claude_module, "ClaudeProvider", lambda api_key=None: seen.append(api_key)
    )

    first = _harness_with_key(tmp_path, "first", "ANTHROPIC_API_KEY", "sk-first")
    second = _harness_with_key(tmp_path, "second", "ANTHROPIC_API_KEY", "sk-second")

    ext.build_provider("claude", first)
    ext.build_provider("claude", second)

    assert seen == ["sk-first", "sk-second"]
    # ...and reading a harness's .env never wrote it into this process.
    import os

    assert "ANTHROPIC_API_KEY" not in os.environ


def test_openai_compatible_providers_do_not_bleed_keys_either(tmp_path: Path, monkeypatch):
    from hiveloom import ext
    from hiveloom.models import openai_compat

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: list[str | None] = []
    monkeypatch.setattr(
        openai_compat,
        "OpenAICompatProvider",
        lambda base_url, api_key=None: seen.append(api_key),
    )

    first = _harness_with_key(tmp_path, "one", "OPENAI_API_KEY", "sk-one")
    second = _harness_with_key(tmp_path, "two", "OPENAI_API_KEY", "sk-two")

    ext.build_provider("openai", first)
    ext.build_provider("openai", second)

    assert seen == ["sk-one", "sk-two"]


def test_the_process_environment_still_wins_over_a_harness_env(tmp_path: Path, monkeypatch):
    """Unchanged precedence: an exported key still overrides the harness file."""
    from hiveloom import ext
    from hiveloom.models import claude as claude_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-environment")
    seen: list[str | None] = []
    monkeypatch.setattr(
        claude_module, "ClaudeProvider", lambda api_key=None: seen.append(api_key)
    )

    harness = _harness_with_key(tmp_path, "h", "ANTHROPIC_API_KEY", "sk-from-the-file")
    ext.build_provider("claude", harness)

    assert seen == ["sk-from-the-environment"]


# --------------------------------------------------------------------------- #
# Caller cancellation: a graceful stop, not a kill
# --------------------------------------------------------------------------- #
_SLOW_TOOL = '''
from hiveloom.tools import tool


@tool(description="A trivial tool.")
def slow(value: str) -> str:
    return f"got {value}"
'''


class _BlockingProvider(FakeModelProvider):
    """Holds the first turn open long enough for the caller to give up."""

    started = threading.Event()

    def complete(self, **kwargs):
        if not _BlockingProvider.started.is_set():
            _BlockingProvider.started.set()
            time.sleep(1.5)
        return super().complete(**kwargs)


def test_caller_cancellation_stops_the_peer_run_at_the_next_turn(tmp_path: Path):
    """When the caller times out, the SDK cancels the handler and the run is
    on an abandoned worker thread: `mcp serve` turns that into a RunControl
    stop, so the peer finishes the turn in flight and stops instead of billing
    out a task nobody will read. It is not instant — hence the documented rule
    that `timeout_seconds` should cover the peer's whole run."""
    directory = tmp_path / "slow"
    construct.init_harness(directory, name="slow-harness", task="Do a slow thing.")
    construct.set_field(directory, "loop.require_verification", "false")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / "slow.py").write_text(_SLOW_TOOL)
    construct.add_tool(directory, code="tools/slow.py:slow", description="Slow.")
    _BlockingProvider.started = threading.Event()

    server = build_mcp_server(
        [directory],
        provider_factory=lambda: _BlockingProvider(
            [tool_response("slow", {"value": "x"}, call_id="c1"), text_response("done")]
        ),
    )

    async def drive() -> None:
        with anyio.move_on_after(0.5):
            await server.call_tool("run_slow-harness", {"input": "cancel me"})

    anyio.run(drive)

    identity = load_spec(harness_path(directory)).identity
    deadline = time.monotonic() + 20
    statuses: list[str] = []
    while time.monotonic() < deadline:
        with Hive() as hive:
            statuses = [r["status"] for r in hive.search_runs("cancel me", harness_key=identity)]
        if statuses:
            break
        time.sleep(0.2)

    assert statuses == ["stopped"]


def test_server_logging_is_plain_stderr_not_rich(tmp_path: Path):
    """The SDK installs a rich handler when a server is constructed; a boxed,
    line-wrapped 700-line traceback is unreadable in a parent agent's log."""
    import logging

    from hiveloom.serve.mcp import configure_stderr_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        # As in a real `mcp serve` process: nothing has configured logging yet,
        # so constructing the server is what installs the SDK's rich handler
        # (pytest's own capture handlers would otherwise make it a no-op).
        root.handlers = []
        build_mcp_server([_make_harness(tmp_path)])
        assert any(
            type(h).__module__.split(".")[0] == "rich" for h in root.handlers
        ), "the SDK no longer installs a rich handler; this guard can go"

        configure_stderr_logging()

        assert not any(
            type(h).__module__.split(".")[0] == "rich" for h in root.handlers
        )
        assert [h.stream for h in root.handlers] == [sys.stderr]
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
