"""Tests for the hiveloom HTTP control plane (`hiveloom.serve.app`).

Offline, no network, no real port: `starlette.testclient.TestClient` only.
Model calls are scripted with `FakeModelProvider`/`FakeStrongModel`.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from starlette.testclient import TestClient

# Reuse the Hive failure-seeding helper and a scripted proposal payload from
# the evolve tests (tests dir is on sys.path) — the established cross-test-file
# pattern in this suite (test_proposals.py does the same from test_evolve).
from test_evolve import _PROPOSAL_PAYLOAD, _seed_failure

from hiveloom import construct
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.models.fake import FakeModelProvider, ModelProvider, text_response
from hiveloom.serve import auth as auth_mod
from hiveloom.serve import keys as keys_mod
from hiveloom.serve.app import create_app
from hiveloom.spec.loader import load_spec


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _harness(tmp_path: Path, *, name: str = "srv") -> Path:
    """A harness whose single validator deterministically passes/fails on one
    model call: `on_fail.max_retries=0` means no retry loop to script around.
    """
    directory = tmp_path / name
    construct.init_harness(directory, name=name, task="Reply with a greeting.")
    construct.add_validator(directory, builtin="regex_match", pattern="^HELLO")
    construct.set_value(directory, "verify.on_fail.max_retries", 0)
    return directory


def _authorize(harness: Path, scopes: list[str]) -> tuple[str, str]:
    """Generate + authorize a keypair; return (key_id, bearer_token)."""
    private_pem, public_b64 = keys_mod.generate_keypair()
    key_id = keys_mod.key_id_for(public_b64)
    path = auth_mod.authorized_keys_path(harness)
    auth_mod.authorize_key(path, name="tester", public_key_b64=public_b64, scopes=scopes)
    scope = scopes[0] if scopes != ["*"] else "*"
    token = keys_mod.sign_token(private_pem, key_id=key_id, subject="tester", scope=scope)
    return key_id, token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _BlockingProvider(ModelProvider):
    """Blocks in `complete` until released — lets a test hold a run "in
    flight" deterministically, instead of racing against a sleep duration.
    """

    def __init__(self, release: threading.Event, response):
        self._release = release
        self._response = response

    def complete(self, *, system, messages, tools, config):
        self._release.wait(timeout=5)
        return self._response


# --------------------------------------------------------------------------- #
# Auth: unauthenticated / wrong scope
# --------------------------------------------------------------------------- #
def test_health_is_unauthenticated(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["name"] == "srv"
    assert body["evolved_counter"] == 0
    assert "version_hash" in body


def test_stats_unauthenticated_is_401(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    with TestClient(app) as client:
        r = client.get("/stats")
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_stats_wrong_scope_is_403_distinct_from_401(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])  # not "read"
    with TestClient(app) as client:
        r = client.get("/stats", headers=_bearer(token))
    assert r.status_code == 403
    assert r.status_code != 401
    assert r.json()["ok"] is False


def test_stats_correct_scope_succeeds(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["read"])
    with TestClient(app) as client:
        r = client.get("/stats", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json()["harness_name"] == "srv"


def test_non_utf8_authorized_keys_store_fails_closed_not_crash(tmp_path: Path):
    """Fix-round regression: a store file with invalid UTF-8 bytes used to
    raise a raw UnicodeDecodeError, surfacing as an unhandled 500 text/plain
    error page instead of the documented {"ok": false, ...} JSON shape on
    every authenticated endpoint.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    key_id, token = _authorize(harness, ["read"])
    auth_mod.authorized_keys_path(harness).write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    with TestClient(app) as client:
        r = client.get("/stats", headers=_bearer(token))
    assert r.status_code == 401
    assert r.json()["ok"] is False


