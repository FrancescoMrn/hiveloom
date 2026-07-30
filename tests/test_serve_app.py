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

import pytest
import yaml
from starlette.testclient import TestClient

# Reuse the Hive failure-seeding helper and a scripted proposal payload from
# the evolve tests (tests dir is on sys.path) — the established cross-test-file
# pattern in this suite (test_proposals.py does the same from test_evolve).
from test_evolve import _PROPOSAL_PAYLOAD, _seed_failure

from hiveloom import construct
from hiveloom.errors import AuthorizationError
from hiveloom.generate.llm import FakeStrongModel
from hiveloom.models.fake import FakeModelProvider, ModelProvider, text_response
from hiveloom.serve import app as app_mod
from hiveloom.serve import auth as auth_mod
from hiveloom.serve import keys as keys_mod
from hiveloom.serve.app import _error_response, create_app
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import ALWAYS_FROZEN


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
# _error_response: every mapped exception type gets its own direct test —
# independent of any endpoint, so the next type added to this mapping can't
# silently fall through to 500 the way AuthorizationError did until this
# branch introduced its first caller (fix-round-4/5).
# --------------------------------------------------------------------------- #
def test_error_response_maps_authorization_error_to_403():
    response = _error_response(AuthorizationError("x"))
    assert response.status_code == 403


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


def test_run_input_file_refuses_case_variant_dotenv(tmp_path: Path):
    """Fix-round-2 regression: case-insensitive filesystems (macOS APFS,
    most Windows filesystems) don't correct a caller's casing to the on-disk
    name — `.ENV` must be refused exactly like `.env`.
    """
    harness = _harness(tmp_path)
    (harness / ".env").write_text("ANTHROPIC_API_KEY=super-secret-value\n")
    _, token = _authorize(harness, ["run"])
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    with TestClient(app) as client:
        r = client.post("/run", json={"input_file": ".ENV"}, headers=_bearer(token))
    assert r.status_code == 400


def test_run_input_file_refuses_case_variant_hiveloom_dir(tmp_path: Path):
    harness = _harness(tmp_path)
    _, token = _authorize(harness, ["run"])  # writes .hiveloom/authorized_keys.json
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    with TestClient(app) as client:
        r = client.post(
            "/run", json={"input_file": ".HIVELOOM/authorized_keys.json"}, headers=_bearer(token)
        )
    assert r.status_code == 400


def test_run_input_file_refuses_custom_trace_dir_case_variant(tmp_path: Path):
    """Same as the exact-case custom-trace-dir test above, but with a
    caller-supplied path whose case differs from the configured trace_dir.
    """
    harness = _harness(tmp_path)
    construct.set_value(harness, "logging.trace_dir", "MyLogs")
    (harness / "MyLogs").mkdir()
    (harness / "MyLogs" / "run_x.jsonl").write_text('{"type": "run_started"}\n')
    _, token = _authorize(harness, ["run"])
    app = create_app(
        harness, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    with TestClient(app) as client:
        r = client.post("/run", json={"input_file": "mylogs/run_x.jsonl"}, headers=_bearer(token))
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


def test_add_tool_refused_over_http_but_works_locally(tmp_path: Path):
    """`tools` is frozen over the control plane: it is a code-execution root
    (`add tool shell` runs arbitrary shell on the next run), so a remote
    `mutate`-scoped caller must not reach it, even though the local construct
    API still can.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/tool", json={"builtin": "http_get", "description": "fetch a URL"},
            headers=_bearer(token),
        )
    assert r.status_code == 403
    assert r.json()["ok"] is False
    assert load_spec(harness).tools == []

    construct.add_tool(harness, builtin="http_get", description="fetch a URL")
    assert any(getattr(t, "builtin", None) == "http_get" for t in load_spec(harness).tools)


def test_add_validator_refused_over_http_but_works_locally(tmp_path: Path):
    """`verify.validators` is frozen over the control plane: the
    `command_succeeds` validator runs arbitrary shell, so like `tools` it is a
    code-execution root a remote `mutate` caller must not reach.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/validator", json={"builtin": "file_exists", "path": "output.txt"},
            headers=_bearer(token),
        )
    assert r.status_code == 403
    assert r.json()["ok"] is False
    assert not any(
        getattr(v, "builtin", None) == "file_exists" for v in load_spec(harness).verify.validators
    )

    construct.add_validator(harness, builtin="file_exists", path="output.txt")
    assert any(
        getattr(v, "builtin", None) == "file_exists" for v in load_spec(harness).verify.validators
    )


