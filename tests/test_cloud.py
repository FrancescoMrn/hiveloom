"""Linked-mode sync: link / pull / push / sync against a faked cloud."""

from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib.parse import urlsplit

import pytest

from hiveloom import cloud


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeCloud:
    """A fake hiveloom-cloud link API, driven through the opener seam."""

    def __init__(self, slug: str = "demo-notes", version: str = "aaa1111"):
        self.slug = slug
        self.version = version
        self.protocol: int | None = cloud.PROTOCOL_VERSION
        self.files: dict[str, str] = {"harness.yaml": "name: demo\nlogging: {}\n"}
        self.trace_uploads: list[dict] = []
        self.requests: list[str] = []

    def _zip(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for rel, content in self.files.items():
                archive.writestr(f"{self.slug}/{rel}", content)
        return buffer.getvalue()

    def opener(self, request, timeout=None):
        assert request.get_header("Authorization") == "Bearer hl_link_tok"
        path = urlsplit(request.full_url).path
        self.requests.append(f"{request.get_method()} {path}")
        if path == "/api/link/status":
            payload = {"slug": self.slug, "name": self.slug, "version_hash": self.version}
            if self.protocol is not None:
                payload["protocol"] = self.protocol
            return _Response(json.dumps(payload).encode())
        if path == "/api/link/pull":
            return _Response(self._zip())
        if path == "/api/link/traces":
            body = json.loads(request.data)
            self.trace_uploads.append(body)
            run_count = sum(len(f["content"].splitlines()) > 0 for f in body["files"])
            return _Response(json.dumps({"run_count": run_count}).encode())
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture()
def fake() -> FakeCloud:
    return FakeCloud()


def _link(tmp_path, fake: FakeCloud):
    return cloud.link_harness(
        "https://cloud.test", "hl_link_tok", tmp_path / "demo", opener=fake.opener
    )


def test_link_pulls_and_records_version(tmp_path, fake):
    result = _link(tmp_path, fake)
    base = tmp_path / "demo"
    assert result["changed"] is True
    assert (base / "harness.yaml").read_text().startswith("name: demo")
    stored = json.loads((base / ".hiveloom" / "cloud.json").read_text())
    assert stored["token"] == "hl_link_tok"
    assert stored["version_hash"] == "aaa1111"


def test_link_defaults_directory_to_slug(tmp_path, fake, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = cloud.link_harness("https://cloud.test", "hl_link_tok", opener=fake.opener)
    assert result["dir"] == "demo-notes"
    assert (tmp_path / "demo-notes" / "harness.yaml").exists()


def test_pull_is_a_noop_until_the_remote_hash_moves(tmp_path, fake):
    _link(tmp_path, fake)
    base = tmp_path / "demo"
    assert cloud.pull(base, opener=fake.opener)["changed"] is False

    fake.version = "bbb2222"
    fake.files["harness.yaml"] = "name: demo\nlogging: {}\n# v2\n"
    result = cloud.pull(base, opener=fake.opener)
    assert result["changed"] is True
    assert "# v2" in (base / "harness.yaml").read_text()


def test_pull_preserves_local_hiveloom_dir(tmp_path, fake):
    _link(tmp_path, fake)
    base = tmp_path / "demo"
    trace = base / ".hiveloom" / "traces" / "run_x.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"run_id": "run_x"}\n')
    fake.version = "ccc3333"
    cloud.pull(base, opener=fake.opener)
    assert trace.exists()


def test_push_uploads_trace_files(tmp_path, fake):
    _link(tmp_path, fake)
    base = tmp_path / "demo"
    traces = base / ".hiveloom" / "traces"
    traces.mkdir(parents=True)
    (traces / "run_a.jsonl").write_text('{"run_id": "run_a"}\n')
    result = cloud.push(base, opener=fake.opener)
    assert result["uploaded"] == 1
    assert fake.trace_uploads[0]["files"][0]["name"] == "run_a.jsonl"


def test_push_without_traces_skips_the_request(tmp_path, fake):
    _link(tmp_path, fake)
    before = list(fake.requests)
    assert cloud.push(tmp_path / "demo", opener=fake.opener) == {
        "uploaded": 0,
        "run_count": 0,
    }
    assert fake.requests == before  # no HTTP call happened


def test_sync_pushes_then_pulls(tmp_path, fake):
    _link(tmp_path, fake)
    base = tmp_path / "demo"
    traces = base / ".hiveloom" / "traces"
    traces.mkdir(parents=True)
    (traces / "run_a.jsonl").write_text('{"run_id": "run_a"}\n')
    fake.version = "ddd4444"
    result = cloud.sync(base, opener=fake.opener)
    assert result["uploaded"] == 1
    assert result["changed"] is True
    assert result["version_hash"] == "ddd4444"


def test_unlinked_dir_raises(tmp_path):
    with pytest.raises(cloud.CloudError, match="not linked"):
        cloud.pull(tmp_path)


def test_rejected_token_is_a_friendly_error(tmp_path, fake):
    _link(tmp_path, fake)

    def denying_opener(request, timeout=None):
        raise urlerror.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b""))

    with pytest.raises(cloud.CloudError, match="link token was rejected"):
        cloud.pull(tmp_path / "demo", opener=denying_opener)


