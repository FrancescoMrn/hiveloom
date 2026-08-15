"""Linked-mode sync: link / pull / push / sync against a faked cloud."""

from __future__ import annotations

import io
import json
import zipfile
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


def test_run_sync_flag_requires_a_linked_dir(tmp_path):
    from typer.testing import CliRunner

    from hiveloom.cli import app

    result = CliRunner().invoke(
        app, ["run", str(tmp_path), "--input", "hi", "--sync", "--json"]
    )
    assert result.exit_code == 4
    assert "not linked" in result.output