def test_add_hook_refused_over_http_but_works_locally(tmp_path: Path):
    """`hooks` is an ALWAYS_FROZEN root (fix-round-4): a remote `mutate`-
    scoped caller must not be able to attach one, even though the local
    construct API — the sanctioned way to edit a spec locally — still can.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/hook",
            json={"on": "run_finished", "code": "hooks/my_hook.py:my_hook", "description": "log"},
            headers=_bearer(token),
        )
    assert r.status_code == 403
    assert r.json()["ok"] is False
    assert not (harness / "hooks" / "my_hook.py").exists()
    assert load_spec(harness).hooks == []

    construct.add_hook(
        harness, on="run_finished", code="hooks/my_hook.py:my_hook", description="log"
    )
    assert (harness / "hooks" / "my_hook.py").exists()
    assert any(getattr(h, "event", None) == "run_finished" for h in load_spec(harness).hooks)


def test_add_guardrail_refused_over_http_but_works_locally(tmp_path: Path):
    """`guardrails` is an ALWAYS_FROZEN root (fix-round-4): same refusal as
    hooks, same local-CLI exemption.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/add/guardrail", json={"builtin": "max_wall_clock_seconds", "value": 60},
            headers=_bearer(token),
        )
    assert r.status_code == 403
    assert r.json()["ok"] is False
    guardrail_builtins = [getattr(g, "builtin", None) for g in load_spec(harness).guardrails]
    assert "max_wall_clock_seconds" not in guardrail_builtins

    construct.add_guardrail(harness, builtin="max_wall_clock_seconds", value=60)
    guardrail_builtins = [getattr(g, "builtin", None) for g in load_spec(harness).guardrails]
    assert "max_wall_clock_seconds" in guardrail_builtins


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
    # A non-frozen removable target (a skill). `tools` is frozen over the
    # control plane — see test_remove_tool_refused_over_http.
    harness = _harness(tmp_path)
    construct.add_skill(harness, name="greeting", description="how to greet")
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/remove", json={"target": "greeting"}, headers=_bearer(token))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": "greeting"}
    assert "greeting" not in load_spec(harness).skills


def test_remove_tool_refused_over_http(tmp_path: Path):
    """Removing a tool touches the frozen `tools` root, so `/remove` refuses it
    over the control plane — a remote caller cannot strip a harness's tools."""
    harness = _harness(tmp_path)
    construct.add_tool(harness, builtin="http_get", description="fetch")
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/remove", json={"target": "http_get"}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False
    assert any(getattr(t, "builtin", None) == "http_get" for t in load_spec(harness).tools)


# --------------------------------------------------------------------------- #
# Fix-round-4 CRITICAL: ALWAYS_FROZEN roots refused over the control plane
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "value"),
    [
        # Every value here is independently valid for its field (confirmed
        # against construct.set_value directly before this fix existed) —
        # each one WOULD have been accepted (200) pre-fix. A placeholder
        # like "x" would be rejected by ordinary type validation (a plain
        # 400) regardless of this fix, which would make the test pass for
        # the wrong reason.
        ("guardrails", []),
        ("model", {"provider": "claude", "id": "claude-sonnet-5", "max_tokens": 4096}),
        ("model.id", "claude-sonnet-5"),
        ("logging.redact", ["secret"]),
        ("extensions", []),
        ("hooks", []),
        ("evolution.auto_propose", {}),
        ("evolution.auto_propose.enabled", True),
    ],
)
def test_set_refuses_every_always_frozen_root(tmp_path: Path, path: str, value):
    """`mutate` scope must not reach any ALWAYS_FROZEN root: setting `model`
    could repoint the executor at an attacker-controlled base_url, setting
    `logging.redact` could strip redaction so secrets land in traces in
    cleartext, setting `guardrails` could remove the cost cap entirely.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/set", json={"path": path, "value": value}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False


@pytest.mark.parametrize("path", ["Model", "logging.Redact", "GUARDRAILS"])
def test_set_refuses_case_variant_frozen_root(tmp_path: Path, path: str):
    """Fix-round-5 regression: before the `_covered` casefold fix, a
    case-variant path like this was refused too, but only as an incidental
    400 (an unrecognized top-level key rejected by pydantic's
    `extra="forbid"`, since dotted-path lookup is itself case-sensitive) —
    not because the frozen-root check actually caught it. Post-fix it must
    be refused as 403 (an authorization failure), not merely happen to
    fail for an unrelated reason.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/set", json={"path": path, "value": []}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False


