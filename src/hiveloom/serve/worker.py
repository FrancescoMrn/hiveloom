"""A stateless worker: harnesses that live in an application, run over HTTP.

The application stores each harness as a bundle (see :mod:`hiveloom.serve.bundle`),
the journal of every run and the proposals evolution drafted. Every request
carries the whole bundle; the worker materializes it in a fresh temporary
directory, does the one job asked, answers, and deletes the directory. It
keeps no Hive, no trace and no trust decision between requests: its
``HIVELOOM_HOME`` is a private temporary directory of its own, so the trust it
records for its temporary harnesses never reaches the machine's trust store.

Endpoints (all ``POST`` except ``/healthz``; JSON in, JSON out):

- ``GET /healthz`` — ``{ok, hiveloom_version}``, unauthenticated.
- ``/harness/create`` ``{name, task, ops?}`` → ``{bundle, version_hash}``.
- ``/harness/construct`` ``{bundle, ops}`` → ``{bundle, version_hash}``;
  any invalid op is a 422 and nothing is applied.
- ``/harness/validate`` ``{bundle}`` → ``{ok, errors, version_hash}``.
- ``/runs`` ``{bundle, input|messages, context?, stream?}`` → the run's
  result, or with ``stream`` NDJSON frames (``run_accepted``, every trace
  event, ``run_result``); the result carries ``journal`` (the run's redacted
  journal lines) and ``version_hash``.
- ``/evolve`` ``{bundle, journals, notes?, reflect?}`` → ``{proposals, note}``:
  the journals ingested into a temporary Hive, the failures of the current
  version analysed and a gated proposal drafted, and with ``reflect`` the
  lessons drafted from each failed run. Nothing is applied.
- ``/proposals/apply`` ``{bundle, proposal}`` → ``{bundle, version_hash,
  changed}``: the proposal re-gated against the bundle as it stands and
  applied through the evolver; code changes are always refused.

Ops for ``create``/``construct``: ``{"op": "set", "path", "value"}`` (``path``
``model`` takes a selector), ``{"op": "remove", "path"}``, ``{"op": "add",
"kind": "tool"|"validator"|"mcp-server"|"skill"|"guardrail"|"memory", ...}``
with the keyword arguments of the matching :mod:`hiveloom.construct` function
(never ``code``), and ``{"op": "write", "path", "content"}`` for a text file
under ``schemas/`` or ``skills/``.

Auth: ``HIVELOOM_API_KEY`` is required on every endpoint but ``/healthz``;
the server refuses to start without one unless ``insecure_no_auth`` is set.
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hiveloom import __version__, construct, trust
from hiveloom.errors import HiveloomError, SpecError
from hiveloom.serve.bundle import BundleError, check_bundle, extract, materialize

MAX_BODY_BYTES = 20 * 1024 * 1024
_ADD = {
    "tool": "add_tool",
    "validator": "add_validator",
    "mcp-server": "add_mcp_server",
    "skill": "add_skill",
    "guardrail": "add_guardrail",
    "memory": "add_memory_entry",
}
_WRITABLE = ("schemas/", "skills/")


def _version_hash(directory: Path) -> str:
    from hiveloom.logging.trace import spec_version_hash
    from hiveloom.spec.loader import load_spec

    return spec_version_hash(load_spec(directory), directory)


def apply_ops(directory: Path, ops: list[Any]) -> None:
    """Apply construction ops to a materialized harness, in order; the first invalid one raises."""
    if not isinstance(ops, list):
        raise SpecError('"ops" must be a list')
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise SpecError(f'op {i}: must be an object with an "op"')
        kind = op["op"]
        if kind == "set":
            path, value = op.get("path"), op.get("value")
            if not isinstance(path, str):
                raise SpecError(f"op {i}: set needs a path")
            if path == "model":
                construct.set_model(directory, str(value))
            else:
                construct.set_value(directory, path, value)
        elif kind == "remove":
            construct.remove_item(directory, str(op.get("path")))
        elif kind == "add":
            fn = _ADD.get(str(op.get("kind")))
            if fn is None:
                raise SpecError(f"op {i}: cannot add {op.get('kind')!r}")
            kwargs = {k: v for k, v in op.items() if k not in ("op", "kind")}
            if "code" in kwargs:
                raise BundleError(f"op {i}: a worker never adds code")
            getattr(construct, fn)(directory, **kwargs)
        elif kind == "write":
            path, content = op.get("path"), op.get("content")
            if (
                not isinstance(path, str)
                or not path.startswith(_WRITABLE)
                or not isinstance(content, str)
            ):
                raise SpecError(f"op {i}: write takes a text file under schemas/ or skills/")
            check_bundle({"files": {"harness.yaml": "{}", path: content}})
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        else:
            raise SpecError(f"op {i}: unknown op {kind!r}")
    from hiveloom.spec.loader import validate_harness

    validate_harness(directory)


class WorkerServer(ThreadingHTTPServer):
    """The stateless worker. One process, many applications' harnesses, nothing kept."""

    daemon_threads = True

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8790,
        *,
        api_key: str | None = None,
        insecure_no_auth: bool = False,
        concurrency: int = 4,
        provider_factory: Callable[[], Any] | None = None,
        strong_model: Any = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("HIVELOOM_API_KEY")
        if not self.api_key and not insecure_no_auth:
            raise SpecError(
                "the worker needs HIVELOOM_API_KEY (or --insecure-no-auth for local work)"
            )
        self.provider_factory = provider_factory
        self.strong_model = strong_model
        self._slots = threading.Semaphore(concurrency)
        # the worker's own home: trust, Hive defaults and caches never reach the machine's
        self.home = Path(tempfile.mkdtemp(prefix="hiveloom-worker-home-"))
        os.environ["HIVELOOM_HOME"] = str(self.home)
        os.environ.pop("HIVELOOM_DB", None)
        super().__init__((host, port), _Handler)

    def server_close(self) -> None:
        super().server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    # -- the jobs ---------------------------------------------------------- #

    def workspace(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="hiveloom-worker-", dir=self.home))

    def harness(self, bundle: Any) -> tuple[Path, Path]:
        """A fresh directory holding the bundle, trusted for this request alone."""
        root = self.workspace()
        directory = materialize(bundle, root / "harness")
        trust.record_trust(directory)
        return root, directory

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        name, task = body.get("name"), body.get("task")
        if not isinstance(name, str) or not isinstance(task, str):
            raise SpecError('create needs "name" and "task"')
        root = self.workspace()
        try:
            directory = root / "harness"
            directory.mkdir()
            trust.record_trust(directory)
            construct.init_harness(directory, name, task)
            apply_ops(directory, body.get("ops") or [])
            bundle = extract(directory)
            check_bundle(bundle)
            return {"bundle": bundle, "version_hash": _version_hash(directory)}
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def construct(self, body: dict[str, Any]) -> dict[str, Any]:
        root, directory = self.harness(body.get("bundle"))
        try:
            apply_ops(directory, body.get("ops") or [])
            bundle = extract(directory)
            check_bundle(bundle)
            return {"bundle": bundle, "version_hash": _version_hash(directory)}
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def validate(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            root, directory = self.harness(body.get("bundle"))
        except (BundleError, SpecError) as exc:
            return {"ok": False, "errors": [str(exc)], "version_hash": None}
        try:
            from hiveloom.spec.loader import validate_harness

            validate_harness(directory)
            return {"ok": True, "errors": [], "version_hash": _version_hash(directory)}
        except HiveloomError as exc:
            return {"ok": False, "errors": [str(exc)], "version_hash": None}
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def run(
        self, body: dict[str, Any], on_event: Callable[[Any], None] | None, run_id: str
    ) -> dict[str, Any]:
        from hiveloom import runner

        has_input = isinstance(body.get("input"), str) and bool(body["input"])
        has_messages = isinstance(body.get("messages"), list) and bool(body["messages"])
        if has_input == has_messages:
            raise SpecError('a run carries exactly one of a non-empty "input" or "messages"')
        context = body.get("context")
        if context is not None and not (
            isinstance(context, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in context.items())
        ):
            raise SpecError('"context" must be an object of strings')
        root, directory = self.harness(body.get("bundle"))
        try:
            kwargs: dict[str, Any] = (
                {"conversation": body["messages"]}
                if has_messages
                else {"input_value": body["input"], "literal_input": True}
            )
            if context is not None:
                kwargs["context"] = dict(context)
            provider = self.provider_factory() if self.provider_factory else None
            result = runner.run_harness(
                directory,
                provider=provider,
                ingest=False,
                on_event=on_event,
                run_id=run_id,
                approve_network=lambda _host: False,
                **kwargs,
            )
            payload = runner.run_result_payload(result)
            trace = Path(payload.get("trace_path") or "")
            payload["journal"] = (
                [
                    json.loads(line)
                    for line in trace.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if trace.is_file()
                else []
            )
            payload["version_hash"] = _version_hash(directory)
            payload.pop("trace_path", None)
            return payload
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def evolve(self, body: dict[str, Any]) -> dict[str, Any]:
        from hiveloom import runner
        from hiveloom.evolve import analyzer
        from hiveloom.evolve.proposals import create_proposal, proposal_payload
        from hiveloom.evolve.reflect import reflect_on_run
        from hiveloom.generate.llm import build_strong_model
        from hiveloom.logging.hive import Hive
        from hiveloom.logging.trace import spec_version_hash
        from hiveloom.spec.loader import load_spec

        journals = body.get("journals")
        if not isinstance(journals, list) or not all(isinstance(j, list) for j in journals):
            raise SpecError('"journals" must be a list of journals, each a list of lines')
        notes = [str(n) for n in body.get("notes") or []]
        root, directory = self.harness(body.get("bundle"))
        try:
            spec = load_spec(directory)
            traces = directory / spec.logging.trace_dir
            traces.mkdir(parents=True, exist_ok=True)
            for i, journal in enumerate(journals):
                run_id = next(
                    (
                        str(line.get("run_id"))
                        for line in journal
                        if isinstance(line, dict) and line.get("run_id")
                    ),
                    f"run_{i}",
                )
                (traces / f"{run_id}.jsonl").write_text(
                    "".join(json.dumps(line) + "\n" for line in journal), encoding="utf-8"
                )
            model = self.strong_model or build_strong_model(None, directory)
            out: list[dict[str, Any]] = []
            with Hive(root / "hive.db") as hive:
                name = runner.resolve_and_ingest(directory, hive)
                version = spec_version_hash(spec, directory)
                report = analyzer.analyze(
                    hive,
                    name,
                    version=version,
                    excerpt_config=spec.evolution.trace_excerpts,
                    redaction=spec.logging.redact,
                    objectives=spec.evolution.objectives,
                    evolution=spec.evolution,
                    analyst_notes=notes or None,
                )
                note = ""
                if report.is_empty():
                    note = "nothing to evolve — no failures recorded for the current version"
                else:
                    # a draft the gate refuses is said, and the night goes on to its lessons
                    try:
                        out.append(
                            proposal_payload(
                                create_proposal(
                                    hive, spec, directory, report, model, trigger="night"
                                )
                            )
                        )
                    except HiveloomError as exc:
                        note = f"no proposal: {exc}"
                if body.get("reflect"):
                    for failed in hive.recent_failures(spec.identity, 5):
                        for record in reflect_on_run(
                            hive, spec, directory, failed["run_id"], model
                        ):
                            out.append(proposal_payload(record))
            proposals = [
                {
                    "id": p["id"],
                    "trigger": p["trigger"],
                    "rationale": p["rationale"],
                    "changes": p["proposal"].get("yaml_changes", []),
                    "proposal": p["proposal"],
                    "gate": p["gate"],
                }
                for p in out
            ]
            return {"proposals": proposals, "note": note}
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        from hiveloom.evolve.evolver import MutationProposal, apply_proposal
        from hiveloom.logging.hive import Hive

        sent = body.get("proposal")
        raw = (
            sent.get("proposal")
            if isinstance(sent, dict) and isinstance(sent.get("proposal"), dict)
            else sent
        )
        proposal = MutationProposal.model_validate(raw)
        root, directory = self.harness(body.get("bundle"))
        try:
            with Hive(root / "hive.db") as hive:
                result = apply_proposal(
                    directory,
                    proposal,
                    hive=hive,
                    approve_code=lambda _change: False,
                    apply_yaml=True,
                )
            if not result.changed:
                raise SpecError(
                    "the proposal changed nothing: its gate refused every change, "
                    "or the harness already holds it"
                )
            bundle = extract(directory)
            check_bundle(bundle)
            return {"bundle": bundle, "version_hash": _version_hash(directory), "changed": True}
        finally:
            shutil.rmtree(root, ignore_errors=True)


class _Handler(BaseHTTPRequestHandler):
    server: WorkerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        key = self.server.api_key
        if not key:
            return True
        given = self.headers.get("Authorization", "")
        token = given[7:] if given.startswith("Bearer ") else self.headers.get("X-API-Key", "")
        return hmac.compare_digest(token.encode(), key.encode())

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/healthz":
            self._send(404, {"ok": False, "error": f"unknown path {self.path}"})
            return
        self._send(200, {"ok": True, "hiveloom_version": __version__})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        jobs = {
            "/harness/create": self.server.create,
            "/harness/construct": self.server.construct,
            "/harness/validate": self.server.validate,
            "/evolve": self.server.evolve,
            "/proposals/apply": self.server.apply,
        }
        if self.path not in (*jobs, "/runs"):
            self._send(404, {"ok": False, "error": f"unknown path {self.path}"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "missing or invalid API key"})
            return
        body = self._body()
        if body is None:
            self._send(400, {"ok": False, "error": "body must be a JSON object"})
            return
        if not self.server._slots.acquire(blocking=False):
            self._send(429, {"ok": False, "error": "the worker is at capacity"})
            return
        try:
            if self.path == "/runs":
                self._run(body)
                return
            self._send(200, jobs[self.path](body))
        except (BundleError, SpecError, ValueError) as exc:
            self._send(422, {"ok": False, "error": str(exc)})
        except HiveloomError as exc:
            self._send(422, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one request must not kill the worker
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self.server._slots.release()

    def _run(self, body: dict[str, Any]) -> None:
        from hiveloom import runner

        run_id = runner.new_run_id()
        if not body.get("stream"):
            self._send(200, self.server.run(body, None, run_id))
            return
        # validate before the stream opens, so a refusal is a status, not a frame
        if not (isinstance(body.get("input"), str) and body["input"]) and not (
            isinstance(body.get("messages"), list) and body["messages"]
        ):
            raise SpecError('a run carries a non-empty "input" or "messages"')
        check_bundle(body.get("bundle"))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(line: str) -> None:
            data = (line + "\n").encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        chunk(json.dumps({"type": "run_accepted", "run_id": run_id}))
        try:
            final = {
                "type": "run_result",
                **self.server.run(body, lambda event: chunk(event.model_dump_json()), run_id),
            }
        except Exception as exc:  # noqa: BLE001 - already streaming: the failure is the last frame
            final = {
                "type": "run_result",
                "ok": False,
                "status": "error",
                "run_id": run_id,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        chunk(json.dumps(final))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
