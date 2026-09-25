"""Expose harnesses as MCP tools: the agent-facing front door.

``hiveloom mcp serve DIR [DIR ...]`` serves one ``run_<name>`` tool per
harness (stdio by default, streamable HTTP with ``--http``), so any
MCP-capable agent can delegate a task to a harness — a versioned, guardrailed,
verified executor — instead of improvising the task itself. The tool
description is the harness description, and the result is structured:
``status`` says whether the output passed the harness's validators, so the
calling agent never has to guess. A ``list_harnesses`` tool carries the
catalog plus each harness's measured fitness from the Hive, and
``--registered`` serves everything in the local registry (:mod:`hiveloom.registry`).

Mirrors the HTTP servers' caller contract: input is always literal text
(``literal_input=True``), because a remote caller must never be able to read
server files by sending a string that happens to name one. Trust is enforced
per harness directory before any of its code loads, exactly as ``run`` does.

The blocking run executes in a worker thread (``anyio.to_thread``), so long
harness runs do not stall the protocol loop, and every failure is returned as
data (``status: "error"`` with a ``reason``) rather than raised: a caller on
the far end of a pipe cannot read this process's traceback.

Harness-to-harness delegation rides the request ``_meta``: a hiveloom harness
calling one of these tools sends its own run id, harness id, depth and chain
under :data:`DELEGATION_META_KEY`, so the child's run is linked to its parent
in the Hive and a runaway graph is refused by ``max_depth``/cycle check before
it spends anything.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from hiveloom import runner, trust
from hiveloom.errors import SpecError
from hiveloom.loop.control import RunControl
from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks
from hiveloom.tools.mcp import ARTIFACT_ENVELOPE

logger = logging.getLogger("hiveloom.serve.mcp")

# The request `_meta` key carrying one harness's delegation lineage to the
# next. Namespaced so an ordinary MCP client can never trip it, and mirrored
# by the client half in :mod:`hiveloom.tools.mcp`.
DELEGATION_META_KEY = "hiveloom"
DELEGATION_KIND = "delegation"

# How many harnesses may sit on one delegation chain (A -> B -> C is depth 3).
# A bound is required, not decoration: harness-to-harness delegation is a
# graph a remote caller drives, and every hop spends real model budget.
DEFAULT_MAX_DEPTH = 3

_INSTRUCTIONS = (
    "Each run_* tool runs one hiveloom harness: a versioned, guardrailed agent "
    "that performs a single task and verifies its own output. Pass the task "
    "input as literal text. A result with status 'success' passed the "
    "harness's validators; any other status explains itself via 'reason' and "
    "'verdicts'. Call list_harnesses first when unsure which harness fits: it "
    "includes each harness's measured success rate and average cost. Failed "
    "runs are recorded and drive the harness's evolution — they are signal, "
    "not noise. Nothing raises: a run that could not start at all comes back "
    "as status 'error' with the reason."
)


def _sanitize(name: str) -> str:
    """Map a harness name onto the MCP tool-name charset ([a-zA-Z0-9_-])."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return cleaned or "harness"