def test_set_ordinary_path_still_succeeds(tmp_path: Path):
    """The deny-list must not overreach: an ordinary, non-frozen field is
    unaffected."""
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/set", json={"path": "system_prompt", "value": "Be helpful."},
            headers=_bearer(token),
        )
    assert r.status_code == 200
    assert load_spec(harness).system_prompt == "Be helpful."


# --------------------------------------------------------------------------- #
# Post-merge CRITICAL: bypasses that survived the fix-round-4 deny-list
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "value"),
    [
        # Writing a parent mapping overwrites its frozen child: `logging`
        # replaces the frozen `logging.redact`, `evolution` replaces the frozen
        # `evolution.auto_propose`. Each value is independently valid for its
        # field, so pre-fix each was accepted (200) and silently defeated the
        # freeze — stripping redaction / enabling the paid auto-propose trigger.
        ("logging", {"redact": []}),
        ("evolution", {"auto_propose": {"enabled": True, "min_failures": 1}}),
    ],
)
def test_set_parent_of_frozen_leaf_is_refused(tmp_path: Path, path: str, value):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/set", json={"path": path, "value": value}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False
    # The frozen child is untouched.
    assert load_spec(harness).evolution.auto_propose.enabled is False


def test_set_name_is_refused_so_hive_binding_holds(tmp_path: Path):
    """`name` binds every Hive lookup (`/trace`, `/proposals`) to this harness.
    A remote caller must not rewrite it to re-point those reads at another
    harness's traces and proposals."""
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/set", json={"path": "name", "value": "victim"}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False
    assert load_spec(harness).name == "srv"


def test_set_tools_shell_is_refused_no_rce(tmp_path: Path):
    """The RCE vector: `/set tools` to a shell-executing builtin. `tools` is a
    frozen code-execution root over the control plane, so it is refused."""
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post(
            "/set",
            json={"path": "tools", "value": [{"builtin": "shell", "commands": ["echo hi"]}]},
            headers=_bearer(token),
        )
    assert r.status_code == 403
    assert r.json()["ok"] is False
    assert load_spec(harness).tools == []


def test_remove_refuses_dotted_path_under_frozen_root(tmp_path: Path):
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/remove", json={"target": "model"}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False


def test_remove_refuses_named_entry_in_frozen_list_section(tmp_path: Path):
    """A frozen root can also be touched by NAME, not just by dotted path:
    `remove` matches an entry in the `guardrails` list by builtin name.
    `init_harness` auto-injects a `max_cost_usd` guardrail, so one always
    exists to target.
    """
    harness = _harness(tmp_path)
    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/remove", json={"target": "max_cost_usd"}, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["ok"] is False
    guardrail_builtins = [getattr(g, "builtin", None) for g in load_spec(harness).guardrails]
    assert "max_cost_usd" in guardrail_builtins


