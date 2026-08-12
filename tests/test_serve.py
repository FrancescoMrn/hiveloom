"""Tests for `hiveloom serve` — the HTTP deployment interface."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hiveloom.errors import SpecError
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.provider import ModelResponse
from hiveloom.serve import HarnessServer

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)
# The example validator requires the summary to be shorter than the input.
_LONG_INPUT = "The quick brown fox jumps over the lazy dog. " * 5


def _make_harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 20)
    return target


def _success_script() -> list[ModelResponse]:
    return [
        tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
        text_response(_VALID_SUMMARY),
    ]


@pytest.fixture()
def server(tmp_path):
    """A served example harness backed by a scripted fake provider."""
    harness = _make_harness(tmp_path)
    providers: list[FakeModelProvider] = []

    def factory() -> FakeModelProvider:
        provider = FakeModelProvider(_success_script())
        providers.append(provider)
        return provider

    srv = HarnessServer(harness, port=0, provider_factory=factory)
    srv.providers = providers  # expose for assertions
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _url(srv: HarnessServer, path: str) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def _post(srv: HarnessServer, path: str, body: dict, headers: dict | None = None):
    request = urllib.request.Request(
        _url(srv, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    return urllib.request.urlopen(request)


def test_healthz_reports_identity(server):
    with urllib.request.urlopen(_url(server, "/healthz")) as response:
        payload = json.loads(response.read())
    assert payload["ok"] is True
    assert payload["name"] == "example-summarizer"
    assert payload["version_hash"]


def test_run_returns_cli_shaped_payload(server):
    with _post(server, "/runs", {"input": _LONG_INPUT}) as response:
        payload = json.loads(response.read())
    assert payload["ok"] is True
    assert payload["status"] == "success"
    assert payload["output"] == _VALID_SUMMARY
    assert payload["run_id"].startswith("run_")
    assert payload["cost_usd"] > 0


def test_run_input_is_literal_never_a_file_path(server):
    # "notes.txt" exists in the harness dir; over HTTP it must NOT be read.
    with _post(server, "/runs", {"input": "notes.txt"}) as response:
        json.loads(response.read())  # short literal input fails verify; irrelevant here
    first_message = server.providers[-1].calls[0]["messages"][0]
    assert first_message["content"] == "notes.txt"


def test_stream_emits_events_then_run_result_last(server):
    with _post(server, "/runs", {"input": _LONG_INPUT, "stream": True}) as response:
        assert response.headers["Content-Type"] == "application/x-ndjson"
        lines = [json.loads(line) for line in response.read().splitlines()]
    types = [line["type"] for line in lines]
    assert types[0] == "run_accepted"
    assert lines[0]["run_id"].startswith("run_")
    assert types[1] == "run_started"
    assert "tool_call" in types
    assert types[-1] == "run_result"
    assert lines[-1]["status"] == "success"
    assert lines[-1]["run_id"] == lines[0]["run_id"]


def test_bad_body_is_400_and_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server, "/runs", {"nope": 1})
    assert err.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(_url(server, "/missing"))
    assert err.value.code == 404


def test_api_key_enforced_when_configured(tmp_path):
    harness = _make_harness(tmp_path)
    srv = HarnessServer(
        harness,
        port=0,
        api_key="sekret",
        provider_factory=lambda: FakeModelProvider(_success_script()),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(srv, "/runs", {"input": "go"})
        assert err.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(srv, "/runs", {"input": "go"}, headers={"X-API-Key": "wrong"})
        assert err.value.code == 401
        # healthz stays open for orchestrator probes even with a key set.
        with urllib.request.urlopen(_url(srv, "/healthz")) as response:
            assert json.loads(response.read())["ok"] is True
        for headers in ({"Authorization": "Bearer sekret"}, {"X-API-Key": "sekret"}):
            with _post(srv, "/runs", {"input": _LONG_INPUT}, headers=headers) as response:
                assert json.loads(response.read())["status"] == "success"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_saturated_server_answers_429(tmp_path):
    harness = _make_harness(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeModelProvider):
        def complete(self, **kwargs):
            started.set()
            assert release.wait(timeout=10)
            return super().complete(**kwargs)

    srv = HarnessServer(
        harness,
        port=0,
        concurrency=1,
        provider_factory=lambda: BlockingProvider(_success_script()),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        results: list[int] = []

        def first_run() -> None:
            with _post(srv, "/runs", {"input": "go"}) as response:
                results.append(response.status)

        runner_thread = threading.Thread(target=first_run, daemon=True)
        runner_thread.start()
        assert started.wait(timeout=10)
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(srv, "/runs", {"input": "go"})
        assert err.value.code == 429
        assert err.value.headers["Retry-After"]
        release.set()
        runner_thread.join(timeout=10)
        assert results == [200]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_untrusted_harness_refuses_to_start(tmp_path, monkeypatch):
    harness = _make_harness(tmp_path)
    monkeypatch.setenv("HIVELOOM_TRUST", "never")
    with pytest.raises(SpecError):
        HarnessServer(harness, port=0)


def test_bad_concurrency_rejected(tmp_path):
    harness = _make_harness(tmp_path)
    with pytest.raises(SpecError):
        HarnessServer(harness, port=0, concurrency=0)


# --------------------------------------------------------------------------- #
# Conversation input over HTTP
# --------------------------------------------------------------------------- #
THREAD = [
    {"role": "user", "content": "Summarize the notes."},
    {"role": "assistant", "content": "Which part?"},
    {"role": "user", "content": _LONG_INPUT},
]


def test_runs_accepts_a_whole_conversation(server):
    with _post(server, "/runs", {"messages": THREAD}) as response:
        payload = json.loads(response.read())
    assert payload["ok"] is True

    sent = server.providers[0].calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "Summarize the notes."


def test_runs_rejects_both_or_neither_input_form(server):
    for body in ({}, {"input": "x", "messages": THREAD}, {"messages": []}):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, "/runs", body)
        assert excinfo.value.code == 400
        assert "exactly one" in json.loads(excinfo.value.read())["error"]


def test_a_malformed_conversation_is_a_422_not_a_500(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(server, "/runs", {"messages": [{"role": "assistant", "content": "hi"}]})
    assert excinfo.value.code == 422
    assert "last message must be from the user" in json.loads(excinfo.value.read())["error"]


def test_served_runs_return_artifacts(tmp_path):
    """A served harness must be able to drive a UI, not just return text."""
    from hiveloom import construct

    harness = tmp_path / "art"
    construct.init_harness(harness, name="art", task="Emit an artifact.")
    (harness / "tools").mkdir(exist_ok=True)
    (harness / "tools" / "chart.py").write_text(
        "from hiveloom.tools import Artifact, ToolResult, tool\n\n\n"
        '@tool(description="Register a chart.")\n'
        "def chart(title: str) -> ToolResult:\n"
        "    return ToolResult(content='ok', "
        "artifacts=[Artifact(kind='chart', data={'title': title})])\n"
    )
    construct.add_tool(harness, code="tools/chart.py:chart", description="Chart.")

    srv = HarnessServer(
        harness,
        port=0,
        provider_factory=lambda: FakeModelProvider(
            [tool_response("chart", {"title": "T"}, call_id="c1"), text_response("done")]
        ),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with _post(srv, "/runs", {"input": "go"}) as response:
            payload = json.loads(response.read())
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)

    assert payload["artifacts"] == [
        {"kind": "chart", "data": {"title": "T"}, "tool": "chart"}
    ]