def build_mcp_server(
    harness_dirs: Sequence[str | Path],
    *,
    provider_factory: Callable[[], Any] | None = None,
    approve_trust: Callable[[str], bool] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    concurrency: int | None = None,
) -> MCPServer:
    """Build an :class:`MCPServer` exposing ``run_<name>`` for each harness.

    Every directory is trust-gated and its spec validated eagerly, so a broken
    or untrusted harness fails at startup — not on the first tool call from a
    remote agent. ``provider_factory`` is the test seam: called once per run,
    returning a ``ModelProvider`` (``None`` means the spec's own provider).

    ``max_depth`` bounds a harness-to-harness delegation chain (see
    :data:`DEFAULT_MAX_DEPTH`); ``concurrency`` bounds how many runs this
    process executes at once (``None`` — the default — is unlimited, as
    before). A call that arrives while the bound is saturated waits for a
    slot, so the *caller's* ``timeout_seconds`` bounds the wait.
    """
    if max_depth < 1:
        raise SpecError("--max-depth must be at least 1")
    if concurrency is not None and concurrency < 1:
        raise SpecError("--concurrency must be at least 1")
    server = MCPServer(name="hiveloom", instructions=_INSTRUCTIONS)
    # Process-wide, not per harness: the bound exists to cap this machine's
    # concurrent model spend and memory, and every harness here shares it.
    semaphore = threading.BoundedSemaphore(concurrency) if concurrency else None
    seen: dict[str, Path] = {}
    catalog: list[dict[str, str]] = []
    for directory in harness_dirs:
        yaml_path = harness_path(directory)
        base = yaml_path.parent
        trust.ensure_trusted(base, approve_trust)
        spec = load_spec(yaml_path)
        resolve_hooks(spec, base)

        tool_name = f"run_{_sanitize(spec.name)}"
        if tool_name in seen:
            raise SpecError(
                f"harness name collision: '{base}' and '{seen[tool_name]}' both "
                f"expose the tool '{tool_name}'"
            )
        seen[tool_name] = base
        description = (
            f"{spec.description.strip()} "
            f"(hiveloom harness '{spec.name}'; returns a verified, structured result)"
        )
        server.add_tool(
            _make_run_tool(
                base,
                provider_factory,
                identity=spec.identity,
                max_depth=max_depth,
                semaphore=semaphore,
            ),
            name=tool_name,
            description=description,
            structured_output=True,
        )
        catalog.append(
            {
                "tool": tool_name,
                "name": spec.name,
                # The Hive key; fitness in list_harnesses is measured by it,
                # so a same-named harness elsewhere never inflates the numbers
                # a remote agent picks on.
                "key": spec.identity,
                "description": spec.description,
            }
        )
    server.add_tool(
        _make_list_tool(catalog),
        name="list_harnesses",
        description=(
            "List every harness this server offers, with its run_* tool name and "
            "measured fitness (total runs, success rate, average cost/turns from "
            "this machine's run history). Use it to pick the right harness."
        ),
        structured_output=True,
    )
    return server


def _make_list_tool(catalog: list[dict[str, str]]) -> Callable[[], dict[str, Any]]:
    def list_harnesses() -> dict[str, Any]:
        from hiveloom.logging.hive import Hive

        payload: list[dict[str, Any]] = []
        with Hive() as hive:
            for entry in catalog:
                stats = hive.summary(entry["key"])
                payload.append(
                    {
                        **{k: v for k, v in entry.items() if k != "key"},
                        "total_runs": stats["total_runs"],
                        "success_rate": round(stats["success_rate"], 3),
                        "avg_cost_usd": round(stats["avg_cost_usd"], 4),
                        "avg_turns": round(stats["avg_turns"], 1),
                    }
                )
        return {"harnesses": payload}

    return list_harnesses


def _error_result(reason: str) -> dict[str, Any]:
    """The run tool's failure shape: a *result*, not a protocol error.

    An exception escaping the tool reaches the calling agent as the SDK's
    opaque "Error executing tool run_X" while the real reason (a missing
    ``ANTHROPIC_API_KEY``, an untrusted directory) is only ever a traceback on
    this process's stderr — which, over stdio, nobody is reading. Same keys as
    a successful run so one reader parses both.
    """
    return {
        "status": "error",
        "output": "",
        "reason": reason,
        "turns": 0,
        "cost_usd": 0.0,
        "run_id": "",
        "verdicts": [],
    }