def test_zip_slip_is_refused(tmp_path, fake):
    _link(tmp_path, fake)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/../../evil.txt", "boom")
    with pytest.raises(cloud.CloudError, match="escaping"):
        cloud._extract_zip(tmp_path / "demo", buffer.getvalue())
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize(
    "entry,local_rel",
    [
        ("demo/.env", ".env"),
        ("demo/.hiveloom/cloud.json", ".hiveloom/cloud.json"),
    ],
)
def test_protected_entry_is_refused_and_untouched(tmp_path, fake, entry, local_rel):
    """Each protected destination is checked on its own, so its assertion cannot
    pass merely because extraction stopped at an earlier entry."""
    _link(tmp_path, fake)
    base = tmp_path / "demo"
    local_path = base / local_rel
    if local_rel.endswith("cloud.json"):
        original = local_path.read_text()  # the link config written by _link
    else:
        local_path.write_text("SECRET=original\n")
        original = "SECRET=original\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry, json.dumps({"base_url": "https://evil.test"}))
    with pytest.raises(cloud.CloudError, match="protected"):
        cloud._extract_zip(base, buffer.getvalue())
    assert local_path.read_text() == original


def test_entries_ahead_of_a_refused_one_are_still_written(tmp_path, fake):
    """Pull is non-transactional: an ordinary file before the refused entry still
    lands, and the abort happens before any protected destination."""
    _link(tmp_path, fake)
    base = tmp_path / "demo"
    link_config = (base / ".hiveloom" / "cloud.json").read_text()
    (base / ".env").write_text("SECRET=original\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/harness.yaml", "name: demo\nversion: 9\n")
        archive.writestr("demo/.env", "SECRET=stolen\n")
        archive.writestr("demo/.hiveloom/cloud.json", json.dumps({"base_url": "https://evil.test"}))
    with pytest.raises(cloud.CloudError, match="protected"):
        cloud._extract_zip(base, buffer.getvalue())
    assert (base / "harness.yaml").read_text() == "name: demo\nversion: 9\n"
    assert (base / ".env").read_text() == "SECRET=original\n"
    assert (base / ".hiveloom" / "cloud.json").read_text() == link_config


@pytest.mark.parametrize(
    "entry", ["demo/.env.local", "demo/.HIVELOOM/cloud.json", "demo/sub/../.env"]
)
def test_protected_path_variants_are_refused(tmp_path, fake, entry):
    _link(tmp_path, fake)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry, "boom")
    with pytest.raises(cloud.CloudError, match="protected"):
        cloud._extract_zip(tmp_path / "demo", buffer.getvalue())


