"""Run control: stopping a run gracefully and steering it mid-flight.

The loop consumes both signals at its turn boundary — never mid-model-call —
so a stopped run is a *completed* run (status ``"stopped"``, trace intact),
and a steering message lands as an operator message before the next model call.
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hiveloom import runner
from hiveloom.loop.control import RunControl
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.serve import HarnessServer

EXAMPLE_HARNESS = Path(__file__).resolve().parents[1] / "harnesses" / "example-summarizer"

_VALID_SUMMARY = json.dumps(
    {"title": "Fox", "summary": "A fox jumps a dog.", "key_points": ["fox", "dog"]}
)
_LONG_INPUT = "The quick brown fox jumps over the lazy dog. " * 5


def _make_harness(tmp_path: Path) -> Path:
    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE_HARNESS, target)
    (target / "notes.txt").write_text("The quick brown fox jumps over the lazy dog. " * 20)
    return target


# ─── loop-level ──────────────────────────────────────────────────────────────

def test_pre_stopped_control_finishes_without_a_model_call(tmp_path):
    harness = _make_harness(tmp_path)
    control = RunControl()
    control.request_stop("operator changed their mind")
    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
    result = runner.run_harness(
        harness, _LONG_INPUT, provider=provider, control=control, ingest=False
    )
    assert result.status == "stopped"
    assert result.reason == "operator changed their mind"
    assert result.turns == 0, "stop must be honoured before the first model call"


def test_steering_message_reaches_the_next_model_call(tmp_path):
    harness = _make_harness(tmp_path)
    control = RunControl()
    control.send_message("Concentrati solo sulla prima frase.")
    provider = FakeModelProvider(
        [tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
         text_response(_VALID_SUMMARY)]
    )
    result = runner.run_harness(
        harness, _LONG_INPUT, provider=provider, control=control, ingest=False
    )
    assert result.status == "success"
    first_call_texts = json.dumps(provider.calls[0]["messages"])
    assert "Concentrati solo sulla prima frase." in first_call_texts
    assert "Operator message received" in first_call_texts


def test_stop_between_turns_preserves_partial_state(tmp_path):
    harness = _make_harness(tmp_path)
    control = RunControl()

    class StopAfterFirstCall(FakeModelProvider):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            control.request_stop()
            return response

    provider = StopAfterFirstCall(
        [tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
         text_response(_VALID_SUMMARY)]
    )
    result = runner.run_harness(
        harness, _LONG_INPUT, provider=provider, control=control, ingest=False
    )
    assert result.status == "stopped"
    assert result.turns == 1, "the stop lands at the boundary after the in-flight turn"


def test_run_id_override_is_used(tmp_path):
    harness = _make_harness(tmp_path)
    provider = FakeModelProvider([text_response(_VALID_SUMMARY)])
    result = runner.run_harness(
        harness, _LONG_INPUT, provider=provider, run_id="run_fixed4testing00",
        ingest=False,
    )
    assert result.run_id == "run_fixed4testing00"


# ─── serve-level ─────────────────────────────────────────────────────────────

@pytest.fixture()
def server(tmp_path):
    harness = _make_harness(tmp_path)
    srv = HarnessServer(
        harness, port=0,
        provider_factory=lambda: FakeModelProvider(
            [tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
             text_response(_VALID_SUMMARY)]
        ),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _post(srv, path, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request)


def test_stop_and_messages_for_unknown_run_are_404(server):
    for action, body in (("stop", {}), ("messages", {"content": "hi"})):
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(server, f"/runs/run_doesnotexist00/{action}", body)
        assert err.value.code == 404


def test_message_without_content_is_400(server, tmp_path):
    # Register a control by hand to reach the validation branch.
    control = RunControl()
    server.register_control("run_live0000000000", control)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(server, "/runs/run_live0000000000/messages", {})
        assert err.value.code == 400
        with _post(
            server, "/runs/run_live0000000000/messages", {"content": "escludi i test"}
        ) as response:
            assert json.loads(response.read())["queued_for_next_turn"] is True
        assert control.drain_messages() == ["escludi i test"]
    finally:
        server.release_control("run_live0000000000")


def test_streamed_run_can_be_stopped_by_its_announced_id(tmp_path):
    harness = _make_harness(tmp_path)
    release = threading.Event()

    class BlockingProvider(FakeModelProvider):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            release.wait(timeout=10)
            return response

    srv = HarnessServer(
        harness, port=0,
        provider_factory=lambda: BlockingProvider(
            [tool_response("file_read", {"path": "notes.txt"}, call_id="c1"),
             text_response(_VALID_SUMMARY)]
        ),
        concurrency=2,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        response = _post(srv, "/runs", {"input": _LONG_INPUT, "stream": True})
        first = json.loads(response.readline())
        assert first["type"] == "run_accepted"
        run_id = first["run_id"]

        with _post(srv, f"/runs/{run_id}/stop", {"reason": "basta così"}) as stop_resp:
            assert json.loads(stop_resp.read())["stopping"] is True
        release.set()

        lines = [json.loads(line) for line in response.read().splitlines() if line]
        final = lines[-1]
        assert final["type"] == "run_result"
        assert final["status"] == "stopped"
        assert final["reason"] == "basta così"

        # the control is released once the run finished
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(srv, f"/runs/{run_id}/stop", {})
        assert err.value.code == 404
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
