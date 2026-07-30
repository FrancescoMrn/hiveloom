"""Serve a harness over HTTP — the long-lived deployment interface.

``hiveloom run`` is one-shot; ``hiveloom serve`` wraps :func:`runner.run_harness`
in a small stdlib HTTP server so a packaged harness container can stay up and
answer requests. No extra dependencies: like ``OpenAICompatProvider`` this
sticks to the standard library, so the core install price does not change.

Endpoints:

- ``GET /healthz`` — liveness + identity, always unauthenticated (orchestrator
  probes must not need the API key).
- ``POST /runs`` — body ``{"input": "...", "stream": false}``. Non-stream
  responses mirror ``hiveloom run --json``; ``"stream": true`` responds with
  ``application/x-ndjson`` chunks: every trace event as a JSON line, then a
  final ``{"type": "run_result", ...}`` line — byte-compatible with
  ``hiveloom run --stream``.

Auth: when ``HIVELOOM_API_KEY`` is set (or ``api_key`` passed), ``/runs``
requires ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. The key is
env-only on the CLI so it never appears in process listings. A platform
gateway should still be the primary auth layer; this is defense in depth.

Run input over HTTP is always literal text: the local convenience of
``--input FILE`` must not let a remote caller read files out of the container.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hiveloom import __version__, trust
from hiveloom.errors import SpecError
from hiveloom.logging.trace import spec_version_hash
from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks

MAX_BODY_BYTES = 10 * 1024 * 1024  # a run input is text, not an upload


class HarnessServer(ThreadingHTTPServer):
    """HTTP server bound to one harness directory.

    ``provider_factory`` is the test seam: it is called once per run and must
    return a ``ModelProvider`` (``None`` means the spec's own provider).
    """

    daemon_threads = True

    def __init__(
        self,
        harness_dir: str | Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        api_key: str | None = None,
        concurrency: int = 1,
        provider_factory: Callable[[], Any] | None = None,
    ) -> None:
        yaml_path = harness_path(harness_dir)
        self.base_dir = yaml_path.parent
        # Fail fast: an untrusted or invalid harness must not come up half-alive.
        trust.ensure_trusted(self.base_dir)
        spec = load_spec(yaml_path)
        resolve_hooks(spec, self.base_dir)
        self.harness_name = spec.name
        self.version_hash = spec_version_hash(spec, self.base_dir)
        self.api_key = api_key if api_key is not None else os.environ.get("HIVELOOM_API_KEY")
        self.provider_factory = provider_factory
        if concurrency < 1:
            raise SpecError(f"serve concurrency must be >= 1 (got {concurrency})")
        self._slots = threading.Semaphore(concurrency)
        super().__init__((host, port), _Handler)


def result_payload(result: Any) -> dict[str, Any]:
    """The same shape ``hiveloom run --json`` emits, so clients need one parser."""
    return {
        "ok": result.status == "success",
        "status": result.status,
        "output": result.output,
        "turns": result.turns,
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "run_id": result.run_id,
        "trace_path": result.trace_path,
        "reason": result.reason,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: HarnessServer  # narrowed for type checkers

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # request logging is the gateway's job; traces cover the runs

    def _send_json(self, status: int, payload: dict[str, Any], *, retry_after: int = 0) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        key = self.server.api_key
        if not key:
            return True
        supplied = self.headers.get("X-API-Key", "")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = supplied or auth[len("Bearer ") :]
        return bool(supplied) and hmac.compare_digest(supplied, key)

    def _read_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- chunked NDJSON streaming ------------------------------------------ #

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _write_chunk(self, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/healthz":
            self._send_json(404, {"ok": False, "error": f"unknown path {self.path}"})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "name": self.server.harness_name,
                "version_hash": self.server.version_hash,
                "hiveloom_version": __version__,
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/runs":
            self._send_json(404, {"ok": False, "error": f"unknown path {self.path}"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "missing or invalid API key"})
            return
        body = self._read_body()
        if body is None or not isinstance(body.get("input"), str) or not body["input"]:
            self._send_json(
                400, {"ok": False, "error": 'body must be JSON with a non-empty "input" string'}
            )
            return
        if not self.server._slots.acquire(blocking=False):
            self._send_json(
                429, {"ok": False, "error": "server is at capacity"}, retry_after=5
            )
            return
        try:
            self._run(body["input"], stream=bool(body.get("stream")))
        finally:
            self.server._slots.release()

    def _run(self, input_value: str, *, stream: bool) -> None:
        from hiveloom import runner

        provider = self.server.provider_factory() if self.server.provider_factory else None

        if not stream:
            try:
                result = runner.run_harness(
                    self.server.base_dir, input_value, provider=provider, literal_input=True
                )
            except SpecError as exc:
                self._send_json(422, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - one request must not kill the server
                self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(200, result_payload(result))
            return

        client_gone = threading.Event()

        def on_event(event: Any) -> None:
            if client_gone.is_set():
                return  # keep the run (and its trace) alive even if the client left
            try:
                self._write_chunk(event.model_dump_json())
            except (BrokenPipeError, ConnectionResetError):
                client_gone.set()

        self._start_stream()
        try:
            result = runner.run_harness(
                self.server.base_dir, input_value, provider=provider,
                on_event=on_event, literal_input=True,
            )
            final = {"type": "run_result", **result_payload(result)}
        except Exception as exc:  # noqa: BLE001 - surface as the final stream line
            final = {
                "type": "run_result",
                "ok": False,
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if not client_gone.is_set():
            try:
                self._write_chunk(json.dumps(final))
                self._end_stream()
            except (BrokenPipeError, ConnectionResetError):
                pass


def serve_forever(server: HarnessServer) -> None:
    """Blocking accept loop; KeyboardInterrupt shuts down cleanly."""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
