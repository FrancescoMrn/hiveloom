"""The stateless worker: a harness arrives whole, the job is done, nothing is kept."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from hiveloom.generate.llm import FakeStrongModel
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.serve.worker import WorkerServer

KEY = "worker-key"
LESSON = json.dumps(
    {
        "lessons": [
            {
                "kind": "rule",
                "title": "Answer with one JSON object",
                "content": "Return a single JSON object and nothing else.",
                "evidence": "the regex validator rejected prose",
            }
        ]
    }
)
PROPOSAL = json.dumps(
    {
        "rationale": "the runs ran out of turns",
        "target": {"signal": "success_rate", "expect": "increase"},
        "yaml_changes": [{"path": "loop.max_turns", "value": 25}],
    }
)


@pytest.fixture()
def worker(monkeypatch):
    for var in ("HIVELOOM_HOME", "HIVELOOM_DB"):
        if var in os.environ:
            monkeypatch.setenv(var, os.environ[var])
        else:
            monkeypatch.delenv(var, raising=False)
    scripts: list[list] = []
    strong = FakeStrongModel([PROPOSAL, LESSON, LESSON])
    srv = WorkerServer(
        port=0,
        api_key=KEY,
        provider_factory=lambda: FakeModelProvider(scripts.pop(0)),
        strong_model=strong,
    )
    srv.scripts = scripts
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def call(srv, path, body, key=KEY):
    request = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            text = response.read().decode()
            return response.status, text
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def made(srv, ops=None):
    status, text = call(
        srv,
        "/harness/create",
        {
            "name": "summarizer",
            "task": "Answer in JSON.",
            "ops": ops
            or [{"op": "add", "kind": "validator", "builtin": "regex_match", "pattern": r"^\{"}],
        },
    )
    assert status == 200, text
    return json.loads(text)


def test_create_builds_through_the_construction_api_and_carries_no_code(worker):
    out = made(
        worker,
        [
            {"op": "set", "path": "loop.max_turns", "value": 4},
            {"op": "write", "path": "schemas/output.json", "content": "{}"},
            {
                "op": "add",
                "kind": "validator",
                "builtin": "output_schema",
                "schema_file": "schemas/output.json",
            },
        ],
    )
    files = out["bundle"]["files"]
    assert "harness.yaml" in files and "schemas/output.json" in files
    assert "pyproject.toml" not in files and not any(p.endswith(".py") for p in files)
    assert "max_turns: 4" in files["harness.yaml"]
    assert out["version_hash"]


def test_an_op_that_does_not_hold_applies_nothing_and_code_is_never_added(worker):
    bundle = made(worker)["bundle"]
    status, text = call(
        worker,
        "/harness/construct",
        {
            "bundle": bundle,
            "ops": [
                {"op": "set", "path": "loop.max_turns", "value": 3},
                {"op": "set", "path": "nonsense.field", "value": 1},
            ],
        },
    )
    assert status == 422
    status, text = call(
        worker,
        "/harness/construct",
        {"bundle": bundle, "ops": [{"op": "add", "kind": "tool", "code": "tools/x.py:run"}]},
    )
    assert status == 422 and "never adds code" in text
    status, text = call(
        worker,
        "/harness/validate",
        {"bundle": {"files": {**bundle["files"], "tools/x.py": "import os"}}},
    )
    assert status == 200 and json.loads(text)["ok"] is False


def test_every_call_needs_the_key(worker):
    assert call(worker, "/harness/create", {"name": "x", "task": "y"}, key=None)[0] == 401
    assert call(worker, "/harness/create", {"name": "x", "task": "y"}, key="wrong")[0] == 401


def test_a_run_returns_its_journal_and_leaves_nothing_behind(worker):
    bundle = made(worker)["bundle"]
    worker.scripts.append([text_response('{"answer": 1}')])
    secret = "Bearer reader-7f3a"
    status, text = call(
        worker, "/runs", {"bundle": bundle, "input": "go", "context": {"reader_token": secret}}
    )
    assert status == 200, text
    out = json.loads(text)
    assert out["status"] == "success" and out["output"] == '{"answer": 1}'
    assert out["journal"] and out["journal"][0]["type"] == "run_started" and out["version_hash"]
    assert secret not in text
    assert not [p for p in worker.home.iterdir() if p.name.startswith("hiveloom-worker-")]


def test_a_streamed_run_ends_on_its_result_with_the_journal(worker):
    bundle = made(worker)["bundle"]
    worker.scripts.append([text_response('{"answer": 1}')])
    status, text = call(
        worker,
        "/runs",
        {"bundle": bundle, "messages": [{"role": "user", "content": "go"}], "stream": True},
    )
    frames = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert (
        status == 200 and frames[0]["type"] == "run_accepted" and frames[-1]["type"] == "run_result"
    )
    assert frames[-1]["status"] == "success" and frames[-1]["journal"]


def test_the_night_drafts_a_gated_proposal_and_lessons_from_the_journals_and_applies_one_by_hand(
    worker,
):
    bundle = made(worker)["bundle"]
    worker.scripts.append(
        [
            text_response("prose"),
            text_response("prose"),
            text_response("prose"),
            text_response("prose"),
        ]
    )
    failed = json.loads(call(worker, "/runs", {"bundle": bundle, "input": "go"})[1])
    assert failed["status"] == "verify_failed"
    status, text = call(
        worker, "/evolve", {"bundle": bundle, "journals": [failed["journal"]], "reflect": True}
    )
    assert status == 200, text
    proposals = json.loads(text)["proposals"]
    triggers = {p["trigger"] for p in proposals}
    assert "night" in triggers and "reflect" in triggers
    lesson = next(p for p in proposals if p["trigger"] == "reflect")
    assert lesson["changes"][0]["path"] == "memory.entries.+"
    status, text = call(worker, "/proposals/apply", {"bundle": bundle, "proposal": lesson})
    assert status == 200, text
    assert "Answer with one JSON object" in json.loads(text)["bundle"]["files"]["harness.yaml"]
    # a frozen field stays frozen, whoever proposes it
    status, text = call(
        worker,
        "/proposals/apply",
        {
            "bundle": bundle,
            "proposal": {
                "rationale": "x",
                "yaml_changes": [{"path": "model.id", "value": "another-model"}],
            },
        },
    )
    assert status == 422


def test_nothing_to_evolve_is_said(worker):
    bundle = made(worker)["bundle"]
    status, text = call(worker, "/evolve", {"bundle": bundle, "journals": []})
    assert status == 200 and json.loads(text) == {
        "proposals": [],
        "note": "nothing to evolve — no failures recorded for the current version",
    }
