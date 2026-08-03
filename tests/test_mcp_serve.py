"""Tests for `hiveloom mcp serve`: harnesses exposed as MCP tools."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import anyio
import pytest

from hiveloom.errors import SpecError
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.serve.mcp import _sanitize, build_mcp_server

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_SOURCE = "The quick brown fox jumps over the lazy dog. " * 20
_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)


def _make_harness(tmp_path: Path, name: str = "summarizer") -> Path:
    target = tmp_path / name
    shutil.copytree(EXAMPLE_HARNESS, target)
    return target


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
