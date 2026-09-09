"""The UI backend: identity, discovery, and the invariants its routes rest on.

``devtools/`` is not packaged and not importable as ``hiveloom.*``, so the module
is loaded from its path. What is tested here is the behavior a UI would get
wrong silently: a spec edit must never leave an unloadable file on disk, and an
untrusted harness must not run just because the caller is local.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "devtools" / "ui" / "server.py"
EXAMPLE = REPO_ROOT / "harnesses" / "example-summarizer"


def _load():
    spec = importlib.util.spec_from_file_location("hiveloom_ui_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ui = _load()


@pytest.fixture
def harness_copy(tmp_path: Path) -> Path:
    """A throwaway copy, so a test that writes a spec cannot damage the example."""
    import shutil

    target = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".hiveloom"))
    return target


@pytest.fixture
def client(harness_copy: Path, monkeypatch) -> TestClient:
    # An empty registry keeps the catalog to the one --dir harness, so tests do
    # not depend on what the developer running them happens to have registered.
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    monkeypatch.setenv("HIVELOOM_DB", str(harness_copy.parent / "ui-hive.db"))
    monkeypatch.setenv("HIVELOOM_UI_DB", str(harness_copy.parent / "workbench.db"))
    # The trust store is a real user-level file. Creating a harness records a
    # decision in it, so the tests get a home of their own rather than leaving
    # trust decisions behind on the machine that ran them.
    monkeypatch.setenv("HIVELOOM_HOME", str(harness_copy.parent / "hiveloom-home"))
    return TestClient(ui.build_app([str(harness_copy)]))


def test_slug_addresses_a_harness_by_name() -> None:
    assert ui._slug("Example Summarizer") == "example-summarizer"
    assert ui._slug("///") == "harness"


def test_catalog_lists_explicit_dirs(harness_copy: Path, monkeypatch) -> None:
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    found = ui._catalog([str(harness_copy)])
    assert found["example-summarizer"]["ok"] is True
    assert found["example-summarizer"]["explicit"] is True


def test_catalog_recursively_scans_a_harness_tree(tmp_path: Path, monkeypatch) -> None:
    from hiveloom import construct

    root = tmp_path / "harnesses"
    parent = root / "demo"
    nested_fork = parent / ".hiveloom" / "forks" / "probe"
    construct.init_harness(parent, name="demo", task="Parent task.")
    construct.init_harness(nested_fork, name="demo", task="Fork task.")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    found = ui._catalog([], [str(root)])

    assert set(found) == {"demo", "probe"}
    assert found["demo"]["path"] == str(parent.resolve())
    assert found["probe"]["path"] == str(nested_fork.resolve())


def test_scan_is_refreshed_on_each_request(tmp_path: Path, monkeypatch) -> None:
    from hiveloom import construct

    root = tmp_path / "harnesses"
    construct.init_harness(root / "one", name="one", task="First.")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    scan_client = TestClient(ui.build_app([], [str(root)]))
    assert [row["id"] for row in scan_client.get("/api/harnesses").json()["harnesses"]] == [
        "one"
    ]

    construct.init_harness(root / "two", name="two", task="Second.")
    assert [row["id"] for row in scan_client.get("/api/harnesses").json()["harnesses"]] == [
        "one",
        "two",
    ]


def test_broken_harness_is_a_row_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    """One unloadable harness must not cost the UI every other one."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "harness.yaml").write_text("name: [unclosed", encoding="utf-8")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    found = ui._catalog([str(broken)])
    assert found["broken"]["ok"] is False
    assert found["broken"]["error"]


def test_list_and_detail(client: TestClient) -> None:
    rows = client.get("/api/harnesses").json()["harnesses"]
    assert [r["id"] for r in rows] == ["example-summarizer"]

    detail = client.get("/api/harnesses/example-summarizer").json()
    first_spec_line = next(
        line for line in detail["yaml"].splitlines() if line.strip() and not line.startswith("#")
    )
    assert first_spec_line.startswith("schema_version:")
    assert detail["spec"]["name"] == "example-summarizer"


def test_unknown_harness_is_a_typed_404(client: TestClient) -> None:
    response = client.get("/api/harnesses/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_invalid_spec_is_refused_and_rolled_back(client: TestClient, harness_copy: Path) -> None:
    """The editor is free-form YAML; a bad save must not break the harness."""
    original = (harness_copy / "harness.yaml").read_text(encoding="utf-8")

    response = client.put(
        "/api/harnesses/example-summarizer/spec",
        json={"yaml": "name: broken\nthis is: not a harness\n"},
    )
    assert response.status_code == 400
    assert (harness_copy / "harness.yaml").read_text(encoding="utf-8") == original


def test_valid_spec_is_written(client: TestClient, harness_copy: Path) -> None:
    original = (harness_copy / "harness.yaml").read_text(encoding="utf-8")
    edited = original.replace(
        "Summarize a text file into a structured JSON summary.",
        "Summarize a text file. Edited by the UI.",
    )
    assert edited != original

    assert (
        client.put("/api/harnesses/example-summarizer/spec", json={"yaml": edited}).status_code
        == 200
    )
    assert "Edited by the UI" in (harness_copy / "harness.yaml").read_text(encoding="utf-8")


def test_untrusted_harness_will_not_run(client: TestClient, monkeypatch) -> None:
    """Being a local UI is not a reason to skip the trust gate."""
    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: False)

    response = client.post(
        "/api/harnesses/example-summarizer/run", json={"input": "summarize this"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "trust_required"


def test_run_requires_exactly_one_input_form(client: TestClient) -> None:
    both = client.post(
        "/api/harnesses/example-summarizer/run",
        json={"input": "a", "messages": [{"role": "user", "content": "b"}]},
    )
    assert both.status_code == 400
    assert client.post("/api/harnesses/example-summarizer/run", json={}).status_code == 400


def test_run_streams_ndjson(client: TestClient, monkeypatch) -> None:
    """The stream ends with a run_result frame, which is what the UI waits for."""

    class _Verdict:
        verifier, passed, feedback = "json", True, ""

    class _Result:
        status, output, turns = "success", "done", 1
        cost_usd, duration_seconds = 0.01, 0.5
        run_id, trace_path, reason = "run_test", "", ""
        artifacts: list = []
        verdicts = [_Verdict()]

    class _Event:
        def model_dump_json(self) -> str:
            return json.dumps({"type": "model_call", "seq": 0})

    def fake_run(_dir, _input, **kwargs):
        kwargs["on_event"](_Event())
        return _Result()

    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: True)
    monkeypatch.setattr(ui.runner_mod, "run_harness", fake_run)

    with client.stream(
        "POST", "/api/harnesses/example-summarizer/run", json={"input": "go"}
    ) as response:
        assert response.status_code == 200
        frames = [json.loads(line) for line in response.iter_lines() if line.strip()]

    # The stream opens by announcing the pre-allocated run id, so the UI can
    # address stop/steer/model/playbook before the first model call returns.
    assert frames[0]["type"] == "run_accepted"
    assert frames[0]["run_id"].startswith("run_")
    assert frames[1]["type"] == "model_call"
    assert frames[-1]["type"] == "run_result"
    assert frames[-1]["ok"] is True
    assert frames[-1]["verdicts"] == [{"verifier": "json", "passed": True, "feedback": ""}]


def test_run_passes_a_control_and_the_announced_id_to_the_runtime(
    client, monkeypatch
) -> None:
    """The announced id must be the id the run actually uses, or control 404s."""
    seen: dict = {}

    class _Result:
        status, output, turns = "success", "done", 1
        cost_usd, duration_seconds = 0.01, 0.5
        run_id, trace_path, reason = "run_test", "", ""
        artifacts: list = []
        verdicts: list = []

    def fake_run(_dir, _input, **kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: True)
    monkeypatch.setattr(ui.runner_mod, "run_harness", fake_run)

    with client.stream(
        "POST",
        "/api/harnesses/example-summarizer/run",
        json={"input": "go"},
    ) as response:
        frames = [json.loads(line) for line in response.iter_lines() if line.strip()]

    assert seen["run_id"] == frames[0]["run_id"]
    assert seen["control"] is not None


def test_catalog_endpoint_is_the_single_source_of_names(client: TestClient) -> None:
    """The UI must never carry its own list of tool or validator names."""
    catalog = client.get("/api/catalog").json()["catalog"]
    assert "tools" in catalog and "validators" in catalog
    assert any(entry["name"] == "file_read" for entry in catalog["tools"])


def test_trajectory_detail_exposes_native_debugger_evidence(
    client: TestClient, harness_copy: Path
) -> None:
    """The workbench reads integrity, context, lineage, and artifacts from the journal."""
    from hiveloom.logging.trace import TraceWriter, payload_hash

    messages = [{"role": "user", "content": "debug this run"}]
    writer = TraceWriter(
        harness_copy / ".hiveloom" / "traces",
        run_id="run_ui_debug",
        harness_name="example-summarizer",
        version_hash="abc123def456",
    )
    writer.emit("run_started", input="debug this run", policy="react", model="fake")
    writer.emit_context_system("You are a test harness.")
    writer.emit_context_tools([{"name": "file_read", "description": "Read a file"}])
    writer.emit("context_append", message=messages[0])
    call = writer.emit(
        "model_call",
        turn=0,
        phase="act",
        num_messages=1,
        context_head=writer.context_head,
        messages_hash=payload_hash(messages),
    )
    writer.emit(
        "model_response",
        turn=0,
        phase="act",
        text="done",
        stop_reason="end_turn",
        usage={"input_tokens": 12, "output_tokens": 1},
        cost_usd=0.0001,
    )
    writer.emit("verification_result", verifier="contains", passed=True, feedback="")
    writer.emit(
        "run_finished",
        status="success",
        reason="",
        turns=1,
        cost_usd=0.0001,
        duration_seconds=0.25,
        output="done",
        artifacts=[{"kind": "summary", "data": {"answer": "done"}}],
        model_path="fake:debug",
    )

    runs = client.get("/api/harnesses/example-summarizer/runs").json()["runs"]
    assert [run["run_id"] for run in runs] == ["run_ui_debug"]

    detail = client.get("/api/runs/run_ui_debug")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["integrity"]["ok"] is True
    assert payload["integrity"]["chained"] is True
    assert payload["integrity"]["checked"] == 8
    assert payload["fork_points"] == [
        {
            "seq": call.seq,
            "turn": 0,
            "phase": "act",
            "num_messages": 1,
            "timestamp": call.timestamp,
        }
    ]
    assert payload["run"]["verifications"][0]["verifier"] == "contains"
    assert payload["artifacts"] == [{"kind": "summary", "data": {"answer": "done"}}]
    assert payload["lineage"]["run"]["run_id"] == "run_ui_debug"

    context = client.get(f"/api/runs/run_ui_debug/context/{call.seq}")
    assert context.status_code == 200
    materialized = context.json()
    assert materialized["available"] is True
    assert materialized["faithful"] is True
    assert materialized["request"]["system"] == "You are a test harness."
    assert materialized["request"]["messages"] == messages
    assert materialized["request"]["tools"][0]["name"] == "file_read"


def test_context_endpoint_has_typed_missing_event(client: TestClient) -> None:
    response = client.get("/api/runs/no-such-run/context/12")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_context_endpoint_does_not_claim_legacy_empty_fold_is_exact(
    client: TestClient, harness_copy: Path
) -> None:
    from hiveloom.logging.trace import TraceWriter

    writer = TraceWriter(
        harness_copy / ".hiveloom" / "traces",
        run_id="run_ui_legacy",
        harness_name="example-summarizer",
        version_hash="legacy123456",
    )
    writer.emit("run_started", input="old run", policy="react", model="fake")
    call = writer.emit("model_call", turn=0, phase="act", num_messages=1)
    writer.emit(
        "run_finished",
        status="error",
        reason="legacy",
        turns=1,
        cost_usd=0,
        duration_seconds=0.1,
    )
    client.get("/api/harnesses/example-summarizer/runs")

    materialized = client.get(f"/api/runs/run_ui_legacy/context/{call.seq}").json()
    assert materialized["available"] is False
    assert materialized["faithful"] is False


# --------------------------------------------------------------------------- #
# Live run control
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_run(client, monkeypatch):
    """A run parked mid-flight, so control endpoints have something to address.

    The fake run blocks until released, which is the only way to exercise a
    surface whose whole point is acting on a run that has not finished.
    """
    import threading

    started = threading.Event()
    release = threading.Event()
    captured: dict = {}

    class _Result:
        status, output, turns = "stopped", "partial", 1
        cost_usd, duration_seconds = 0.0, 0.0
        run_id, trace_path, reason = "run_test", "", "stopped"
        artifacts: list = []
        verdicts: list = []

    def fake_run(_dir, _input, **kwargs):
        captured["control"] = kwargs["control"]
        captured["run_id"] = kwargs["run_id"]
        started.set()
        release.wait(timeout=5)
        return _Result()

    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: True)
    monkeypatch.setattr(ui.runner_mod, "run_harness", fake_run)

    frames: list = []
    stream_done = threading.Event()

    def consume():
        with client.stream(
            "POST", "/api/harnesses/example-summarizer/run", json={"input": "go"}
        ) as response:
            for line in response.iter_lines():
                if line.strip():
                    frames.append(json.loads(line))
        stream_done.set()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    assert started.wait(timeout=5), "the fake run never started"

    yield {
        "run_id": captured["run_id"],
        "control": captured["control"],
        "release": release,
        "frames": frames,
        "done": stream_done,
    }

    release.set()
    stream_done.wait(timeout=5)


def test_a_running_run_is_listed_and_addressable(client, live_run) -> None:
    assert live_run["run_id"] in client.get("/api/runs/live").json()["run_ids"]


def test_stop_reaches_the_runtime_control(client, live_run) -> None:
    response = client.post(
        f"/api/runs/{live_run['run_id']}/stop", json={"reason": "changed my mind"}
    )
    assert response.status_code == 200
    assert live_run["control"].stop_requested()
    assert live_run["control"].stop_reason == "changed my mind"


def test_steering_messages_can_be_queued_listed_edited_and_withdrawn(
    client, live_run
) -> None:
    run_id = live_run["run_id"]

    first = client.post(f"/api/runs/{run_id}/messages", json={"content": "exclude X"})
    second = client.post(f"/api/runs/{run_id}/messages", json={"content": "typo"})
    assert first.status_code == 200

    listed = client.get(f"/api/runs/{run_id}/messages").json()["messages"]
    assert [m["content"] for m in listed] == ["exclude X", "typo"]

    edited = client.patch(
        f"/api/runs/{run_id}/messages/{second.json()['id']}",
        json={"content": "actually include Y"},
    )
    assert edited.status_code == 200
    # An edit must not reorder the queue — the agent is told things in order.
    listed = client.get(f"/api/runs/{run_id}/messages").json()["messages"]
    assert [m["content"] for m in listed] == ["exclude X", "actually include Y"]

    assert client.delete(
        f"/api/runs/{run_id}/messages/{first.json()['id']}"
    ).status_code == 200
    remaining = client.get(f"/api/runs/{run_id}/messages").json()["messages"]
    assert [m["content"] for m in remaining] == ["actually include Y"]

    # And the loop still sees plain strings.
    assert live_run["control"].drain_messages() == ["actually include Y"]


def test_editing_a_delivered_message_is_reported_not_silently_ignored(
    client, live_run
) -> None:
    run_id = live_run["run_id"]
    queued = client.post(f"/api/runs/{run_id}/messages", json={"content": "late"}).json()
    live_run["control"].drain_messages()  # the loop consumed it

    response = client.patch(
        f"/api/runs/{run_id}/messages/{queued['id']}", json={"content": "too late"}
    )
    assert response.status_code == 404
    assert "already delivered" in response.json()["error"]["message"]


def test_model_and_playbook_switches_reach_the_control(client, live_run) -> None:
    run_id = live_run["run_id"]

    assert client.post(
        f"/api/runs/{run_id}/model", json={"model": "claude-opus-5", "reason": "stuck"}
    ).status_code == 200
    assert client.post(
        f"/api/runs/{run_id}/playbook", json={"name": "targeting"}
    ).status_code == 200

    assert live_run["control"].drain_model_switches() == [
        {"model": "claude-opus-5", "provider": None, "reason": "stuck"}
    ]
    assert live_run["control"].drain_playbook_switches() == [
        {"name": "targeting", "reason": ""}
    ]


def test_control_on_an_unknown_run_is_a_clean_404(client) -> None:
    response = client.post("/api/runs/run_nope/stop", json={})
    assert response.status_code == 404
    assert "not running in this process" in response.json()["error"]["message"]


def test_an_empty_steering_message_is_rejected(client, live_run) -> None:
    response = client.post(
        f"/api/runs/{live_run['run_id']}/messages", json={"content": "   "}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Fork and export
# --------------------------------------------------------------------------- #
def _real_run(harness_copy: Path) -> str:
    """A genuine finished run, so the journal is real and forkable."""
    from hiveloom import runner
    from hiveloom.models.fake import FakeModelProvider, text_response

    (harness_copy / "notes.txt").write_text("The quick brown fox. " * 40)
    result = runner.run_harness(
        harness_copy,
        "notes.txt",
        provider=FakeModelProvider([text_response("not json")]),
    )
    return result.run_id


def test_export_returns_the_journal_bytes_verbatim(client, harness_copy) -> None:
    """A re-serialized export would not verify against the hash chain."""
    from hiveloom.logging.hive import Hive

    run_id = _real_run(harness_copy)
    response = client.get(f"/api/runs/{run_id}/export")

    assert response.status_code == 200
    with Hive() as hive:
        on_disk = Path(hive.get_run(run_id)["trace_path"]).read_bytes()
    assert response.content == on_disk


def test_fork_creates_a_runnable_variant_inside_the_harness(
    client, harness_copy
) -> None:
    run_id = _real_run(harness_copy)

    response = client.post(f"/api/runs/{run_id}/fork", json={"name": "probe"})

    assert response.status_code == 200, response.text
    payload = response.json()
    directory = Path(payload["directory"])
    assert directory.parent == harness_copy / ".hiveloom" / "forks"
    assert (directory / "harness.yaml").is_file()
    assert (directory / "fork.yaml").is_file()
    assert payload["parent_run_id"] == run_id


def test_fork_with_a_model_override_rehashes_the_variant(client, harness_copy) -> None:
    from hiveloom.spec.loader import load_spec

    run_id = _real_run(harness_copy)

    response = client.post(
        f"/api/runs/{run_id}/fork", json={"name": "on-sonnet", "model": "claude-sonnet-5"}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model_override"]["model"] == "claude-sonnet-5"
    spec = load_spec(Path(payload["directory"]) / "harness.yaml")
    assert spec.model.id == "claude-sonnet-5"


def test_a_browser_supplied_fork_name_cannot_escape_the_directory(
    client, harness_copy
) -> None:
    """A fork writes files; the caller must never choose where."""
    run_id = _real_run(harness_copy)

    for name in ("../escape", "/etc/hiveloom", "a/b"):
        response = client.post(f"/api/runs/{run_id}/fork", json={"name": name})
        assert response.status_code == 400, name


def test_forking_an_unknown_run_is_a_clean_404(client) -> None:
    assert client.post("/api/runs/run_nope/fork", json={}).status_code == 404


def test_providers_report_key_presence_never_its_value(client: TestClient, monkeypatch) -> None:
    """The model directory is routinely read over someone's shoulder."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    body = client.get("/api/providers").json()
    claude = next(p for p in body["providers"] if p["name"] == "claude")
    assert claude["api_key_env"] == "ANTHROPIC_API_KEY"
    assert claude["api_key_set"] is True
    assert "sk-secret-value" not in json.dumps(body)


def test_a_key_in_the_harness_env_counts_as_set(client: TestClient, harness_copy: Path) -> None:
    """The provider factories read `<harness>/.env` before the environment.

    That file is where `hiveloom init` tells people to put the key, so a
    directory that only consulted `os.environ` called the one configured
    provider unconfigured — and the pickers, having nothing left to offer,
    fell back to offering every provider hiveloom knows.
    """
    (harness_copy / ".env").write_text("ANTHROPIC_API_KEY=sk-lives-in-the-harness\n")

    body = client.get("/api/providers?harness=example-summarizer").json()
    claude = next(p for p in body["providers"] if p["name"] == "claude")
    assert (claude["api_key_set"], claude["api_key_from"]) == (True, "harness")
    assert "sk-lives-in-the-harness" not in json.dumps(body)

    # Reading it must not load it: the API process's own environment is not a
    # place a GET gets to write to, and every other harness would inherit it.
    import os

    assert "ANTHROPIC_API_KEY" not in os.environ

    # And without the harness there is no .env to read, so the answer narrows
    # back to what the process itself can prove.
    plain = client.get("/api/providers").json()
    assert next(p for p in plain["providers"] if p["name"] == "claude")["api_key_set"] is False


def test_the_workbench_env_supplies_keys_to_every_harness(
    harness_copy: Path, monkeypatch, tmp_path: Path
) -> None:
    """`~/.hiveloom/.env` is the workbench's own credential, not a harness's.

    A run happens inside this process, so the file is adopted into the
    environment rather than consulted at call time — and never over a name that
    was already set, which would let a workbench file silently replace a key
    whoever started the API chose to export.
    """
    home = tmp_path / "hiveloom-home"
    home.mkdir()
    (home / ".env").write_text("ANTHROPIC_API_KEY=sk-workbench\n")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    monkeypatch.setenv("HIVELOOM_DB", str(tmp_path / "ui-hive.db"))
    monkeypatch.setenv("HIVELOOM_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = TestClient(ui.build_app([str(harness_copy)]))
    body = client.get("/api/providers?harness=example-summarizer").json()
    claude = next(p for p in body["providers"] if p["name"] == "claude")

    assert (claude["api_key_set"], claude["api_key_from"]) == (True, "workbench")
    assert "sk-workbench" not in json.dumps(body)


def test_a_harness_extension_provider_is_scoped_to_its_own_harness(
    harness_copy: Path, monkeypatch
) -> None:
    """One harness's extension must not put a provider in everyone's picker.

    The provider registry is process-global and the catalog loads every spec to
    fill the rail, so routing-lab's offline demo provider is registered the
    moment the rail is drawn. It is unrunnable anywhere else — nothing
    registers that id when another harness's spec is built — so the directory
    has to say which harness it belongs to rather than offering it to all.
    """
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    monkeypatch.setenv("HIVELOOM_DB", str(harness_copy.parent / "ui-hive.db"))
    monkeypatch.setenv("HIVELOOM_HOME", str(harness_copy.parent / "hiveloom-home"))
    routing_lab = REPO_ROOT / "harnesses" / "routing-lab"
    client = TestClient(ui.build_app([str(harness_copy), str(routing_lab)]))

    def demo(harness_id: str) -> dict:
        body = client.get(f"/api/providers?harness={harness_id}").json()
        return next(p for p in body["providers"] if p["name"] == "routing_lab")

    # Drawing the rail is what registers it, and it is registered for the whole
    # process — the directory is asked afterwards, exactly as the UI asks.
    assert client.get("/api/harnesses").status_code == 200

    assert demo("routing-lab")["available"] is True
    assert demo("example-summarizer")["available"] is False
    assert demo("example-summarizer")["scope"] == "harness"

    builtin = next(
        p for p in client.get("/api/providers").json()["providers"] if p["name"] == "claude"
    )
    assert (builtin["scope"], builtin["available"]) == ("global", True)


def test_set_model_moves_provider_and_id_together(client: TestClient, harness_copy: Path) -> None:
    """`model.provider` and `model.id` validate against each other, so a UI that
    wrote them one at a time would roll itself back every time."""
    before = client.get("/api/harnesses/example-summarizer").json()["spec"]["model"]

    response = client.put(
        "/api/harnesses/example-summarizer/model",
        json={"selector": "openai/gpt-4.1-mini"},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["ok"], body["provider"], body["id"]) == (True, "openai", "gpt-4.1-mini")

    after = client.get("/api/harnesses/example-summarizer").json()["spec"]["model"]
    assert (after["provider"], after["id"]) == ("openai", "gpt-4.1-mini")
    assert after != before
    # Everything the selector does not name survives the move.
    assert after["max_tokens"] == before["max_tokens"]
    assert after["temperature"] == before["temperature"]


def test_set_model_refuses_a_selector_without_a_provider(client: TestClient) -> None:
    response = client.put(
        "/api/harnesses/example-summarizer/model", json={"selector": "gpt-4.1-mini"}
    )
    assert response.status_code == 400
    assert "provider/model-id" in response.json()["error"]["message"]


def test_a_forks_name_does_not_replace_its_parent(tmp_path: Path, monkeypatch) -> None:
    """A fork copies the harness, name included — two folders, one name.

    Keying the catalog on the name alone would let the fork take the parent's
    place in the list, which looks exactly like the fork having vanished.
    """
    import shutil

    parent = tmp_path / "summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = tmp_path / "summarizer-turn2"
    shutil.copytree(parent, fork)

    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    found = ui._catalog([str(parent), str(fork)])

    assert len(found) == 2, "the fork replaced its parent instead of joining it"
    by_folder = {entry["folder"]: entry for entry in found.values()}
    assert set(by_folder) == {"summarizer", "summarizer-turn2"}
    # Both keep the harness's real name; only the id disambiguates them.
    assert {entry["name"] for entry in found.values()} == {"example-summarizer"}
    assert all(entry["id"] == key for key, entry in found.items())


def test_fork_ids_do_not_depend_on_registry_order(tmp_path: Path, monkeypatch) -> None:
    """The parent keeps its plain id however the registry happens to be sorted."""
    import shutil

    parent = tmp_path / "example-summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = tmp_path / "run-abc-turn2"
    shutil.copytree(parent, fork)
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    for order in ([str(parent), str(fork)], [str(fork), str(parent)]):
        found = ui._catalog(order)
        assert set(found) == {"example-summarizer", "run-abc-turn2"}
        assert found["example-summarizer"]["folder"] == "example-summarizer"
        assert found["run-abc-turn2"]["folder"] == "run-abc-turn2"


def test_a_catalog_row_says_which_harness_contains_it(tmp_path: Path, monkeypatch) -> None:
    """The rail nests a fork under its harness, so the row has to carry that.

    Containment is a fact about the path, which is why it is reported instead
    of being re-derived in the browser from the name: a fork starts out sharing
    its parent's name, but renaming it is an ordinary spec edit and two
    unrelated harnesses may share a name without being one harness.
    """
    import shutil

    parent = tmp_path / "example-summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = parent / ".hiveloom" / "forks" / "probe"
    fork.parent.mkdir(parents=True)
    shutil.copytree(parent, fork, ignore=shutil.ignore_patterns(".hiveloom"))
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    found = ui._catalog([str(parent), str(fork)])
    rows = {entry["folder"]: entry for entry in found.values()}

    assert rows["example-summarizer"]["is_fork"] is False
    assert rows["example-summarizer"]["parent_id"] == ""
    assert rows["probe"]["is_fork"] is True
    assert rows["probe"]["root_path"] == str(parent.resolve())
    assert rows["probe"]["parent_id"] == found_id(found, parent)


def found_id(catalog: dict, path: Path) -> str:
    return next(e["id"] for e in catalog.values() if e["path"] == str(path.resolve()))


def test_a_scan_finds_forks_and_relates_them_to_their_harness(
    tmp_path: Path, monkeypatch
) -> None:
    """`.hiveloom/forks` is where forks go, so a scan has to look inside it."""
    import shutil

    root = tmp_path / "harnesses"
    parent = root / "example-summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = parent / ".hiveloom" / "forks" / "probe"
    fork.parent.mkdir(parents=True)
    shutil.copytree(parent, fork, ignore=shutil.ignore_patterns(".hiveloom"))
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    found = ui._catalog([], [str(root)])

    forks = [entry for entry in found.values() if entry["is_fork"]]
    assert [entry["folder"] for entry in forks] == ["probe"]
    assert forks[0]["parent_id"] == found_id(found, parent)


def test_a_fork_of_an_unregistered_harness_claims_no_parent(
    tmp_path: Path, monkeypatch
) -> None:
    """A row pointing at a parent nothing lists would be a dead link in the rail."""
    import shutil

    parent = tmp_path / "example-summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = parent / ".hiveloom" / "forks" / "probe"
    fork.parent.mkdir(parents=True)
    shutil.copytree(parent, fork, ignore=shutil.ignore_patterns(".hiveloom"))
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    found = ui._catalog([str(fork)])

    entry = next(iter(found.values()))
    assert entry["is_fork"] is True
    assert entry["parent_id"] == ""


def test_a_fork_that_no_longer_loads_is_still_that_harnesss_fork(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil

    parent = tmp_path / "example-summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = parent / ".hiveloom" / "forks" / "probe"
    fork.mkdir(parents=True)
    (fork / "harness.yaml").write_text("name: [unclosed", encoding="utf-8")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    found = ui._catalog([str(parent), str(fork)])
    broken = next(e for e in found.values() if not e["ok"])

    assert broken["is_fork"] is True
    assert broken["parent_id"] == found_id(found, parent)


# --------------------------------------------------------------------------- #
# Comparison, evolution, resume
# --------------------------------------------------------------------------- #
def test_sessions_api_was_removed(client: TestClient) -> None:
    assert client.get("/api/sessions").status_code == 404


def test_run_rejects_removed_grouping_field(client: TestClient) -> None:
    response = client.post(
        "/api/harnesses/example-summarizer/run",
        json={"input": "go", "session_id": "old-client"},
    )
    assert response.status_code == 400
    assert "unknown fields" in response.json()["error"]["message"]


def _recorded_run(harness_copy: Path, task: str) -> str:
    from hiveloom import runner
    from hiveloom.models.fake import FakeModelProvider, text_response

    (harness_copy / "notes.txt").write_text("The quick brown fox. " * 40)
    return runner.run_harness(
        harness_copy,
        task,
        provider=FakeModelProvider([text_response("not json")]),
        literal_input=True,
    ).run_id


def test_compare_requires_both_sides(client, harness_copy) -> None:
    response = client.get("/api/harnesses/example-summarizer/compare", params={"left": "a"})
    assert response.status_code == 400


def test_compare_returns_both_sides_and_a_delta(client, harness_copy) -> None:
    _recorded_run(harness_copy, "one")
    from hiveloom.logging.hive import Hive

    with Hive() as hive:
        version = hive.get_run(_recorded_run(harness_copy, "two"))[
            "harness_version_hash"
        ]

    report = client.get(
        "/api/harnesses/example-summarizer/compare",
        params={"left": version, "right": "does-not-exist"},
    ).json()

    assert report["left"]["version"] == version
    assert report["right"]["runs"] == 0
    assert "delta" in report and "underpowered" in report


def test_proposals_list_is_empty_before_anything_is_proposed(client) -> None:
    response = client.get("/api/harnesses/example-summarizer/proposals")
    assert response.status_code == 200
    assert response.json()["proposals"] == []


def test_propose_reports_when_there_is_nothing_to_learn_from(client, harness_copy) -> None:
    """No failures means no proposal — and a reason, not an empty success."""
    response = client.post("/api/harnesses/example-summarizer/evolve/propose", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["changed"] is False
    assert "no failures" in payload["reason"]


def test_propose_from_parent_needs_a_fork(client) -> None:
    """The scoping only has a parent to fall back to on a fork."""
    response = client.post(
        "/api/harnesses/example-summarizer/evolve/propose", json={"from_parent": True}
    )
    assert response.status_code == 400
    assert "not a fork directory" in response.json()["error"]["message"]


def test_propose_from_parent_analyses_the_parent_version(client, harness_copy) -> None:
    """The workbench is where forks are made, so it is where the version
    scoping bites: a fresh fork has no runs of its own, and its evidence is the
    parent's. Asserted through the analysis call, so this fails if the endpoint
    ever goes back to scoping a fork to its own hash."""
    from hiveloom import fork as fork_mod

    fork_dir = harness_copy.parent / "probe"
    fork_dir.mkdir()
    (fork_dir / "harness.yaml").write_text(
        (harness_copy / "harness.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (fork_dir / fork_mod.FORK_FILE).write_text(
        "parent_run_id: run_parent\nparent_harness_version_hash: cafef00dcafe\n"
        "at_seq: 4\nat_turn: 0\n",
        encoding="utf-8",
    )

    seen: dict[str, str] = {}

    def _spy(hive, name, *, version=None, **kwargs):
        seen["version"] = version
        return ui.evolve_mod.FailureReport(harness_name=name, total_runs=0, success_rate=0.0)

    app = TestClient(ui.build_app([str(harness_copy), str(fork_dir)]))
    original, ui.evolve_mod.analyze = ui.evolve_mod.analyze, _spy
    try:
        response = app.post("/api/harnesses/probe/evolve/propose", json={"from_parent": True})
    finally:
        ui.evolve_mod.analyze = original

    assert response.status_code == 200, response.text
    assert seen["version"] == "cafef00dcafe"
    assert "cafef00dcafe" in response.json()["reason"]


def test_an_unknown_proposal_is_a_clean_404(client) -> None:
    assert client.get("/api/proposals/prop_nope").status_code == 404


def test_resume_refuses_a_directory_that_is_not_a_fork(client) -> None:
    response = client.post("/api/harnesses/example-summarizer/resume", json={})
    assert response.status_code == 400
    assert "not a fork directory" in response.json()["error"]["message"]


def test_resume_streams_the_fork_context(
    harness_copy: Path, monkeypatch
) -> None:
    from hiveloom import fork as fork_mod
    from hiveloom.logging.hive import Hive

    parent_run_id = _real_run(harness_copy)
    with Hive() as hive:
        trace_path = Path(hive.get_run(parent_run_id)["trace_path"])
    forked = fork_mod.create_fork(trace_path, harness_copy.parent / "resume-probe")
    seen: dict = {}

    class _Result:
        status, output, turns = "success", "resumed", 1
        cost_usd, duration_seconds = 0.0, 0.1
        run_id, trace_path, reason = "run_resumed", "", ""
        artifacts: list = []
        verdicts: list = []

    def fake_run(_dir, _input=None, **kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: True)
    monkeypatch.setattr(ui.runner_mod, "run_harness", fake_run)
    fork_client = TestClient(ui.build_app([str(forked.directory)]))

    with fork_client.stream(
        "POST",
        "/api/harnesses/example-summarizer/resume",
        json={},
    ) as response:
        frames = [json.loads(line) for line in response.iter_lines() if line.strip()]

    assert response.status_code == 200
    assert frames[0]["type"] == "run_accepted"
    assert frames[-1]["type"] == "run_result"
    assert seen["resume_messages"] == fork_mod.load_fork_context(forked.directory)
    assert seen["lineage"]["parent_run_id"] == parent_run_id


def test_harness_reports_the_version_a_run_would_record(client: TestClient) -> None:
    """The UI must see the version a new run will record."""
    before = client.get("/api/harnesses/example-summarizer").json()
    assert len(before["version_hash"]) >= 8

    client.put(
        "/api/harnesses/example-summarizer/model", json={"selector": "openai/gpt-4.1-mini"}
    )
    after = client.get("/api/harnesses/example-summarizer").json()
    assert after["version_hash"] != before["version_hash"], (
        "changing the model must move the version the next run records"
    )


def test_a_fork_carries_its_lineage_so_the_ui_need_not_read_the_folder_name(
    tmp_path: Path, monkeypatch
) -> None:
    """A fork folder name says nothing; the fork record says where it came from."""
    import shutil

    parent = tmp_path / "example-summarizer"
    shutil.copytree(EXAMPLE, parent, ignore=shutil.ignore_patterns(".hiveloom"))
    fork = tmp_path / "some-fork"
    shutil.copytree(parent, fork)
    (fork / "fork.yaml").write_text(
        "parent_run_id: run_abc123\nat_turn: 2\nat_seq: 17\n"
        "created_at: '2026-08-25T20:26:57+00:00'\nharness_version_hash: deadbeefcafe\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    found = ui._catalog([str(parent), str(fork)])

    assert found["example-summarizer"]["fork"] is None
    record = found["some-fork"]["fork"]
    assert record["parent_run_id"] == "run_abc123"
    assert record["at_turn"] == 2


# --------------------------------------------------------------------- #
# Version labels
# --------------------------------------------------------------------- #
def test_version_tags_round_trip_on_disk(client: TestClient, harness_copy: Path) -> None:
    """A label is a fact about the harness, so it lives with the harness.

    In ``.hiveloom/`` specifically: that is the one directory ``safe_path``
    refuses, so a running harness can neither read the labels its operator uses
    nor rewrite them.
    """
    assert client.get("/api/harnesses/example-summarizer/tags").json()["tags"] == {}

    response = client.put(
        "/api/harnesses/example-summarizer/tags",
        json={"version": "9b2c04e1a77c", "label": "baseline"},
    )
    assert response.status_code == 200
    assert response.json()["tags"] == {"9b2c04e1a77c": "baseline"}

    stored = json.loads((harness_copy / ".hiveloom" / "version_tags.json").read_text())
    assert stored == {"9b2c04e1a77c": "baseline"}
    assert client.get("/api/harnesses/example-summarizer/tags").json()["tags"] == stored


def test_an_empty_label_clears_the_tag(client: TestClient) -> None:
    client.put(
        "/api/harnesses/example-summarizer/tags", json={"version": "abc123", "label": "keep"}
    )
    cleared = client.put(
        "/api/harnesses/example-summarizer/tags", json={"version": "abc123", "label": ""}
    )
    assert cleared.json()["tags"] == {}


def test_run_alias_round_trips_on_disk_and_joins_the_run_list(
    client: TestClient, harness_copy: Path
) -> None:
    """An alias is display metadata: it lives beside the harness (never in the
    Hive, which a re-ingest rewrites) and rides along on every run row."""
    from hiveloom.logging.trace import TraceWriter

    writer = TraceWriter(
        harness_copy / ".hiveloom" / "traces",
        run_id="run_alias_target",
        harness_name="example-summarizer",
        version_hash="abc123def456",
    )
    writer.emit("run_started", input="name me")
    writer.emit("run_finished", status="success", turns=1, cost_usd=0.0, duration_seconds=0.1)

    runs = client.get("/api/harnesses/example-summarizer/runs").json()["runs"]
    assert runs, "the seeded run must appear in the list"
    run_id = runs[0]["run_id"]
    assert runs[0]["alias"] is None

    response = client.put(
        f"/api/harnesses/example-summarizer/runs/{run_id}/alias",
        json={"alias": "the hallucination repro"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "run_id": run_id, "alias": "the hallucination repro"}

    stored = json.loads((harness_copy / ".hiveloom" / "run_aliases.json").read_text())
    assert stored == {run_id: "the hallucination repro"}
    listed = client.get("/api/harnesses/example-summarizer/runs").json()["runs"]
    assert listed[0]["alias"] == "the hallucination repro"

    cleared = client.put(
        f"/api/harnesses/example-summarizer/runs/{run_id}/alias", json={"alias": ""}
    )
    assert cleared.json()["alias"] is None
    listed = client.get("/api/harnesses/example-summarizer/runs").json()["runs"]
    assert listed[0]["alias"] is None


def test_tag_endpoint_refuses_a_missing_version(client: TestClient) -> None:
    response = client.put("/api/harnesses/example-summarizer/tags", json={"label": "x"})
    assert response.status_code == 400


def test_unreadable_tag_file_is_no_tags_rather_than_a_crash(harness_copy: Path) -> None:
    """Workbench metadata must never be able to break the harness's own screens."""
    state = harness_copy / ".hiveloom"
    state.mkdir(exist_ok=True)
    (state / "version_tags.json").write_text("{not json", encoding="utf-8")
    assert ui._read_tags(str(harness_copy)) == {}


# --------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------- #
def test_upload_lands_in_the_workspace_and_returns_a_path(
    client: TestClient, harness_copy: Path
) -> None:
    """The browser sends bytes; the harness gets a path it can open itself."""
    import base64

    response = client.post(
        "/api/harnesses/example-summarizer/files",
        json={"name": "q3-deck.md", "content_base64": base64.b64encode(b"# Q3").decode()},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["path"] == "uploads/q3-deck.md"
    assert body["bytes"] == 4
    assert (harness_copy / "uploads" / "q3-deck.md").read_bytes() == b"# Q3"


def test_upload_cannot_escape_the_harness_directory(
    client: TestClient, harness_copy: Path
) -> None:
    """A filename is untrusted input, so it never reaches the filesystem intact."""
    import base64

    response = client.post(
        "/api/harnesses/example-summarizer/files",
        json={
            "name": "../../../etc/passwd",
            "content_base64": base64.b64encode(b"nope").decode(),
        },
    )
    assert response.status_code == 201
    # Reduced to one segment under uploads/ rather than refused-and-forgotten:
    # the write is contained, which is the property that matters.
    assert response.json()["path"] == "uploads/passwd"
    assert (harness_copy / "uploads" / "passwd").exists()
    assert not (harness_copy.parent / "etc").exists()


def test_upload_refuses_a_file_over_the_ceiling(client: TestClient) -> None:
    import base64

    oversized = base64.b64encode(b"x" * (ui._MAX_UPLOAD_BYTES + 1)).decode()
    response = client.post(
        "/api/harnesses/example-summarizer/files",
        json={"name": "big.bin", "content_base64": oversized},
    )
    assert response.status_code == 400
    assert "ceiling" in response.json()["error"]["message"]


def test_upload_refuses_content_that_is_not_base64(client: TestClient) -> None:
    response = client.post(
        "/api/harnesses/example-summarizer/files",
        json={"name": "x.txt", "content_base64": "not base64!!"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------- #
# Per-run model
# --------------------------------------------------------------------- #
def test_a_run_model_is_queued_as_a_swap_not_written_to_the_spec(
    client: TestClient, harness_copy: Path, monkeypatch
) -> None:
    """Running once on another model must not move the harness.

    The loop consumes queued switches at the top of the turn, *before* the
    first model call, so a switch queued here takes effect from turn 1 and is
    journalled like any other — while ``harness.yaml`` is left alone.
    """
    before = (harness_copy / "harness.yaml").read_text(encoding="utf-8")
    seen: dict = {}

    class _Result:
        status, output, turns = "success", "done", 1
        cost_usd, duration_seconds = 0.01, 0.5
        run_id, trace_path, reason = "run_test", "", ""
        artifacts: list = []
        verdicts: list = []

    def fake_run(_dir, _input, **kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: True)
    monkeypatch.setattr(ui.runner_mod, "run_harness", fake_run)

    with client.stream(
        "POST",
        "/api/harnesses/example-summarizer/run",
        json={"input": "go", "model": "openai/gpt-4.1-mini"},
    ) as response:
        assert response.status_code == 200
        list(response.iter_lines())

    queued = seen["control"].drain_model_switches()
    assert queued == [
        {
            "model": "gpt-4.1-mini",
            "provider": "openai",
            "reason": "run model, set in the workbench",
        }
    ]
    assert (harness_copy / "harness.yaml").read_text(encoding="utf-8") == before


def test_a_run_model_without_a_provider_is_refused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(ui.trust_mod, "is_trusted", lambda _path: True)
    response = client.post(
        "/api/harnesses/example-summarizer/run", json={"input": "go", "model": "gpt-4.1-mini"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------- #
# Trust on create
# --------------------------------------------------------------------- #
def test_creating_a_harness_can_leave_it_untrusted(client: TestClient, tmp_path: Path) -> None:
    """Trust is the caller's to grant. A workbench set to ask first must be able
    to say no, and the gate has to hear it."""
    target = tmp_path / "asked-first"
    response = client.post(
        "/api/harnesses",
        json={
            "directory": str(target),
            "name": "asked-first",
            "task": "Do a thing.",
            "trust": False,
        },
    )
    assert response.status_code == 201
    assert response.json()["trusted"] is False
    assert ui.trust_mod.is_trusted(str(target)) is False


def test_creating_a_harness_trusts_it_by_default(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "vouched"
    response = client.post(
        "/api/harnesses",
        json={"directory": str(target), "name": "vouched", "task": "Do a thing."},
    )
    assert response.status_code == 201
    assert response.json()["trusted"] is True
    assert ui.trust_mod.is_trusted(str(target)) is True


# --------------------------------------------------------------------- #
# Model settings
# --------------------------------------------------------------------- #
def test_model_settings_write_temperature_and_context_budget(
    client: TestClient, harness_copy: Path
) -> None:
    response = client.put(
        "/api/harnesses/example-summarizer/model",
        json={"temperature": 0.4, "max_input_tokens": 12345},
    )
    assert response.status_code == 200
    assert response.json()["temperature"] == 0.4
    assert response.json()["max_input_tokens"] == 12345

    spec = client.get("/api/harnesses/example-summarizer").json()["spec"]
    assert spec["model"]["temperature"] == 0.4
    assert spec["context"]["max_input_tokens"] == 12345


def test_an_empty_temperature_omits_the_field(client: TestClient) -> None:
    """Some models deprecate the field, so None is a real value here."""
    client.put("/api/harnesses/example-summarizer/model", json={"temperature": 0.7})
    response = client.put("/api/harnesses/example-summarizer/model", json={"temperature": ""})
    assert response.json()["temperature"] is None
    spec = client.get("/api/harnesses/example-summarizer").json()["spec"]
    assert spec["model"]["temperature"] is None


def test_model_settings_refuse_a_value_the_schema_rejects(
    client: TestClient, harness_copy: Path
) -> None:
    """A refused write leaves the file as it was — the commit is transactional."""
    before = (harness_copy / "harness.yaml").read_text(encoding="utf-8")
    response = client.put("/api/harnesses/example-summarizer/model", json={"temperature": 9.0})
    assert response.status_code == 400
    assert (harness_copy / "harness.yaml").read_text(encoding="utf-8") == before


def test_model_endpoint_refuses_an_empty_body(client: TestClient) -> None:
    response = client.put("/api/harnesses/example-summarizer/model", json={})
    assert response.status_code == 400
    assert "nothing to set" in response.json()["error"]["message"]


# --------------------------------------------------------------------- #
# Chat-first copilot and generated harness interfaces
# --------------------------------------------------------------------- #
def test_copilot_info_names_the_bundled_expert(client: TestClient) -> None:
    response = client.get("/api/copilot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "hiveloom-copilot"
    assert payload["version_hash"]
    assert any("Create a harness" in suggestion for suggestion in payload["suggestions"])


def test_conversations_persist_thread_selection_and_artifacts(
    client: TestClient, harness_copy: Path
) -> None:
    created = client.post("/api/conversations", json={})
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    messages = [
        {"role": "user", "content": "Why did this extraction fail?"},
        {
            "role": "assistant",
            "content": "The output failed its schema check.",
            "artifacts": [
                {"kind": "run_evidence", "data": {"run": {"run_id": "run_123"}}}
            ],
        },
    ]
    saved = client.put(
        f"/api/conversations/{conversation_id}",
        json={
            "messages": messages,
            "selection": {
                "harness_id": "example-summarizer",
                "run_id": "run_123",
            },
        },
    )

    assert saved.status_code == 200
    assert saved.json()["title"] == "Why did this extraction fail?"
    summary = client.get("/api/conversations").json()["conversations"][0]
    assert summary["id"] == conversation_id
    assert summary["message_count"] == 2
    assert summary["selection"]["harness_id"] == "example-summarizer"

    # Persistence belongs to the server-side workbench home, not one React
    # mount or TestClient instance.
    reloaded = TestClient(ui.build_app([str(harness_copy)]))
    record = reloaded.get(f"/api/conversations/{conversation_id}").json()
    assert record["messages"] == messages
    assert record["selection"]["run_id"] == "run_123"
    assert (harness_copy.parent / "workbench.db").read_bytes().startswith(
        b"SQLite format 3\x00"
    )


def test_conversation_delete_and_unknown_ids_are_typed(client: TestClient) -> None:
    conversation_id = client.post("/api/conversations", json={}).json()["id"]
    assert client.delete(f"/api/conversations/{conversation_id}").json() == {"ok": True}
    missing = client.get(f"/api/conversations/{conversation_id}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_memories_persist_with_global_and_harness_scope(
    client: TestClient, harness_copy: Path
) -> None:
    global_memory = client.post(
        "/api/memories", json={"content": "Prefer concise explanations."}
    )
    harness_memory = client.post(
        "/api/memories",
        json={
            "content": "The summary must name its source URL.",
            "harness_id": "example-summarizer",
        },
    )
    assert global_memory.status_code == 201
    assert harness_memory.status_code == 201
    assert global_memory.json()["scope"] == "global"
    assert harness_memory.json()["scope"] == "harness"

    assert [row["id"] for row in client.get("/api/memories").json()["memories"]] == [
        global_memory.json()["id"]
    ]
    scoped = client.get("/api/memories?harness=example-summarizer").json()["memories"]
    assert {row["id"] for row in scoped} == {
        global_memory.json()["id"],
        harness_memory.json()["id"],
    }

    reloaded = TestClient(ui.build_app([str(harness_copy)]))
    assert len(reloaded.get("/api/memories?harness=example-summarizer").json()["memories"]) == 2
    assert client.delete(f"/api/memories/{harness_memory.json()['id']}").json() == {
        "ok": True
    }


def test_copilot_memory_tools_use_the_selected_harness_scope(
    harness_copy: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    store = ui._MemoryStore(harness_copy.parent / "memory.db")

    def catalog():
        return ui._catalog([str(harness_copy)])

    service = ui._CopilotWorkbench(
        catalog=catalog,
        resolve=lambda harness_id: catalog()[harness_id],
        creation_root=harness_copy.parent,
        memory=store,
        selected_harness="example-summarizer",
    )
    global_memory = service.remember_memory("Use short answers.", "global")
    local_memory = service.remember_memory("Always retain citations.", "harness")

    recalled = service.recall_memories()
    assert {row["id"] for row in recalled["memories"]} == {
        global_memory["id"],
        local_memory["id"],
    }
    assert local_memory["harness_id"] == "example-summarizer"
    assert service.forget_memory(local_memory["id"])["ok"] is True


def test_copilot_chat_runs_the_expert_with_caller_owned_selection(
    client: TestClient, monkeypatch
) -> None:
    seen: dict = {}

    class _Result:
        status, output, turns = "success", "I inspected the selected harness.", 1
        cost_usd, duration_seconds = 0.02, 0.4
        run_id, trace_path, reason = "run_copilot", "", ""
        verdicts: list = []
        artifacts = [
            {
                "kind": "harness_contract",
                "tool": "inspect_harness",
                "data": {"name": "example-summarizer"},
            }
        ]

    def fake_run(directory, input_value, **kwargs):
        seen["directory"] = Path(directory)
        seen["input_value"] = input_value
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(ui.runner_mod, "run_harness", fake_run)
    with client.stream(
        "POST",
        "/api/copilot/chat",
        json={
            "messages": [{"role": "user", "content": "Inspect this harness"}],
            "selection": {"harness_id": "example-summarizer"},
        },
    ) as response:
        frames = [json.loads(line) for line in response.iter_lines() if line.strip()]

    assert response.status_code == 200
    assert seen["directory"].resolve() == ui._copilot_dir().resolve()
    assert seen["input_value"] is None
    assert seen["conversation"][0]["content"] == "Inspect this harness"
    selection = seen["context"]["workbench"].selection()
    assert selection["harness"]["id"] == "example-summarizer"
    assert frames[0]["type"] == "run_accepted"
    assert frames[-1]["artifacts"][0]["kind"] == "harness_contract"


def test_copilot_creates_a_valid_harness_through_construct(
    tmp_path: Path, monkeypatch
) -> None:
    from hiveloom.spec.loader import load_spec

    root = tmp_path / "harnesses"
    root.mkdir()
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))

    def catalog():
        return ui._catalog([], [str(root)])

    def resolve(harness_id: str):
        entry = catalog().get(harness_id)
        if entry is None:
            raise LookupError(harness_id)
        return entry

    service = ui._CopilotWorkbench(
        catalog=catalog,
        resolve=resolve,
        creation_root=root,
    )
    created = service.create_harness(
        name="Document facts",
        task="Extract facts from one document.",
        system_prompt="Extract only facts supported by the task input.",
        builtin_tools=["file_read"],
        output_schema_json=json.dumps(
            {
                "type": "object",
                "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
                "required": ["facts"],
            }
        ),
        max_turns=6,
    )

    directory = root / "document-facts"
    spec = load_spec(directory / "harness.yaml")
    assert created["id"] == "document-facts"
    assert spec.loop.max_turns == 6
    assert spec.loop.require_verification is True
    assert [tool.builtin for tool in spec.tools] == ["file_read"]
    assert spec.verify.validators[0].builtin == "output_schema"
    assert (directory / "schemas" / "output.json").is_file()
    assert ui.trust_mod.is_trusted(directory) is True


def test_copilot_reads_an_attached_file_through_safe_path(
    harness_copy: Path, monkeypatch
) -> None:
    attachment = harness_copy / "uploads" / "failure-case.md"
    attachment.parent.mkdir(exist_ok=True)
    attachment.write_text("# Failure case\nThe title was invented.\n", encoding="utf-8")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])

    def catalog():
        return ui._catalog([str(harness_copy)])

    service = ui._CopilotWorkbench(
        catalog=catalog,
        resolve=lambda harness_id: catalog()[harness_id],
        creation_root=harness_copy.parent,
        selected_harness="example-summarizer",
    )

    result = service.read_harness_file("uploads/failure-case.md")
    assert result["content"].startswith("# Failure case")
    assert result["path"] == "uploads/failure-case.md"

    with pytest.raises(ValueError, match="protected harness state"):
        service.read_harness_file(".hiveloom/traces/private.jsonl")


def test_copilot_creation_rolls_back_if_registration_cannot_commit(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "harnesses"
    root.mkdir()
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        ui.registry_mod,
        "register",
        lambda _directory: (_ for _ in ()).throw(OSError("registry unavailable")),
    )
    service = ui._CopilotWorkbench(
        catalog=lambda: ui._catalog([], [str(root)]),
        resolve=lambda harness_id: ui._resolve([], harness_id, [str(root)]),
        creation_root=root,
    )

    with pytest.raises(OSError, match="registry unavailable"):
        service.create_harness(
            name="Incomplete",
            task="This must not survive a partial commit.",
            system_prompt="Do the requested task.",
            builtin_tools=[],
            output_schema_json="",
            max_turns=4,
        )

    assert not (root / "incomplete").exists()


def test_generated_interface_is_persisted_and_preserves_template_like_text(
    client: TestClient, harness_copy: Path
) -> None:
    service = ui._CopilotWorkbench(
        catalog=lambda: ui._catalog([str(harness_copy)]),
        resolve=lambda harness_id: ui._resolve([str(harness_copy)], harness_id),
        creation_root=harness_copy.parent,
        selected_harness="example-summarizer",
    )
    artifact = service.create_interface(
        "",
        title="A __FIELD__ interface",
        input_label="Source document",
        submit_label="Summarize",
        input_kind="file",
    )

    assert artifact["contract"]["kind"] == "file"
    assert "<title>A __FIELD__ interface</title>" in artifact["html"]
    assert "contentBase64" in artifact["html"]
    assert "hiveloom:progress" in artifact["html"]
    assert "function requestId()" in artifact["html"]
    assert "async function execute()" in artifact["html"]
    assert "submit.addEventListener('click', requestRun)" in artifact["html"]
    assert "Enter a complete URL beginning with" in artifact["html"]
    assert "Powered by" not in artifact["html"]
    assert "pending = crypto.randomUUID()" not in artifact["html"]
    assert Path(artifact["path"]).is_file()
    response = client.get("/api/harnesses/example-summarizer/interface")
    assert response.status_code == 200
    assert response.json()["exists"] is True
    assert response.json()["sha256"] == artifact["sha256"]


# --------------------------------------------------------------------- #
# Shipping the workbench: where an installed copy reads from and writes to
# --------------------------------------------------------------------- #
#
# The workbench is distributed as `hiveloom-workbench`, so the same module runs
# from two very different places: this checkout, and a read-only site-packages
# directory shared between virtualenvs. Everything below is a rule that only
# breaks in the second case, which is exactly the case a developer never
# exercises by hand.


@pytest.fixture
def installed(tmp_path: Path, monkeypatch) -> Path:
    """Make the module believe it is an installed copy, not this checkout.

    `vite.config.ts` is the signal, and an installed package has none. Pointing
    `_PACKAGE_DIR` at a directory without one is the whole disguise.
    """
    package = tmp_path / "site-packages" / "hiveloom_workbench"
    package.mkdir(parents=True)
    monkeypatch.setattr(ui, "_PACKAGE_DIR", package)
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))
    return package


def test_a_checkout_is_recognised_as_a_checkout() -> None:
    assert ui._is_source_checkout()
    assert ui._workbench_home() == ui._PACKAGE_DIR / ".hiveloom"
    assert ui._copilot_dir() == ui._PACKAGE_DIR / "copilot"


def test_an_installed_workbench_keeps_its_state_under_the_hiveloom_home(
    installed: Path, tmp_path: Path
) -> None:
    # Never beside the module: site-packages is read-only on plenty of systems,
    # and is shared between environments on the rest.
    assert not ui._is_source_checkout()
    assert ui._workbench_home() == tmp_path / "home" / "workbench"
    assert installed not in ui._workbench_home().parents


def test_an_installed_copilot_is_materialized_where_it_can_journal(
    installed: Path, tmp_path: Path
) -> None:
    bundled = installed / "copilot"
    (bundled / "tools").mkdir(parents=True)
    (bundled / "harness.yaml").write_text("name: copilot\n", encoding="utf-8")
    (bundled / "tools" / "workbench.py").write_text("# tools\n", encoding="utf-8")

    working = ui._copilot_dir()

    assert working == tmp_path / "home" / "workbench" / "copilot"
    assert (working / "harness.yaml").read_text(encoding="utf-8") == "name: copilot\n"
    assert (working / "tools" / "workbench.py").read_text(encoding="utf-8") == "# tools\n"


def test_upgrading_the_copilot_keeps_the_journal_and_the_env(installed: Path) -> None:
    """An upgrade must replace what shipped and nothing else.

    The working copy accumulates the two things the distribution has no claim
    on: the journals a copilot run writes, and the key it runs with.
    """
    bundled = installed / "copilot"
    bundled.mkdir()
    (bundled / "harness.yaml").write_text("version: 1\n", encoding="utf-8")
    working = ui._copilot_dir()

    (working / ".env").write_text("ANTHROPIC_API_KEY=sk-local\n", encoding="utf-8")
    (working / ".hiveloom" / "traces").mkdir(parents=True)
    (working / ".hiveloom" / "traces" / "run.jsonl").write_text("{}\n", encoding="utf-8")

    (bundled / "harness.yaml").write_text("version: 2\n", encoding="utf-8")
    assert ui._copilot_dir() == working

    assert (working / "harness.yaml").read_text(encoding="utf-8") == "version: 2\n"
    assert (working / ".env").read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-local\n"
    assert (working / ".hiveloom" / "traces" / "run.jsonl").is_file()


# --------------------------------------------------------------------- #
# Serving the built interface
# --------------------------------------------------------------------- #


@pytest.fixture
def bundled_client(harness_copy: Path, installed: Path, monkeypatch) -> TestClient:
    """A workbench that carries a built frontend, as an installed one does."""
    web = installed / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (web / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    # The copilot is validated at startup and has to be a real harness.
    import shutil

    shutil.copytree(REPO_ROOT / "devtools" / "ui" / "copilot", installed / "copilot")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    monkeypatch.setenv("HIVELOOM_DB", str(harness_copy.parent / "ui-hive.db"))
    monkeypatch.setenv("HIVELOOM_UI_DB", str(harness_copy.parent / "workbench.db"))
    return TestClient(ui.build_app([str(harness_copy)]))


def test_the_built_interface_and_its_assets_are_served(bundled_client: TestClient) -> None:
    index = bundled_client.get("/")
    assert index.status_code == 200
    assert "<div id=root>" in index.text

    asset = bundled_client.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"


def test_a_client_route_falls_back_to_the_document(bundled_client: TestClient) -> None:
    # The app owns its own routing, so a deep link typed into the address bar
    # has no file behind it and must still load the page.
    deep = bundled_client.get("/harness/example-summarizer/runs")
    assert deep.status_code == 200
    assert "<div id=root>" in deep.text


def test_an_unknown_api_path_stays_a_json_404(bundled_client: TestClient) -> None:
    # The catch-all is declared last precisely so it cannot swallow the API. A
    # caller that mistypes a route must get an error, not a page.
    missing = bundled_client.get("/api/not-a-route")
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "not found"}


def test_the_api_still_answers_alongside_the_interface(bundled_client: TestClient) -> None:
    response = bundled_client.get("/api/harnesses")
    assert response.status_code == 200
    assert [h["id"] for h in response.json()["harnesses"]] == ["example-summarizer"]


def test_a_path_outside_the_bundle_is_not_served(
    bundled_client: TestClient, installed: Path
) -> None:
    (installed / "secret.txt").write_text("not for the browser", encoding="utf-8")
    # Encoded as well as literal: an HTTP client collapses `..` before it ever
    # leaves, so the literal form alone would test the client, not the server.
    for attempt in ("/assets/../../secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"):
        escaped = bundled_client.get(attempt)
        assert "not for the browser" not in escaped.text


def test_a_workbench_without_a_bundle_says_so_instead_of_failing(
    harness_copy: Path, installed: Path, monkeypatch
) -> None:
    """The API is useful on its own; a missing frontend is a packaging fault.

    It has to be reported as one, because the two ways out — run the dev
    servers, or reinstall — differ by how the workbench was obtained.
    """
    import shutil

    shutil.copytree(REPO_ROOT / "devtools" / "ui" / "copilot", installed / "copilot")
    monkeypatch.setattr(ui.registry_mod, "registered", lambda: [])
    monkeypatch.setenv("HIVELOOM_DB", str(harness_copy.parent / "ui-hive.db"))
    monkeypatch.setenv("HIVELOOM_UI_DB", str(harness_copy.parent / "workbench.db"))
    client = TestClient(ui.build_app([str(harness_copy)]))

    assert ui._web_root() is None
    page = client.get("/")
    assert page.status_code == 503
    assert "devtools/ui/dev.sh" in page.text
    assert "pip install --force-reinstall hiveloom-workbench" in page.text
    # The API is unaffected by the missing bundle.
    assert client.get("/api/harnesses").status_code == 200


def test_the_published_package_must_not_look_like_a_checkout(installed: Path) -> None:
    """The checkout signal has to be a file the npm package never ships.

    It cannot be `package.json`: the published package contains one, so every
    install would claim to be a working copy and write its database and its
    copilot into `node_modules` — disposable, and shared between projects.
    `vite.config.ts` is build-only and excluded from `files`, which the package
    build additionally refuses to ship.
    """
    (installed / "package.json").write_text('{"name": "hiveloom-workbench"}', encoding="utf-8")
    assert not ui._is_source_checkout()

    (installed / "vite.config.ts").write_text("export default {}", encoding="utf-8")
    assert ui._is_source_checkout()


def test_health_reports_identity_for_the_launcher(bundled_client: TestClient) -> None:
    # The Node launcher polls this to know the API is up, and compares `version`
    # with its own before opening a browser.
    body = bundled_client.get("/api/health").json()
    assert body == {
        "ok": True,
        "service": "hiveloom-workbench",
        "version": ui.__version__,
        "serves_web": True,
    }


def test_the_npm_package_manifest_ships_the_api_and_not_the_source() -> None:
    """`files` is what decides the tarball, so it is worth asserting directly.

    The package carries three things that must travel together — the compiled
    UI, the Python API, and the launcher — and must not carry the frontend
    source or the build config.
    """
    import json

    manifest = json.loads(
        (REPO_ROOT / "devtools" / "ui" / "package.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "hiveloom-workbench"
    assert manifest["bin"] == {"hiveloom-workbench": "bin/cli.mjs"}
    assert not manifest.get("private"), "a published package cannot be private"
    shipped = set(manifest["files"])
    assert {"bin/", "web/", "server.py", "copilot/"} <= shipped
    assert "src/" not in shipped and "vite.config.ts" not in shipped
    # The launcher and the API are one release; the runtime warns when they
    # disagree, and a mismatch here would ship that warning to every user.
    assert manifest["version"] == ui.__version__
