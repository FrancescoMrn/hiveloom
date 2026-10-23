"""The `http_verdict` builtin validator: a remote endpoint owns the verdict."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hiveloom import catalog, construct
from hiveloom.errors import SpecError
from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.verify.base import VerificationContext
from hiveloom.verify.builtin import HttpVerdictVerifier


class _VerdictServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests: list[dict] = []
        super().__init__(("127.0.0.1", 0), _VerdictHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/verdict"


class _VerdictHandler(BaseHTTPRequestHandler):
    server: _VerdictServer

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        self.server.requests.append({"body": body, "headers": dict(self.headers)})
        status, answer = self.server.answers.pop(0) if self.server.answers else (200, {})
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def verdicts():
    servers: list[_VerdictServer] = []

    def start(*answers):
        srv = _VerdictServer(answers)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _context(*artifacts):
    return VerificationContext(run_id="run_x", artifacts=tuple(artifacts))


def test_listed_in_the_catalog():
    assert "http_verdict" in catalog.BUILTIN_VALIDATORS


def test_posts_output_evidence_and_drew(verdicts):
    srv = verdicts((200, {"passed": True}))
    verifier = HttpVerdictVerifier(srv.url, {"Authorization": "tok"})
    ctx = _context(
        {"kind": "evidence", "data": {"id": "deal-1"}, "tool": "search"},
        {"kind": "view", "data": {"blocks": []}, "tool": "draw"},
    )
    verdict = verifier.validate("the answer", {"context": {"tok": "Bearer s3"}}, ctx)
    assert verdict.passed
    sent = srv.requests[0]
    assert sent["body"]["output"] == "the answer"
    assert sent["body"]["evidence"] == ["deal-1"]
    assert sent["body"]["drew"] is True
    assert len(sent["body"]["artifacts"]) == 2
    assert sent["headers"]["Authorization"] == "Bearer s3"


def test_failure_lists_dropped_parts(verdicts):
    srv = verdicts((200, {"passed": False, "dropped": ["Revenue is 4M", "Ada leads"]}))
    verdict = HttpVerdictVerifier(srv.url).validate("x", {}, _context())
    assert not verdict.passed
    assert verdict.feedback == "These parts did not pass:\n- Revenue is 4M\n- Ada leads"


def test_feedback_field_is_accepted(verdicts):
    srv = verdicts((200, {"passed": False, "feedback": "cite a record"}))
    verdict = HttpVerdictVerifier(srv.url).validate("x", {}, _context())
    assert not verdict.passed and verdict.feedback == "cite a record"


def test_missing_context_key_fails_naming_it(verdicts):
    srv = verdicts()
    verdict = HttpVerdictVerifier(srv.url, {"Authorization": "tok"}).validate(
        "x", {"context": {}}, _context()
    )
    assert not verdict.passed and "'tok'" in verdict.feedback
    assert srv.requests == []


def test_non_2xx_and_unreachable_fail_without_raising(verdicts):
    srv = verdicts((503, {"error": "down"}))
    verdict = HttpVerdictVerifier(srv.url).validate("x", {}, _context())
    assert not verdict.passed and "503" in verdict.feedback
    dead = HttpVerdictVerifier("http://127.0.0.1:9/verdict", timeout_seconds=2)
    verdict = dead.validate("x", {}, _context())
    assert not verdict.passed and "unreachable" in verdict.feedback


def test_cli_param_shape_validates(harness_dir):
    construct.add_validator(
        harness_dir,
        builtin="http_verdict",
        url="https://app.example/verdict",
        header_context={"Authorization": "reader_token"},
    )
    with pytest.raises(SpecError):
        construct.add_validator(harness_dir, builtin="http_verdict", url=None)


def test_retry_with_feedback_end_to_end(verdicts, harness_dir):
    from hiveloom import runner

    srv = verdicts(
        (200, {"passed": False, "dropped": ["the invented figure"]}),
        (200, {"passed": True}),
    )
    construct.add_validator(
        harness_dir,
        builtin="http_verdict",
        url=srv.url,
        header_context={"Authorization": "reader_token"},
    )
    provider = FakeModelProvider([text_response("first"), text_response("second")])
    result = runner.run_harness(
        harness_dir,
        provider=provider,
        input_value="go",
        literal_input=True,
        context={"reader_token": "Bearer secret-value"},
    )
    assert result.status == "success"
    assert [r["body"]["output"] for r in srv.requests] == ["first", "second"]
    retry_prompt = json.dumps(provider.calls[1]["messages"])
    assert "the invented figure" in retry_prompt
    traces = list((harness_dir / ".hiveloom").rglob("*.jsonl"))
    assert traces
    assert all("secret-value" not in t.read_text() for t in traces)