@pytest.mark.parametrize("entry", ["demo/.env.example", "demo/.env.template"])
def test_shipped_env_templates_are_ordinary_harness_files(tmp_path, fake, entry):
    """The packaged zip ships these templates, so pull must NOT refuse them — this
    is the Major false-positive the security review caught, now locked."""
    _link(tmp_path, fake)
    name = entry.split("/", 1)[1]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry, "TOKEN=example\n")
    assert cloud._extract_zip(tmp_path / "demo", buffer.getvalue()) == 1
    assert (tmp_path / "demo" / name).read_text() == "TOKEN=example\n"


def test_nested_non_template_env_is_refused(tmp_path, fake):
    """A non-template .env* is refused anywhere by name, mirroring packaging — not
    just at the root."""
    _link(tmp_path, fake)
    (tmp_path / "demo" / "fixtures").mkdir()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/fixtures/.env", "FIXTURE=1\n")
    with pytest.raises(cloud.CloudError, match="protected"):
        cloud._extract_zip(tmp_path / "demo", buffer.getvalue())
    assert not (tmp_path / "demo" / "fixtures" / ".env").exists()


def test_status_without_protocol_field_means_revision_one(tmp_path, fake):
    fake.protocol = None
    result = _link(tmp_path, fake)
    assert result["changed"] is True


def test_protocol_mismatch_is_a_friendly_error(tmp_path, fake):
    fake.protocol = cloud.PROTOCOL_VERSION + 1
    with pytest.raises(cloud.CloudError, match="upgrade hiveloom"):
        _link(tmp_path, fake)


def test_plain_http_to_a_remote_host_is_refused(tmp_path, fake):
    with pytest.raises(cloud.CloudError, match="allow-insecure-http"):
        cloud.link_harness(
            "http://cloud.test", "hl_link_tok", tmp_path / "demo", opener=fake.opener
        )
    assert fake.requests == []  # refused before any request, token never sent


def test_plain_http_is_allowed_with_the_flag_or_locally(tmp_path, fake):
    cloud.link_harness(
        "http://cloud.test",
        "hl_link_tok",
        tmp_path / "a",
        allow_insecure_http=True,
        opener=fake.opener,
    )
    cloud.link_harness(
        "http://localhost:8000", "hl_link_tok", tmp_path / "b", opener=fake.opener
    )
    assert (tmp_path / "a" / "harness.yaml").exists()
    assert (tmp_path / "b" / "harness.yaml").exists()


