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

The blocking run executes in a worker thread (the MCP SDK runs sync tool
functions via ``anyio.to_thread``), so long harness runs do not stall the
protocol loop.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from hiveloom import runner, trust
from hiveloom.errors import SpecError
from hiveloom.spec.loader import harness_path, load_spec, resolve_hooks

_INSTRUCTIONS = (
    "Each run_* tool runs one hiveloom harness: a versioned, guardrailed agent "
    "that performs a single task and verifies its own output. Pass the task "
    "input as literal text. A result with status 'success' passed the "
    "harness's validators; any other status explains itself via 'reason' and "
    "'verdicts'. Call list_harnesses first when unsure which harness fits: it "
    "includes each harness's measured success rate and average cost. Failed "
    "runs are recorded and drive the harness's evolution — they are signal, "
    "not noise."
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
) -> MCPServer:
    """Build an :class:`MCPServer` exposing ``run_<name>`` for each harness.

    Every directory is trust-gated and its spec validated eagerly, so a broken
    or untrusted harness fails at startup — not on the first tool call from a
    remote agent. ``provider_factory`` is the test seam: called once per run,
    returning a ``ModelProvider`` (``None`` means the spec's own provider).
    """
    server = MCPServer(name="hiveloom", instructions=_INSTRUCTIONS)
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
            _make_run_tool(base, provider_factory),
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


def _make_run_tool(
    base: Path, provider_factory: Callable[[], Any] | None
) -> Callable[[str], dict[str, Any]]:
    def run(input: str) -> dict[str, Any]:
        provider = provider_factory() if provider_factory else None
        result = runner.run_harness(base, input, provider=provider, literal_input=True)
        return {
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

    return run


def serve_stdio(
    harness_dirs: Iterable[str | Path],
    *,
    approve_trust: Callable[[str], bool] | None = None,
) -> None:
    """Serve the harnesses over stdio until the client disconnects."""
    server = build_mcp_server(list(harness_dirs), approve_trust=approve_trust)
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
) -> None:
    """Serve the harnesses over streamable HTTP (``/mcp``) until interrupted."""
    import uvicorn

    check_http_bind(host, api_key)
    server = build_mcp_server(list(harness_dirs), approve_trust=approve_trust)
    app = build_http_app(server, api_key=api_key, host=host)
    uvicorn.run(app, host=host, port=port, log_level="warning")
