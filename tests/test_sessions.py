"""Session-grouped traces: runs of one conversation read together.

A ``session_id`` puts a run's trace under ``<trace_dir>/<session_id>/`` and
stamps every event with the id; the Hive ingests nested traces the same as
flat ones, so nothing downstream changes.
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
from hiveloom.models.fake import FakeModelProvider, text_response
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


def _run(harness: Path, session_id: str | None = None, ingest: bool = False):
    return runner.run_harness(
        harness,
        _LONG_INPUT,
        provider=FakeModelProvider([text_response(_VALID_SUMMARY)]),
        session_id=session_id,
        ingest=ingest,
    )


def test_session_runs_group_in_one_directory(tmp_path):
    harness = _make_harness(tmp_path)
    first = _run(harness, session_id="thread-abc123")
    second = _run(harness, session_id="thread-abc123")
    other = _run(harness, session_id="thread-zzz")
    flat = _run(harness)

    traces = harness / ".hiveloom" / "traces"
    grouped = sorted(p.name for p in (traces / "thread-abc123").glob("*.jsonl"))
    assert grouped == sorted([f"{first.run_id}.jsonl", f"{second.run_id}.jsonl"])
    assert (traces / "thread-zzz" / f"{other.run_id}.jsonl").is_file()
    # without a session the layout is unchanged
    assert (traces / f"{flat.run_id}.jsonl").is_file()


def test_events_carry_the_session_id(tmp_path):
    harness = _make_harness(tmp_path)
    result = _run(harness, session_id="thread-abc123")
    trace_file = (
        harness / ".hiveloom" / "traces" / "thread-abc123" / f"{result.run_id}.jsonl"
    )
    events = [json.loads(line) for line in trace_file.read_text().splitlines()]
    assert events and all(e["session_id"] == "thread-abc123" for e in events)


def test_invalid_session_id_is_rejected(tmp_path):
    harness = _make_harness(tmp_path)
    with pytest.raises(ValueError, match="session_id"):
        _run(harness, session_id="../escape")


def test_hive_ingests_session_grouped_traces(tmp_path):
    from hiveloom.logging.hive import Hive

    harness = _make_harness(tmp_path)
    grouped = _run(harness, session_id="thread-abc123")
    flat = _run(harness)
    with Hive(tmp_path / "hive.db") as hive:
        count = hive.ingest_dir(harness / ".hiveloom" / "traces")
    assert count == 2, (grouped.run_id, flat.run_id)


# ─── over HTTP ───────────────────────────────────────────────────────────────

@pytest.fixture()
def server(tmp_path):
    harness = _make_harness(tmp_path)
    srv = HarnessServer(
        harness, port=0,
        provider_factory=lambda: FakeModelProvider([text_response(_VALID_SUMMARY)]),
    )
    srv.harness_dir = harness
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _post(srv, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}/runs",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request)


def test_serve_groups_traces_by_session(server):
    with _post(server, {"input": _LONG_INPUT, "session_id": "chat-42"}) as response:
        payload = json.loads(response.read())
    trace_file = (
        server.harness_dir / ".hiveloom" / "traces" / "chat-42"
        / f"{payload['run_id']}.jsonl"
    )
    assert trace_file.is_file()


def test_serve_rejects_bad_session_id(server):
    for bad in ("../etc", "a/b", "x" * 65, 7):
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(server, {"input": _LONG_INPUT, "session_id": bad})
        assert err.value.code == 400
