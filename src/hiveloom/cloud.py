"""Linked mode: pair a local harness folder with a hiveloom-cloud harness.

The web app mints a per-harness *link token* (``hl_link_…``). ``hiveloom
cloud link URL TOKEN`` stores it in ``<dir>/.hiveloom/cloud.json`` and pulls
the harness; from then on ``hiveloom cloud sync`` keeps the two sides
converged: it **pushes** the local run traces (so the web can evolve the
harness from them) and **pulls** the packaged harness whenever the remote
version hash moved (so local serving always runs what the web shows).

The web side is the source of truth for the artifact; the local side is the
source of truth for run traces. Pull therefore overwrites harness files but
never touches ``.hiveloom/`` (traces, this link config) or ``.env*`` —
exactly the paths the cloud's packaged zip excludes.

The link API is a small open protocol — any server implementing it can be
linked, not just hiveloom-cloud. The contract (endpoints, payloads, the
``protocol`` version field, and the trust model) lives in
``docs/sync-protocol.md``. Note the trust model: pulled harness files can
include extensions that execute locally, so only link servers you trust.

Only the standard library (urllib), mirroring the OpenAI-compat provider's
no-dependency stance.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

import yaml

from hiveloom.errors import HiveloomError

CONFIG_RELPATH = Path(".hiveloom") / "cloud.json"

# The link-API revision this client speaks. Servers advertise theirs in the
# `protocol` field of /api/link/status (absent means 1, the initial revision);
# see docs/sync-protocol.md for the full contract.
PROTOCOL_VERSION = 1

_DEFAULT_TRACE_DIR = "./.hiveloom/traces"
_TIMEOUT = 60
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class CloudError(HiveloomError):
    """A cloud link operation failed (bad token, unreachable host, …)."""


@dataclass
class Link:
    base_url: str
    token: str
    # The version hash of the last pull; lets sync decide without loading the
    # spec (which would import its extensions before a trust decision).
    version_hash: str | None = None


def _config_path(harness_dir: str | Path) -> Path:
    return Path(harness_dir) / CONFIG_RELPATH


def load_link(harness_dir: str | Path) -> Link:
    path = _config_path(harness_dir)
    if not path.exists():
        raise CloudError(
            f"{path.parent.parent} is not linked — run `hiveloom cloud link URL TOKEN` first"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return Link(
        base_url=data["base_url"],
        token=data["token"],
        version_hash=data.get("version_hash"),
    )


def save_link(harness_dir: str | Path, link: Link) -> None:
    path = _config_path(harness_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"base_url": link.base_url, "token": link.token}
    if link.version_hash:
        payload["version_hash"] = link.version_hash
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _request(
    link: Link,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    opener=None,
) -> tuple[int, bytes]:
    url = f"{link.base_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urlrequest.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {link.token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    open_fn = opener or urlrequest.urlopen
    try:
        with open_fn(request, timeout=_TIMEOUT) as response:
            return response.status, response.read()
    except urlerror.HTTPError as exc:
        if exc.code == 401:
            raise CloudError(
                "the link token was rejected (revoked or regenerated?) — "
                "mint a new one from the harness page and re-run `hiveloom cloud link`"
            ) from exc
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise CloudError(f"cloud request {method} {path} failed ({exc.code}): {detail}") from exc
    except urlerror.URLError as exc:
        raise CloudError(f"cannot reach {link.base_url}: {exc.reason}") from exc


def remote_status(link: Link, *, opener=None) -> dict[str, Any]:
    _, body = _request(link, "/api/link/status", opener=opener)
    status = json.loads(body)
    protocol = status.get("protocol", 1)
    if protocol != PROTOCOL_VERSION:
        raise CloudError(
            f"the server at {link.base_url} speaks link protocol {protocol}, "
            f"but this client speaks {PROTOCOL_VERSION} — upgrade "
            + ("hiveloom" if protocol > PROTOCOL_VERSION else "the server")
        )
    return status


def _trace_dir(harness_dir: Path) -> Path:
    """The spec's trace dir via plain YAML — no spec load, no extension imports."""
    configured = _DEFAULT_TRACE_DIR
    yaml_path = harness_dir / "harness.yaml"
    if yaml_path.exists():
        try:
            doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            configured = (doc.get("logging") or {}).get("trace_dir") or configured
        except yaml.YAMLError:
            pass
    path = Path(configured)
    return path if path.is_absolute() else harness_dir / path