def _lineage_from_meta(ctx: Context) -> dict[str, Any] | None:
    """Read the caller's delegation lineage from this request's ``_meta``.

    Rebuilt field by field rather than trusted wholesale: the payload comes
    from a remote caller, and it ends up on this run's journal and in the Hive
    (as ``parent_run_id``). Anything unrecognized is dropped.
    """
    try:
        meta = ctx.request_context.meta
    except (AttributeError, ValueError):
        # No request context: an in-process call, not a wire call.
        return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get(DELEGATION_META_KEY)
    if not isinstance(raw, dict) or raw.get("kind") != DELEGATION_KIND:
        return None
    try:
        depth = int(raw.get("depth", 1))
    except (TypeError, ValueError):
        depth = 1
    chain = [str(item) for item in (raw.get("chain") or []) if isinstance(item, (str, int))]
    return {
        "kind": DELEGATION_KIND,
        "parent_run_id": str(raw.get("parent_run_id") or ""),
        "parent_harness_id": str(raw.get("parent_harness_id") or ""),
        "depth": max(depth, 1),
        "chain": chain,
    }


def _delegation_refusal(
    lineage: dict[str, Any] | None, identity: str, max_depth: int
) -> str | None:
    """Why this delegated call must not run, or ``None`` to proceed.

    Two ways a delegation graph runs away: it goes too deep, or it comes back
    to a harness already on the chain. Both are refused *before* the first
    paid turn, and refused as data so the caller can read the reason.
    """
    if lineage is None:
        return None
    if lineage["depth"] > max_depth:
        return (
            f"delegation refused: depth {lineage['depth']} exceeds this server's "
            f"max-depth {max_depth} (chain: {' -> '.join(lineage['chain']) or '-'})"
        )
    if identity in lineage["chain"]:
        return (
            f"delegation refused: cycle — harness '{identity}' is already running "
            f"in this chain ({' -> '.join(lineage['chain'])})"
        )
    return None


def _make_run_tool(
    base: Path,
    provider_factory: Callable[[], Any] | None,
    *,
    identity: str,
    max_depth: int,
    semaphore: threading.BoundedSemaphore | None,
) -> Callable[..., Any]:
    def _execute(
        input_value: str, lineage: dict[str, Any] | None, control: RunControl
    ) -> dict[str, Any]:
        if semaphore is not None:
            # Blocking on purpose, and in the worker thread: the protocol loop
            # stays responsive, and the caller's own timeout bounds the wait.
            semaphore.acquire()
        try:
            if control.stop_requested():
                # Cancelled while queued: never start a run nobody is waiting for.
                return _error_result("caller cancelled the call before the run started")
            provider = provider_factory() if provider_factory else None
            result = runner.run_harness(
                base,
                input_value,
                provider=provider,
                literal_input=True,
                lineage=lineage,
                control=control,
            )
        finally:
            if semaphore is not None:
                semaphore.release()
        payload: dict[str, Any] = {
            "status": result.status,
            "output": result.output,
            "reason": result.reason,
            "turns": result.turns,
            "cost_usd": result.cost_usd,
            "run_id": result.run_id,
            "verdicts": [
                {"verifier": v.verifier, "passed": v.passed, "feedback": v.feedback}
                for v in result.verdicts
            ],
        }
        if result.artifacts:
            # The caller channel, not the model channel: a harness behind MCP
            # keeps the artifact surface a local run has. The client half
            # (hiveloom.tools.mcp) lifts this back into Artifact objects and
            # keeps it out of the text the model is billed for.
            payload[ARTIFACT_ENVELOPE] = {
                "artifacts": [
                    # `kind`/`data` only: that is the Artifact contract the
                    # client half reconstructs. The producing tool's name is
                    # the peer's internal business, not the caller's.
                    {"kind": artifact.get("kind"), "data": artifact.get("data")}
                    for artifact in result.artifacts
                    if artifact.get("kind")
                ]
            }
        return payload

    async def run(input: str, ctx: Context) -> dict[str, Any]:
        lineage = _lineage_from_meta(ctx)
        refusal = _delegation_refusal(lineage, identity, max_depth)
        if refusal is not None:
            logger.warning("%s", refusal)
            return _error_result(refusal)
        control = RunControl()
        try:
            return await anyio.to_thread.run_sync(
                partial(_execute, input, lineage, control), abandon_on_cancel=True
            )
        except anyio.get_cancelled_exc_class():
            # The caller timed out or disconnected and the SDK cancelled this
            # handler. The run is on an abandoned worker thread, so ask it to
            # stop at its next turn boundary rather than letting it bill out
            # the rest of a task nobody will read.
            control.request_stop("the MCP caller cancelled this call (timeout or disconnect)")
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is data, not a protocol error
            # Plain stderr, not rich: this is a server process whose stdout is
            # the protocol channel and whose stderr is somebody's log file.
            logger.exception("run tool '%s' failed", identity)
            return _error_result(f"{type(exc).__name__}: {exc}")

    return run