# --------------------------------------------------------------------------- #
# /run — sync
# --------------------------------------------------------------------------- #
def test_run_sync_success_matches_run_result_payload(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO world")])
    )
    _, token = _authorize(harness, ["run"])
    with TestClient(app) as client:
        r = client.post("/run", json={"input": "hi"}, headers=_bearer(token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "success"
    assert body["output"] == "HELLO world"
    assert set(body) == {
        "ok", "status", "output", "turns", "cost_usd", "duration_seconds", "run_id",
        "trace_path", "reason",
    }


def test_run_sync_verify_failed_is_still_200(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("goodbye")])
    )
    _, token = _authorize(harness, ["run"])
    with TestClient(app) as client:
        r = client.post("/run", json={"input": "hi"}, headers=_bearer(token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "verify_failed"
    assert set(body) == {
        "ok", "status", "output", "turns", "cost_usd", "duration_seconds", "run_id",
        "trace_path", "reason",
    }


# --------------------------------------------------------------------------- #
# /run — input handling: input is ALWAYS literal text; input_file is contained
# --------------------------------------------------------------------------- #
def test_run_input_containing_a_real_path_is_treated_as_literal_text(tmp_path: Path):
    harness = _harness(tmp_path)
    real_file = harness / "secret.txt"
    real_file.write_text("do not read me")
    captured: list[FakeModelProvider] = []

    def factory(base):
        provider = FakeModelProvider([text_response("HELLO")])
        captured.append(provider)
        return provider

    app = create_app(harness, provider_factory=factory)
    _, token = _authorize(harness, ["run"])
    with TestClient(app) as client:
        r = client.post(
            "/run", json={"input": str(real_file)}, headers=_bearer(token)
        )
    assert r.status_code == 200
    # The literal path string reached the model, not the file's contents.
    sent_message = captured[0].calls[0]["messages"][0]["content"]
    assert sent_message == str(real_file)
    assert "do not read me" not in sent_message


def test_run_input_file_outside_harness_dir_refused(tmp_path: Path):
    harness = _harness(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("should never be read")
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    _, token = _authorize(harness, ["run"])
    with TestClient(app) as client:
        r = client.post(
            "/run", json={"input_file": "../outside.txt"}, headers=_bearer(token)
        )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_run_input_file_within_harness_dir_is_read(tmp_path: Path):
    harness = _harness(tmp_path)
    (harness / "notes.txt").write_text("from the file")
    captured: list[FakeModelProvider] = []

    def factory(base):
        provider = FakeModelProvider([text_response("HELLO")])
        captured.append(provider)
        return provider

    app = create_app(harness, provider_factory=factory)
    _, token = _authorize(harness, ["run"])
    with TestClient(app) as client:
        r = client.post(
            "/run", json={"input_file": "notes.txt"}, headers=_bearer(token)
        )
    assert r.status_code == 200
    assert captured[0].calls[0]["messages"][0]["content"] == "from the file"


def test_run_input_file_refuses_authorized_keys_json(tmp_path: Path):
    """CRITICAL fix-round regression: staying inside the harness dir isn't
    enough — .hiveloom/ (which holds this harness's OWN auth store) lives
    inside it. A run-scoped caller must never be able to read it.
    """
    harness = _harness(tmp_path)
    _, token = _authorize(harness, ["run"])  # this call writes .hiveloom/authorized_keys.json
    captured: list[FakeModelProvider] = []

    def factory(base):
        provider = FakeModelProvider([text_response("HELLO")])
        captured.append(provider)
        return provider

    app = create_app(harness, provider_factory=factory)
    with TestClient(app) as client:
        r = client.post(
            "/run", json={"input_file": ".hiveloom/authorized_keys.json"}, headers=_bearer(token)
        )
    assert r.status_code == 400
    assert captured == []  # never even reached run_harness/the model


def test_run_input_file_refuses_another_runs_trace(tmp_path: Path):
    """A run-scoped caller must not be able to read a PRIOR run's full
    transcript out of .hiveloom/traces/ — that's what /trace and its `read`
    scope exist to gate; `run` scope must not grant broader data access.
    """
    harness = _harness(tmp_path)
    _, token = _authorize(harness, ["run"])
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    with TestClient(app) as client:
        first = client.post("/run", json={"input": "hi"}, headers=_bearer(token))
        assert first.status_code == 200
        trace_path = Path(first.json()["trace_path"])
        rel_trace = trace_path.relative_to(harness.resolve()).as_posix()

        r = client.post("/run", json={"input_file": rel_trace}, headers=_bearer(token))
    assert r.status_code == 400


def test_run_input_file_refuses_dotenv(tmp_path: Path):
    """A deployed harness routinely holds a live ANTHROPIC_API_KEY in .env
    (ext.py loads it from there) — input_file must never forward it.
    """
    harness = _harness(tmp_path)
    (harness / ".env").write_text("ANTHROPIC_API_KEY=super-secret-value\n")
    _, token = _authorize(harness, ["run"])
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    with TestClient(app) as client:
        r = client.post("/run", json={"input_file": ".env"}, headers=_bearer(token))
    assert r.status_code == 400


def test_run_input_file_refuses_custom_trace_dir(tmp_path: Path):
    """The sensitivity check must cover the configured trace_dir even when
    it is NOT the default `.hiveloom/traces` (and so wouldn't be caught by
    the `.hiveloom/` exclusion alone).
    """
    harness = _harness(tmp_path)
    construct.set_value(harness, "logging.trace_dir", "logs")
    (harness / "logs").mkdir()
    (harness / "logs" / "run_x.jsonl").write_text('{"type": "run_started"}\n')
    _, token = _authorize(harness, ["run"])
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    with TestClient(app) as client:
        r = client.post("/run", json={"input_file": "logs/run_x.jsonl"}, headers=_bearer(token))
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# /run?stream=true
# --------------------------------------------------------------------------- #
def test_run_stream_emits_ordered_sse_frames_ending_in_run_result(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO world")])
    )
    _, token = _authorize(harness, ["run"])
    with TestClient(app) as client:
        with client.stream(
            "POST", "/run?stream=true", json={"input": "hi"}, headers=_bearer(token)
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line.startswith("data: ")]

    frames = [json.loads(line[len("data: ") :]) for line in lines]
    assert frames[0]["type"] == "run_started"
    assert frames[-1]["type"] == "run_result"
    assert frames[-1]["status"] == "success"
    assert any(f["type"] == "run_finished" for f in frames[:-1])


# --------------------------------------------------------------------------- #
# /run — queue-full 503
# --------------------------------------------------------------------------- #
def test_run_exceeding_queue_cap_returns_503_with_retry_after(tmp_path: Path):
    harness = _harness(tmp_path)
    release = threading.Event()
    app = create_app(
        harness,
        max_concurrent_runs=1,
        max_queued_runs=1,  # cap = 2
        provider_factory=lambda base: _BlockingProvider(release, text_response("HELLO")),
    )
    _, token = _authorize(harness, ["run"])

    results: list = []
    with TestClient(app) as client:
        def fire():
            results.append(client.post("/run", json={"input": "hi"}, headers=_bearer(token)))

        threads = [threading.Thread(target=fire) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.3)  # let all 3 reach RunSlots.submit() while still blocked
        release.set()
        for t in threads:
            t.join(timeout=5)

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200, 503]
    rejected = next(r for r in results if r.status_code == 503)
    assert "Retry-After" in rejected.headers
    assert rejected.json()["ok"] is False


# --------------------------------------------------------------------------- #
# Mutating endpoints round-trip
# --------------------------------------------------------------------------- #
def test_set_round_trips(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/set", json={"path": "loop.max_turns", "value": 7}, headers=_bearer(token)
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "path": "loop.max_turns"}
    assert load_spec(harness).loop.max_turns == 7


def test_add_tool_round_trips(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/tool", json={"builtin": "http_get", "description": "fetch a URL"},
            headers=_bearer(token),
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "added": "tool", "ref": "http_get"}
    assert any(getattr(t, "builtin", None) == "http_get" for t in load_spec(harness).tools)


def test_add_validator_round_trips(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/validator", json={"builtin": "file_exists", "path": "output.txt"},
            headers=_bearer(token),
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "added": "validator", "ref": "file_exists"}
    assert any(
        getattr(v, "builtin", None) == "file_exists" for v in load_spec(harness).verify.validators
    )


def test_add_hook_round_trips(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/hook",
            json={"on": "run_finished", "code": "hooks/my_hook.py:my_hook", "description": "log"},
            headers=_bearer(token),
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "added": "hook", "ref": "run_finished:hooks/my_hook.py:my_hook"}
    assert (harness / "hooks" / "my_hook.py").exists()
    assert any(getattr(h, "event", None) == "run_finished" for h in load_spec(harness).hooks)


def test_add_guardrail_added_then_replaced(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        # max_wall_clock_seconds has no auto-injected default (unlike
        # max_cost_usd), so the first call is a genuine "added".
        r1 = client.post(
            "/add/guardrail", json={"builtin": "max_wall_clock_seconds", "value": 60},
            headers=_bearer(token),
        )
        assert r1.status_code == 200
        assert r1.json() == {"ok": True, "added": "guardrail", "ref": "max_wall_clock_seconds"}

        r2 = client.post(
            "/add/guardrail", json={"builtin": "max_wall_clock_seconds", "value": 120},
            headers=_bearer(token),
        )
    assert r2.status_code == 200
    body = r2.json()
    assert body["replaced"] == "guardrail"
    assert body["after"]["value"] == 120


def test_add_skill_round_trips(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/skill", json={"name": "greeting", "description": "how to greet"},
            headers=_bearer(token),
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "added": "skill", "ref": "greeting"}
    assert "greeting" in load_spec(harness).skills


def test_add_unknown_kind_is_400(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/add/bogus", json={}, headers=_bearer(token))
    assert r.status_code == 400


def test_remove_round_trips(tmp_path: Path):
    harness = _harness(tmp_path)
    construct.add_tool(harness, builtin="http_get", description="fetch")
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/remove", json={"target": "http_get"}, headers=_bearer(token))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": "http_get"}
    assert load_spec(harness).tools == []


def test_validate_endpoint(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["read"])
    with TestClient(app) as client:
        r = client.post("/validate", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "srv", "message": "harness is valid"}


# --------------------------------------------------------------------------- #
# Spec lock: concurrent /set never loses an update
# --------------------------------------------------------------------------- #
def test_set_concurrent_writes_never_lose_an_update(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    n = 10

    def set_turns(i: int):
        with TestClient(app) as client:
            return client.post(
                "/set", json={"path": "loop.max_turns", "value": 10 + i}, headers=_bearer(token)
            )

    def set_tokens(i: int):
        with TestClient(app) as client:
            return client.post(
                "/set", json={"path": "model.max_tokens", "value": 5000 + i},
                headers=_bearer(token),
            )

    with ThreadPoolExecutor(max_workers=2 * n) as pool:
        futures = [pool.submit(set_turns, i) for i in range(n)]
        futures += [pool.submit(set_tokens, i) for i in range(n)]
        results = [f.result() for f in futures]

    assert all(r.status_code == 200 for r in results)
    spec = load_spec(harness)
    # Neither field's write was lost/reverted by the other's concurrent
    # read-modify-write cycle — each holds one of ITS OWN sent values, not
    # the original default (20 / 4096).
    assert spec.loop.max_turns in range(10, 10 + n)
    assert spec.model.max_tokens in range(5000, 5000 + n)


# --------------------------------------------------------------------------- #
# Proposals: propose -> list -> show -> apply / reject
# --------------------------------------------------------------------------- #
def test_proposals_propose_list_show_apply_flow(tmp_path: Path):
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, name="srv")
    app = create_app(harness, strong_model=FakeStrongModel([_PROPOSAL_PAYLOAD]))
    read_token = _authorize(harness, ["read"])[1]
    evolve_token = _authorize(harness, ["evolve"])[1]

    with TestClient(app) as client:
        r = client.post("/evolve/propose", json={}, headers=_bearer(evolve_token))
        assert r.status_code == 200
        proposed = r.json()
        assert proposed["ok"] is True
        assert proposed["status"] == "pending"
        proposal_id = proposed["id"]

        r = client.get("/proposals", headers=_bearer(read_token))
        assert r.status_code == 200
        listed = r.json()["proposals"]
        assert len(listed) == 1
        assert listed[0]["id"] == proposal_id

        r = client.get(f"/proposals/{proposal_id}", headers=_bearer(read_token))
        assert r.status_code == 200
        shown = r.json()
        # Same shape whether reached via list or show — both go through the
        # one shared `proposal_payload` the CLI's `proposals show --json` uses.
        assert shown == {"ok": True, **listed[0]}

        r = client.get("/proposals/does-not-exist", headers=_bearer(read_token))
        assert r.status_code == 404

        r = client.post(
            f"/proposals/{proposal_id}/apply",
            json={"apply_yaml": True},
            headers=_bearer(evolve_token),
        )
        assert r.status_code == 200
        applied = r.json()
        assert applied["changed"] is True

    assert load_spec(harness).loop.max_turns == 25


def test_proposals_reject(tmp_path: Path):
    harness = _harness(tmp_path)
    _seed_failure(tmp_path, name="srv")
    app = create_app(harness, strong_model=FakeStrongModel([_PROPOSAL_PAYLOAD]))
    evolve_token = _authorize(harness, ["evolve"])[1]

    with TestClient(app) as client:
        r = client.post("/evolve/propose", json={}, headers=_bearer(evolve_token))
        proposal_id = r.json()["id"]

        r = client.post(
            f"/proposals/{proposal_id}/reject",
            json={"reason": "not needed"},
            headers=_bearer(evolve_token),
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "proposal_id": proposal_id, "status": "rejected"}

        r = client.post(
            f"/proposals/{proposal_id}/reject", json={}, headers=_bearer(evolve_token)
        )
        # Already resolved -> a caller mistake, not a missing resource.
        assert r.status_code == 400