class _LocalServer:
    """A real HTTP server on 127.0.0.1 that records the Authorization it sees.

    ``routes`` maps a path to ``(status, headers, body)``; any other path 404s.
    """

    def __init__(self):
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.seen: list[tuple[str, str | None]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                server.seen.append((self.path, self.headers.get("Authorization")))
                status, headers, body = server.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

            def log_message(self, *_args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


@pytest.fixture()
def servers(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    started: list[_LocalServer] = []

    def start() -> _LocalServer:
        started.append(_LocalServer())
        return started[-1]

    yield start
    for server in started:
        server.close()


def _serve_fake_cloud(server: _LocalServer, fake: FakeCloud, prefix: str = "") -> None:
    status = {"slug": fake.slug, "version_hash": fake.version, "protocol": fake.protocol}
    server.routes[f"{prefix}/api/link/status"] = (200, {}, json.dumps(status).encode())
    server.routes[f"{prefix}/api/link/pull"] = (200, {}, fake._zip())
    server.routes[f"{prefix}/api/link/traces"] = (200, {}, b'{"run_count": 0}')


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    ("path", "method"),
    [("/api/link/status", "GET"), ("/api/link/pull", "GET"), ("/api/link/traces", "POST")],
)
def test_cross_host_redirect_never_forwards_the_token(servers, fake, path, method, code):
    # For these codes the handler refuses the redirect before urllib applies its
    # own POST semantics: 307/308 reject a POST redirect outright, 301/302/303
    # downgrade a POST to a bodyless GET.
    origin, landing = servers(), servers()
    _serve_fake_cloud(landing, fake, prefix="/land")
    origin.routes[path] = (code, {"Location": f"{landing.url}/land{path}"}, b"")
    link = cloud.Link(base_url=origin.url, token="hl_link_tok")
    payload = {"files": []} if method == "POST" else None
    with pytest.raises(cloud.CloudError, match="refusing to follow a redirect"):
        cloud._request(link, path, method=method, payload=payload)
    assert origin.seen == [(path, "Bearer hl_link_tok")]
    assert landing.seen == []  # the redirect was never followed


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_to_another_host_is_refused_for_every_3xx(servers, fake, code):
    origin, landing = servers(), servers()
    _serve_fake_cloud(landing, fake)
    # Another host *name* for the same machine (localhost vs 127.0.0.1) is
    # already a different origin. The two servers also bind different ports;
    # the port-only case is covered separately below.
    other = landing.url.replace("127.0.0.1", "localhost")
    origin.routes["/api/link/status"] = (code, {"Location": f"{other}/api/link/status"}, b"")
    link = cloud.Link(base_url=origin.url, token="hl_link_tok")
    with pytest.raises(cloud.CloudError, match="refusing to follow a redirect"):
        cloud.remote_status(link)
    assert landing.seen == []


def test_redirect_to_another_port_on_the_same_host_is_refused(servers, fake):
    origin, landing = servers(), servers()
    _serve_fake_cloud(landing, fake)
    assert urlsplit(origin.url).hostname == urlsplit(landing.url).hostname
    origin.routes["/api/link/status"] = (
        302,
        {"Location": f"{landing.url}/api/link/status"},
        b"",
    )
    link = cloud.Link(base_url=origin.url, token="hl_link_tok")
    with pytest.raises(cloud.CloudError, match="refusing to follow a redirect"):
        cloud.remote_status(link)
    assert landing.seen == []


def test_redirect_to_an_unparseable_location_is_refused(servers):
    origin = servers()
    # urllib accepts this Location, but its port cannot be parsed.
    origin.routes["/api/link/status"] = (
        302,
        {"Location": "http://127.0.0.1:notaport/api/link/status"},
        b"",
    )
    link = cloud.Link(base_url=origin.url, token="hl_link_tok")
    with pytest.raises(cloud.CloudError, match="refusing to follow a redirect to an unparseable"):
        cloud.remote_status(link)
    assert origin.seen == [("/api/link/status", "Bearer hl_link_tok")]


def test_same_host_redirect_is_followed_with_the_token(servers, fake, tmp_path):
    server = servers()
    _serve_fake_cloud(server, fake, prefix="/v2")
    server.routes["/api/link/status"] = (302, {"Location": "/v2/api/link/status"}, b"")
    server.routes["/api/link/pull"] = (
        307,
        {"Location": f"{server.url}/v2/api/link/pull"},
        b"",
    )
    result = cloud.link_harness(server.url, "hl_link_tok", tmp_path / "demo")
    assert result["changed"] is True
    assert (tmp_path / "demo" / "harness.yaml").exists()
    assert ("/v2/api/link/status", "Bearer hl_link_tok") in server.seen
    assert ("/v2/api/link/pull", "Bearer hl_link_tok") in server.seen


def test_insecure_http_link_works_over_a_real_connection(servers, fake, tmp_path):
    server = servers()
    _serve_fake_cloud(server, fake)
    cloud.link_harness(server.url, "hl_link_tok", tmp_path / "demo", allow_insecure_http=True)
    assert cloud.pull(tmp_path / "demo")["changed"] is False
    assert all(auth == "Bearer hl_link_tok" for _, auth in server.seen)


def test_run_sync_flag_requires_a_linked_dir(tmp_path):
    from typer.testing import CliRunner

    from hiveloom.cli import app

    result = CliRunner().invoke(
        app, ["run", str(tmp_path), "--input", "hi", "--sync", "--json"]
    )
    assert result.exit_code == 4
    assert "not linked" in result.output