def configure_stderr_logging() -> None:
    """Send this process's logs to stderr, plainly.

    stdout is the MCP protocol channel on stdio, so anything printed there
    corrupts the stream. And the MCP SDK installs a *rich* handler on the root
    logger when a server is constructed: a boxed, line-wrapped 700-line
    traceback is exactly what nobody can read in a parent agent's log, so it
    is replaced here with a plain one.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if type(handler).__module__.split(".")[0] == "rich":
            root.removeHandler(handler)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    if root.level in (logging.NOTSET, 0) or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def serve_stdio(
    harness_dirs: Iterable[str | Path],
    *,
    approve_trust: Callable[[str], bool] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    concurrency: int | None = None,
) -> None:
    """Serve the harnesses over stdio until the client disconnects."""
    server = build_mcp_server(
        list(harness_dirs),
        approve_trust=approve_trust,
        max_depth=max_depth,
        concurrency=concurrency,
    )
    # After the build: a startup failure is the CLI's to report (on stderr,
    # with an exit code), and configuring logging first would bind a handler
    # to a stream this process never gets to use.
    configure_stderr_logging()
    server.run("stdio")


def build_http_app(server: MCPServer, *, api_key: str | None, host: str = "127.0.0.1"):
    """The streamable-HTTP ASGI app, bearer-gated when ``api_key`` is set.

    Auth mirrors ``serve/simple.py``: ``Authorization: Bearer <key>`` or
    ``X-API-Key: <key>``, compared with :func:`hmac.compare_digest`. Like the
    simple server, this is defense in depth — a platform gateway should still
    be the primary auth layer (no TLS here).
    """
    import hmac

    inner = server.streamable_http_app(host=host)
    if not api_key:
        return inner

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            supplied = headers.get("x-api-key", "")
            auth = headers.get("authorization", "")
            if auth.startswith("Bearer "):
                supplied = supplied or auth[len("Bearer ") :]
            if not (supplied and hmac.compare_digest(supplied, api_key)):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error": "unauthorized"}',
                    }
                )
                return
        await inner(scope, receive, send)

    return guarded


def check_http_bind(host: str, api_key: str | None) -> None:
    """Refuse a non-loopback bind without a key: that would publish every
    registered harness to the network unauthenticated."""
    if api_key:
        return
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise SpecError(
            f"refusing to bind {host} without auth: set HIVELOOM_API_KEY, "
            "or bind 127.0.0.1"
        )


def serve_http(
    harness_dirs: Iterable[str | Path],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key: str | None = None,
    approve_trust: Callable[[str], bool] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    concurrency: int | None = None,
) -> None:
    """Serve the harnesses over streamable HTTP (``/mcp``) until interrupted."""
    import uvicorn

    check_http_bind(host, api_key)
    server = build_mcp_server(
        list(harness_dirs),
        approve_trust=approve_trust,
        max_depth=max_depth,
        concurrency=concurrency,
    )
    configure_stderr_logging()
    app = build_http_app(server, api_key=api_key, host=host)
    uvicorn.run(app, host=host, port=port, log_level="warning")