def _extract_zip(harness_dir: Path, blob: bytes) -> int:
    """Unpack the packaged harness (top-level folder stripped) into the dir."""
    resolved_root = harness_dir.resolve()
    written = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts
            if len(parts) < 2:
                continue  # everything lives under "<name>/"
            rel = Path(*parts[1:])
            target = (harness_dir / rel).resolve()
            if not target.is_relative_to(resolved_root):
                raise CloudError(f"refusing zip entry escaping the harness dir: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            written += 1
    return written


def _check_scheme(base_url: str, allow_insecure_http: bool) -> None:
    """Refuse to send the link token over plain HTTP to a non-local host."""
    split = urlsplit(base_url)
    if split.scheme == "https":
        return
    if split.scheme != "http":
        raise CloudError(f"unsupported URL scheme {split.scheme!r} — use https://")
    if split.hostname in _LOCAL_HOSTS or allow_insecure_http:
        return
    raise CloudError(
        f"refusing to send the link token over plain HTTP to {split.hostname} — "
        "use https://, or pass --allow-insecure-http if you accept the risk"
    )


def link_harness(
    base_url: str,
    token: str,
    directory: str | Path | None = None,
    *,
    allow_insecure_http: bool = False,
    opener=None,
) -> dict[str, Any]:
    """Pair a directory with the remote harness and pull it. Returns a summary."""
    _check_scheme(base_url, allow_insecure_http)
    probe = Link(base_url=base_url, token=token)
    status = remote_status(probe, opener=opener)
    target = Path(directory) if directory is not None else Path(status["slug"])
    target.mkdir(parents=True, exist_ok=True)
    save_link(target, probe)
    result = pull(target, opener=opener)
    return {"dir": str(target), "slug": status["slug"], **result}


def pull(harness_dir: str | Path, *, opener=None) -> dict[str, Any]:
    """Fetch the packaged harness when the remote version differs."""
    base = Path(harness_dir)
    link = load_link(base)
    status = remote_status(link, opener=opener)
    remote_hash = status["version_hash"]
    if link.version_hash == remote_hash and (base / "harness.yaml").exists():
        return {"changed": False, "version_hash": remote_hash}
    _, blob = _request(link, "/api/link/pull", opener=opener)
    files = _extract_zip(base, blob)
    save_link(base, Link(link.base_url, link.token, version_hash=remote_hash))
    return {"changed": True, "version_hash": remote_hash, "files": files}


def push(harness_dir: str | Path, *, opener=None) -> dict[str, Any]:
    """Upload the local run traces (idempotent server-side by run_id)."""
    base = Path(harness_dir)
    link = load_link(base)
    directory = _trace_dir(base)
    trace_files = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    if not trace_files:
        return {"uploaded": 0, "run_count": 0}
    payload = {
        "files": [
            {"name": path.name, "content": path.read_text(encoding="utf-8")}
            for path in trace_files
        ]
    }
    _, body = _request(link, "/api/link/traces", method="POST", payload=payload, opener=opener)
    response = json.loads(body)
    return {"uploaded": len(trace_files), "run_count": response.get("run_count", 0)}


def sync(harness_dir: str | Path, *, opener=None) -> dict[str, Any]:
    """Push local traces, then pull the latest version — the everyday command."""
    pushed = push(harness_dir, opener=opener)
    pulled = pull(harness_dir, opener=opener)
    return {**pulled, "uploaded": pushed["uploaded"], "run_count": pushed["run_count"]}