def test_remove_refuses_named_entry_in_a_frozen_section_app_never_heard_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The named-entry refusal must derive from construct's own section table,
    not from a list maintained in `app.py`.

    A branch adding a new frozen list section (`mcp_servers`) registers it in
    `construct._LIST_SECTIONS` and in `ALWAYS_FROZEN` — and must be refused
    here with no edit to `app.py`. Simulating that with a section this module
    has never seen is the only way to prove the derivation rather than a
    coincidence: a hardcoded tuple would let this removal through.
    """
    monkeypatch.setitem(construct._LIST_SECTIONS, "widgets", ("widgets",))
    monkeypatch.setattr(app_mod, "_FROZEN_ROOTS", {*ALWAYS_FROZEN, "widgets"})
    harness = _harness(tmp_path)
    raw = construct.load_raw(harness)
    raw["widgets"] = [{"builtin": "sharp_edge"}]
    (harness / "harness.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    app = create_app(harness)
    _, token = _authorize(harness, ["mutate"])
    with TestClient(app) as client:
        r = client.post("/remove", json={"target": "sharp_edge"}, headers=_bearer(token))
    assert r.status_code == 403
    assert construct.load_raw(harness)["widgets"] == [{"builtin": "sharp_edge"}]


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
            # context.max_input_tokens, not model.max_tokens: `model` is an
            # ALWAYS_FROZEN root (fix-round-4) and is correctly refused
            # over HTTP now — this test needs two DIFFERENT non-frozen
            # fields, not a demonstration of that refusal.
            return client.post(
                "/set", json={"path": "context.max_input_tokens", "value": 5000 + i},
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
    # the original default (20 / 30000).
    assert spec.loop.max_turns in range(10, 10 + n)
    assert spec.context.max_input_tokens in range(5000, 5000 + n)


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


# --------------------------------------------------------------------------- #
# Fix-round-4 IMPORTANT: the Hive is global — direct-id lookups must bind to
# the served harness, not trust an id alone (two harnesses, one shared Hive)
# --------------------------------------------------------------------------- #
def test_trace_endpoint_refuses_another_harnesss_run(tmp_path: Path):
    harness_a = _harness(tmp_path, name="harness-a")
    harness_b = _harness(tmp_path, name="harness-b")

    app_b = create_app(
        harness_b, provider_factory=lambda base: FakeModelProvider([text_response("HELLO")])
    )
    _, token_b = _authorize(harness_b, ["run"])
    with TestClient(app_b) as client_b:
        r = client_b.post("/run", json={"input": "hi"}, headers=_bearer(token_b))
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    # Same Hive (the autouse fixture points HIVELOOM_DB at one throwaway
    # file per test, not per harness) — a caller authorized for harness A
    # must not be able to read harness B's run trace by id alone.
    app_a = create_app(harness_a)
    _, token_a = _authorize(harness_a, ["read"])
    with TestClient(app_a) as client_a:
        r = client_a.get(f"/trace/{run_id}", headers=_bearer(token_a))
    assert r.status_code == 404

    # And harness B's own caller can still read it.
    _, token_b_read = _authorize(harness_b, ["read"])
    with TestClient(app_b) as client_b:
        r = client_b.get(f"/trace/{run_id}", headers=_bearer(token_b_read))
    assert r.status_code == 200


def test_proposals_endpoints_refuse_another_harnesss_proposal(tmp_path: Path):
    harness_a = _harness(tmp_path, name="harness-a")
    harness_b = _harness(tmp_path, name="harness-b")
    _seed_failure(tmp_path, name="harness-b")

    app_b = create_app(harness_b, strong_model=FakeStrongModel([_PROPOSAL_PAYLOAD]))
    _, evolve_token_b = _authorize(harness_b, ["evolve"])
    with TestClient(app_b) as client_b:
        r = client_b.post("/evolve/propose", json={}, headers=_bearer(evolve_token_b))
    assert r.status_code == 200
    proposal_id = r.json()["id"]

    app_a = create_app(harness_a)
    _, read_token_a = _authorize(harness_a, ["read"])
    _, evolve_token_a = _authorize(harness_a, ["evolve"])
    with TestClient(app_a) as client_a:
        r_show = client_a.get(f"/proposals/{proposal_id}", headers=_bearer(read_token_a))
        assert r_show.status_code == 404

        r_reject = client_a.post(
            f"/proposals/{proposal_id}/reject", json={}, headers=_bearer(evolve_token_a)
        )
        assert r_reject.status_code == 404

        r_apply = client_a.post(
            f"/proposals/{proposal_id}/apply",
            json={"apply_yaml": True},
            headers=_bearer(evolve_token_a),
        )
        assert r_apply.status_code == 404

    # None of harness A's attempts touched harness B's proposal: still pending.
    with TestClient(app_b) as client_b:
        r = client_b.get(
            f"/proposals/{proposal_id}", headers=_bearer(_authorize(harness_b, ["read"])[1])
        )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
