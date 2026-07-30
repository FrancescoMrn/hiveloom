"""The hiveloom HTTP control plane: a deployed harness's CLI surface over HTTP.

**Explicitly non-production.** No TLS (tokens are cleartext on the wire), no
replay/nonce cache, single harness per process, localhost bind by default.
See ``docs/control-plane.md`` for the full endpoint table and limitations.

Every endpoint wraps an existing library function 1:1 (``runner``,
``construct``, ``evolve``, ``evolve.proposals``) — this module only adds
bearer auth, bounded run concurrency, the spec lock, and JSON/SSE framing
around them, so there is exactly one implementation of each behavior shared
with the CLI.

Two execution paths keep the async event loop non-blocking without
over-provisioning: ``POST /run`` goes through :class:`RunSlots`, whose
capacity is the whole point (bounded concurrency, 503 when full); everything
else (construct mutations, evolve/propose, proposals, stats, trace) is cheap
file/SQLite I/O dispatched via ``asyncio.to_thread`` — real work off the
loop, but no reason to compete with runs for the same bounded pool.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from hiveloom import construct
from hiveloom import evolve as evolve_mod
from hiveloom import runner as runner_mod
from hiveloom.errors import (
    AuthenticationError,
    AuthorizationError,
    HiveloomError,
    NotFoundError,
    SpecError,
)
from hiveloom.evolve import proposals as proposals_mod
from hiveloom.evolve.evolver import _read_counter
from hiveloom.generate.llm import StrongModel, build_strong_model
from hiveloom.logging.hive import Hive
from hiveloom.logging.trace import spec_version_hash
from hiveloom.models.provider import ModelProvider
from hiveloom.serve import auth as auth_mod
from hiveloom.serve.runslots import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    DEFAULT_MAX_QUEUED_RUNS,
    RunQueueFullError,
    RunSlots,
)
from hiveloom.spec.loader import harness_path, load_spec, validate_harness
from hiveloom.tools.builtin import _safe_path
from hiveloom.tools.registry import ToolError

_SSE_DONE = object()


# --------------------------------------------------------------------------- #
# Shared plumbing: body parsing, error -> status-code mapping
# --------------------------------------------------------------------------- #
def _parse_body(raw: bytes) -> dict[str, Any]:
    """Decode a request body as a JSON object (empty body -> ``{}``).

    Raises :class:`SpecError` for non-object JSON; a plain
    ``json.JSONDecodeError`` (a ``ValueError``) for malformed JSON, mapped to
    400 the same way any other caller mistake is — no special case needed.
    """
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        raise SpecError("request body must be a JSON object")
    return data


def _error_response(exc: Exception) -> JSONResponse:
    """Map a raised exception to the status-code table in docs/control-plane.md."""
    if isinstance(exc, RunQueueFullError):
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, NotFoundError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    if isinstance(exc, SpecError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if isinstance(exc, (KeyError, ValueError)):
        return JSONResponse({"ok": False, "error": str(exc).strip("'\"")}, status_code=400)
    if isinstance(exc, HiveloomError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": False, "error": str(exc) or type(exc).__name__}, status_code=500)


def _add_dispatch(harness_dir: str | Path, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """Dispatch ``POST /add/{kind}`` to the matching ``construct`` function.

    Mirrors ``cli.py``'s ``add`` sub-Typer commands' exact response shapes.
    """
    if kind == "tool":
        construct.add_tool(
            harness_dir,
            builtin=body.get("builtin"),
            code=body.get("code"),
            description=body.get("description"),
        )
        return {"ok": True, "added": "tool", "ref": body.get("builtin") or body.get("code")}

    if kind == "validator":
        construct.add_validator(
            harness_dir,
            builtin=body.get("builtin"),
            code=body.get("code"),
            description=body.get("description"),
            schema_file=body.get("schema_file"),
            pattern=body.get("pattern"),
            path=body.get("path"),
            command=body.get("command"),
        )
        return {"ok": True, "added": "validator", "ref": body.get("builtin") or body.get("code")}

    if kind == "guardrail":
        builtin = body.get("builtin")
        if not builtin:
            raise SpecError("'builtin' is required for a guardrail")
        before = construct.find_guardrails(harness_dir, builtin)
        construct.add_guardrail(
            harness_dir, builtin=builtin, value=body.get("value"), pattern=body.get("pattern")
        )
        after = construct.find_guardrails(harness_dir, builtin)
        if len(after) > len(before):
            return {"ok": True, "added": "guardrail", "ref": builtin}
        return {
            "ok": True,
            "replaced": "guardrail",
            "ref": builtin,
            "before": before,
            "after": after[0],
        }

    if kind == "hook":
        on = body.get("on")
        if not on:
            raise SpecError("'on' is required for a hook")
        construct.add_hook(
            harness_dir,
            on=on,
            builtin=body.get("builtin"),
            code=body.get("code"),
            description=body.get("description"),
        )
        ref = f"{on}:{body.get('builtin') or body.get('code')}"
        return {"ok": True, "added": "hook", "ref": ref}

    if kind == "skill":
        name = body.get("name")
        description = body.get("description")
        if not name or not description:
            raise SpecError("'name' and 'description' are required for a skill")
        construct.add_skill(harness_dir, name=name, description=description)
        return {"ok": True, "added": "skill", "ref": name}

    raise SpecError(f"unknown add kind '{kind}' (expected tool/validator/guardrail/hook/skill)")


def _require_proposal(hive: Hive, proposal_id: str) -> proposals_mod.ProposalRecord:
    """Fetch a proposal or raise :class:`NotFoundError` — shared by every
    ``/proposals/{id}...`` endpoint's existence check.
    """
    record = proposals_mod.get_proposal(hive, proposal_id)
    if record is None:
        raise NotFoundError(f"no proposal with id '{proposal_id}'")
    return record


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(
    harness_dir: str | Path,
    *,
    keys_path: str | Path | None = None,
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
    max_queued_runs: int = DEFAULT_MAX_QUEUED_RUNS,
    provider_factory: Callable[[Path], ModelProvider] | None = None,
    strong_model: StrongModel | None = None,
) -> Starlette:
    """Build the control-plane app for one harness directory.

    ``provider_factory``/``strong_model`` are test seams (mirroring
    ``run_harness(provider=...)``): when given, ``/run`` and
    ``/evolve/propose`` use them instead of resolving a real model, so the
    test suite runs fully offline.
    """
    base = harness_path(harness_dir).parent
    resolved_keys_path = auth_mod.authorized_keys_path(harness_dir, override=keys_path)
    # Closes the read-modify-write race on harness.yaml across concurrent
    # mutating endpoints (/set, /add/*, /remove, /proposals/{id}/apply).
    # Deliberately NOT held for a run's duration: /run loads its own spec
    # snapshot up front, so a concurrent mutation only ever affects later
    # runs — the same semantics as redeploying a harness folder underneath
    # a running process. Do not "fix" this into holding the lock around
    # /run; that would serialize every run behind every mutation for no
    # correctness benefit and would risk a real deadlock.
    spec_lock = threading.Lock()
    runslots = RunSlots(max_concurrent_runs, max_queued_runs)

    async def _check_scope(request: Request, scope: str) -> JSONResponse | None:
        """Verify the request's bearer token for ``scope``. ``None`` if authorized."""
        try:
            await asyncio.to_thread(
                auth_mod.verify_bearer,
                request.headers.get("authorization"),
                keys_path=resolved_keys_path,
                required_scope=scope,
            )
        except AuthenticationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
        except AuthorizationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        return None

    async def _handle(
        request: Request, *, scope: str | None, work: Callable[[], dict[str, Any]]
    ) -> JSONResponse:
        """Auth-check ``scope`` (skipped if ``None``), then run ``work`` off the
        event loop, mapping any exception it raises through :func:`_error_response`.
        """
        if scope is not None:
            auth_error = await _check_scope(request, scope)
            if auth_error is not None:
                return auth_error
        try:
            payload = await asyncio.to_thread(work)
        except Exception as exc:  # noqa: BLE001 - _error_response is the single mapping point
            return _error_response(exc)
        return JSONResponse(payload, status_code=200)

    def _run(run_input: str, *, on_event=None):
        """Build the provider (test seam or real) and call run_harness — the
        one call site shared by /run's sync and streaming branches.
        """
        provider = provider_factory(base) if provider_factory is not None else None
        return runner_mod.run_harness(
            harness_dir,
            run_input,
            provider=provider,
            strong_model=strong_model,
            resolve_input=False,
            on_event=on_event,
        )

    # ----------------------------------------------------------------- #
    # GET /health — no auth
    # ----------------------------------------------------------------- #
    async def health(request: Request) -> JSONResponse:
        def work() -> dict[str, Any]:
            spec = load_spec(harness_dir)
            return {
                "ok": True,
                "name": spec.name,
                "version_hash": spec_version_hash(spec, base),
                "evolved_counter": _read_counter(harness_path(harness_dir)),
            }

        return await _handle(request, scope=None, work=work)

    # ----------------------------------------------------------------- #
    # POST /run — scope "run"; the only endpoint bounded by RunSlots
    # ----------------------------------------------------------------- #
    async def run_endpoint(request: Request) -> Response:
        auth_error = await _check_scope(request, "run")
        if auth_error is not None:
            return auth_error

        try:
            body = _parse_body(await request.body())
        except (SpecError, ValueError) as exc:
            return _error_response(exc)

        input_text = body.get("input")
        input_file = body.get("input_file")
        if input_text is None and input_file is None:
            return _error_response(SpecError("provide 'input' or 'input_file'"))

        if input_file is not None:
            # Contained to the harness dir — never an arbitrary server-side
            # file read. `input` itself is ALWAYS literal text (below): a
            # caller-supplied string that happens to name a real file on the
            # server must never be silently read.
            try:
                resolved = await asyncio.to_thread(_safe_path, base, input_file)
            except ToolError as exc:
                return _error_response(SpecError(str(exc)))
            if not resolved.is_file():
                return _error_response(SpecError(f"input_file not found: {input_file}"))
            run_input = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
        else:
            run_input = input_text

        stream = request.query_params.get("stream", "").lower() == "true"

        if not stream:
            def work() -> dict[str, Any]:
                return runner_mod.run_result_payload(_run(run_input))

            try:
                future = runslots.submit(work)
            except RunQueueFullError as exc:
                return _error_response(exc)
            try:
                payload = await asyncio.wrap_future(future)
            except Exception as exc:  # noqa: BLE001 - _error_response is the single mapping point
                return _error_response(exc)
            return JSONResponse(payload, status_code=200)

        # Streaming: the on_event callback fires from the worker thread;
        # bridge it to an async generator with a thread-safe queue, ending in
        # the final run_result frame (or an error frame) then a sentinel —
        # matching `hiveloom run --stream`'s ordering.
        events: queue.Queue = queue.Queue()

        def on_event(event: Any) -> None:
            events.put(event.model_dump_json())

        def stream_work() -> None:
            try:
                result = _run(run_input, on_event=on_event)
                payload = runner_mod.run_result_payload(result)
                events.put(json.dumps({"type": "run_result", **payload}))
            except Exception as exc:  # noqa: BLE001 - already streaming; report inline
                events.put(json.dumps({"type": "error", "error": str(exc)}))
            finally:
                events.put(_SSE_DONE)

        try:
            runslots.submit(stream_work)
        except RunQueueFullError as exc:
            return _error_response(exc)

        async def event_stream():
            while True:
                item = await asyncio.to_thread(events.get)
                if item is _SSE_DONE:
                    break
                yield f"data: {item}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ----------------------------------------------------------------- #
    # GET /stats, GET /trace/{run_id}, POST /validate — scope "read"
    # ----------------------------------------------------------------- #
    async def stats_endpoint(request: Request) -> JSONResponse:
        def work() -> dict[str, Any]:
            with Hive() as hive:
                name = runner_mod.resolve_and_ingest(harness_dir, hive)
                summary = hive.summary(name)
                recent = hive.recent_failures(name, 5)
            return {"ok": True, **summary, "recent_failures": recent}

        return await _handle(request, scope="read", work=work)

    async def trace_endpoint(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def work() -> dict[str, Any]:
            with Hive() as hive:
                run = hive.get_run(run_id)
            if run is None:
                raise NotFoundError(f"run '{run_id}' not found in the Hive")
            events: list[dict[str, Any]] = []
            trace_file = Path(run.get("trace_path", ""))
            if trace_file.exists():
                events = [
                    json.loads(line)
                    for line in trace_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            return {"ok": True, "run": run, "events": events}

        return await _handle(request, scope="read", work=work)

    async def validate_endpoint(request: Request) -> JSONResponse:
        def work() -> dict[str, Any]:
            spec = validate_harness(harness_dir)
            return {"ok": True, "name": spec.name, "message": "harness is valid"}

        return await _handle(request, scope="read", work=work)

    # ----------------------------------------------------------------- #
    # POST /set, /add/{kind}, /remove — scope "mutate", under the spec lock
    # ----------------------------------------------------------------- #
    async def set_endpoint(request: Request) -> JSONResponse:
        raw = await request.body()

        def work() -> dict[str, Any]:
            body = _parse_body(raw)
            path = body.get("path")
            if not path:
                raise SpecError("'path' is required")
            with spec_lock:
                construct.set_value(harness_dir, path, body.get("value"))
            return {"ok": True, "path": path}

        return await _handle(request, scope="mutate", work=work)

    async def add_endpoint(request: Request) -> JSONResponse:
        kind = request.path_params["kind"]
        raw = await request.body()

        def work() -> dict[str, Any]:
            body = _parse_body(raw)
            with spec_lock:
                return _add_dispatch(harness_dir, kind, body)

        return await _handle(request, scope="mutate", work=work)

    async def remove_endpoint(request: Request) -> JSONResponse:
        raw = await request.body()

        def work() -> dict[str, Any]:
            body = _parse_body(raw)
            target = body.get("target")
            if not target:
                raise SpecError("'target' is required")
            with spec_lock:
                construct.remove_item(harness_dir, target)
            return {"ok": True, "removed": target}

        return await _handle(request, scope="mutate", work=work)

    # ----------------------------------------------------------------- #
    # Evolve / proposals queue
    # ----------------------------------------------------------------- #
    async def propose_endpoint(request: Request) -> JSONResponse:
        raw = await request.body()

        def work() -> dict[str, Any]:
            body = _parse_body(raw)
            with Hive() as hive:
                name = runner_mod.resolve_and_ingest(harness_dir, hive)
                report = evolve_mod.analyze(hive, name)
                if report.is_empty():
                    return {"ok": True, "changed": False, "reason": "no failures to learn from"}
                spec = load_spec(harness_dir)
                model = strong_model or build_strong_model(body.get("model"), base)
                record = proposals_mod.create_proposal(
                    hive, spec, harness_dir, report, model, trigger="http"
                )
            return {"ok": True, **proposals_mod.proposal_payload(record)}

        return await _handle(request, scope="evolve", work=work)

    async def proposals_list_endpoint(request: Request) -> JSONResponse:
        status = request.query_params.get("status")

        def work() -> dict[str, Any]:
            with Hive() as hive:
                name = runner_mod.resolve_and_ingest(harness_dir, hive)
                records = proposals_mod.list_proposals(hive, harness_name=name, status=status)
            return {"ok": True, "proposals": [proposals_mod.proposal_payload(r) for r in records]}

        return await _handle(request, scope="read", work=work)

    async def proposals_show_endpoint(request: Request) -> JSONResponse:
        proposal_id = request.path_params["proposal_id"]

        def work() -> dict[str, Any]:
            with Hive() as hive:
                record = _require_proposal(hive, proposal_id)
            return {"ok": True, **proposals_mod.proposal_payload(record)}

        return await _handle(request, scope="read", work=work)

    async def proposals_apply_endpoint(request: Request) -> JSONResponse:
        proposal_id = request.path_params["proposal_id"]
        raw = await request.body()

        def work() -> dict[str, Any]:
            body = _parse_body(raw)
            approved_files = set(body.get("approve_code") or [])

            def approve_code(change: Any) -> bool:
                return change.file in approved_files

            # Also under the spec lock: apply_proposal_by_id may write
            # harness.yaml, the same read-modify-write race /set etc. guard
            # against. Not held for the whole request — just this call.
            with spec_lock:
                with Hive() as hive:
                    _require_proposal(hive, proposal_id)
                    result = proposals_mod.apply_proposal_by_id(
                        hive,
                        harness_dir,
                        proposal_id,
                        approve_code=approve_code,
                        apply_yaml=bool(body.get("apply_yaml", False)),
                    )
            return {"ok": True, "proposal_id": proposal_id, **result.model_dump()}

        return await _handle(request, scope="evolve", work=work)

    async def proposals_reject_endpoint(request: Request) -> JSONResponse:
        proposal_id = request.path_params["proposal_id"]
        raw = await request.body()

        def work() -> dict[str, Any]:
            body = _parse_body(raw)
            with Hive() as hive:
                _require_proposal(hive, proposal_id)
                proposals_mod.reject_proposal(hive, proposal_id, body.get("reason", ""))
            return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}

        return await _handle(request, scope="evolve", work=work)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/run", run_endpoint, methods=["POST"]),
        Route("/stats", stats_endpoint, methods=["GET"]),
        Route("/trace/{run_id}", trace_endpoint, methods=["GET"]),
        Route("/validate", validate_endpoint, methods=["POST"]),
        Route("/set", set_endpoint, methods=["POST"]),
        Route("/add/{kind}", add_endpoint, methods=["POST"]),
        Route("/remove", remove_endpoint, methods=["POST"]),
        Route("/evolve/propose", propose_endpoint, methods=["POST"]),
        Route("/proposals", proposals_list_endpoint, methods=["GET"]),
        Route("/proposals/{proposal_id}", proposals_show_endpoint, methods=["GET"]),
        Route("/proposals/{proposal_id}/apply", proposals_apply_endpoint, methods=["POST"]),
        Route("/proposals/{proposal_id}/reject", proposals_reject_endpoint, methods=["POST"]),
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            runslots.shutdown()

    return Starlette(routes=routes, lifespan=lifespan)
