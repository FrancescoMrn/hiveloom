"""MCP server consumption: sync/async bridge + tool adapter.

hiveloom's harness loop is synchronous; the ``mcp`` SDK is async end to end.
A single :class:`McpBridge` per run owns one ``anyio`` blocking portal (one
background event-loop thread) plus one ``ExitStack`` that keeps every
connected session — and, for stdio, its subprocess — alive for the run's
lifetime. A stdio server must not be respawned per tool call.

Tools only: sampling, resources, and prompts are explicit v1 non-goals.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp.types as mcp_types
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from hiveloom.errors import McpError
from hiveloom.spec.schema import McpHttpServerRef, McpServerRef, McpStdioServerRef
from hiveloom.tools.registry import Tool, ToolError, ToolResult

_MAX_DISCOVERED_TOOLS = 500
_NAME_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    """Strip/replace anything outside [a-zA-Z0-9_-].

    A malformed remote tool name can never produce an invalid tool name in a
    provider payload.
    """
    cleaned = _NAME_UNSAFE_RE.sub("_", name)
    return cleaned or "tool"


def _flatten_call_tool_result(result: mcp_types.CallToolResult) -> str:
    """Join text content blocks; fall back to structuredContent; never silently empty.

    Images/audio/embedded resources are v1 non-goals — ignored, but explained
    rather than dropped without a trace.
    """
    texts = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
    if texts:
        return "\n".join(texts)
    if result.structuredContent is not None:
        return json.dumps(result.structuredContent)
    if result.content:
        kinds = sorted({block.type for block in result.content})
        return f"[mcp tool returned non-text content: {', '.join(kinds)}]"
    return "[mcp tool returned no content]"


class McpBridge:
    """One portal + one ExitStack per :class:`~hiveloom.tools.registry.ToolRegistry`.

    Sessions are kept OPEN via ``portal.wrap_async_context_manager(...)``
    entered into the ExitStack, so a stdio subprocess is spawned once and
    reused for every tool call rather than respawned per call.
    """

    def __init__(self) -> None:
        self._portal_cm = start_blocking_portal(backend="asyncio")
        self._portal = self._portal_cm.__enter__()
        self._stack = ExitStack()

    def connect_stdio(self, params: StdioServerParameters, timeout: float) -> ClientSession:
        read, write = self._stack.enter_context(
            self._portal.wrap_async_context_manager(stdio_client(params))
        )
        session = self._stack.enter_context(
            self._portal.wrap_async_context_manager(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout))
            )
        )
        self._initialize(session, timeout)
        return session

    def connect_http(self, url: str, headers: dict[str, str], timeout: float) -> ClientSession:
        # `streamable_http_client` no longer takes headers/timeout directly
        # (that surface is deprecated); it takes a pre-configured httpx client.
        http_client = self._stack.enter_context(
            self._portal.wrap_async_context_manager(
                httpx.AsyncClient(
                    headers=headers, timeout=httpx.Timeout(timeout), follow_redirects=True
                )
            )
        )
        read, write, _get_session_id = self._stack.enter_context(
            self._portal.wrap_async_context_manager(
                streamable_http_client(url, http_client=http_client)
            )
        )
        session = self._stack.enter_context(
            self._portal.wrap_async_context_manager(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout))
            )
        )
        self._initialize(session, timeout)
        return session

    def _initialize(self, session: ClientSession, timeout: float) -> None:
        """Guard ``session.initialize()`` with a wall-clock timeout.

        A server that hangs on initialize would otherwise block
        ``build_registry`` (and therefore ``run``/``dry_run``) forever.
        """

        async def _init() -> None:
            with anyio.fail_after(timeout):
                await session.initialize()

        try:
            self._portal.call(_init)
        except TimeoutError as exc:
            raise McpError(f"mcp server did not initialize within {timeout}s") from exc

    def call(self, coro_fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``coro_fn(*args)`` on the portal's event loop and block for the result."""
        return self._portal.call(coro_fn, *args)

    def close(self) -> None:
        """Tear down every connected session/subprocess, then the portal.

        Best-effort: never raises, so it never masks a run's real outcome.
        """
        try:
            self._stack.close()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        try:
            self._portal_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


class McpToolAdapter(Tool):
    """Wraps one remote MCP tool as an ordinary hiveloom :class:`Tool`."""

    def __init__(
        self,
        *,
        bridge: McpBridge,
        session: ClientSession,
        server_name: str,
        remote_name: str,
        description: str,
        input_schema: dict[str, Any],
        transport: str,
        timeout: float,
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._remote_name = remote_name
        self._timeout = timeout
        self.name = f"mcp__{server_name}__{_sanitize(remote_name)}"
        self.description = description
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        # MCP's own `annotations` (destructiveHint/readOnlyHint) are
        # SELF-REPORTED by an untrusted server and must never be treated as a
        # security boundary. The real boundaries are ALWAYS_FROZEN (schema.py)
        # plus trust gating (hiveloom.trust) — these tags are informational.
        self.tags = ["mcp", f"mcp:{server_name}"] + (
            ["exec", "dangerous"] if transport == "stdio" else ["network"]
        )

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            result: mcp_types.CallToolResult = self._bridge.call(
                self._session.call_tool,
                self._remote_name,
                kwargs,
                timedelta(seconds=self._timeout),
            )
        except Exception as exc:  # noqa: BLE001 - any transport/timeout failure is a ToolError
            raise ToolError(f"mcp tool '{self.name}' failed: {exc}") from exc
        return ToolResult(content=_flatten_call_tool_result(result), is_error=bool(result.isError))


def _resolve_env(ref: McpStdioServerRef) -> dict[str, str]:
    """Literal ``env`` plus ``env_from_host_env`` secrets resolved from this process.

    ``stdio_client`` merges the result on top of a minimal safe base env
    (``mcp.client.stdio.get_default_environment``) — never a full host
    passthrough.
    """
    env = dict(ref.env)
    for target_var, host_var in ref.env_from_host_env.items():
        value = os.environ.get(host_var)
        if value is None:
            raise McpError(
                f"mcp server '{ref.name}': env_from_host_env wants host variable "
                f"'{host_var}' (for '{target_var}') but it is not set"
            )
        env[target_var] = value
    return env


def _resolve_headers(ref: McpHttpServerRef) -> dict[str, str]:
    """Literal ``headers`` plus ``header_env`` secrets resolved from this process."""
    headers = dict(ref.headers)
    for header_name, host_var in ref.header_env.items():
        value = os.environ.get(host_var)
        if value is None:
            raise McpError(
                f"mcp server '{ref.name}': header_env wants host variable "
                f"'{host_var}' (for header '{header_name}') but it is not set"
            )
        headers[header_name] = value
    return headers


def _list_all_tools(bridge: McpBridge, session: ClientSession) -> list[mcp_types.Tool]:
    """Follow ``ListToolsResult.nextCursor`` pagination to exhaustion, capped."""
    tools: list[mcp_types.Tool] = []
    cursor: str | None = None
    while True:
        result: mcp_types.ListToolsResult = bridge.call(session.list_tools, cursor)
        tools.extend(result.tools)
        if len(tools) > _MAX_DISCOVERED_TOOLS:
            raise McpError(
                f"mcp server reported more than {_MAX_DISCOVERED_TOOLS} tools; "
                "narrow it down with an explicit `tools` allowlist"
            )
        cursor = result.nextCursor
        if not cursor:
            break
    return tools


def connect_mcp_server(ref: McpServerRef, base_dir: Path, bridge: McpBridge) -> list[Tool]:
    """Connect to one declared MCP server and adapt its (allowlisted) tools.

    Called eagerly at ``build_registry`` time for both ``run`` and
    ``dry_run``: the model's tool payload needs full schemas before the first
    call, and one lifecycle path for every tool kind is simpler than a lazy
    one.
    """
    try:
        if isinstance(ref, McpStdioServerRef):
            transport = "stdio"
            cwd = str((base_dir / ref.cwd).resolve() if ref.cwd else base_dir.resolve())
            params = StdioServerParameters(
                command=ref.command, args=ref.args, env=_resolve_env(ref), cwd=cwd
            )
            session = bridge.connect_stdio(params, ref.timeout_seconds)
        else:
            transport = "http"
            session = bridge.connect_http(ref.url, _resolve_headers(ref), ref.timeout_seconds)

        remote_tools = _list_all_tools(bridge, session)
    except McpError:
        raise
    except Exception as exc:  # noqa: BLE001 - any connect/discovery failure is a runtime failure
        raise McpError(
            f"mcp server '{ref.name}' ({ref.transport}) failed to connect: {exc}"
        ) from exc

    allowlist = set(ref.tools) if ref.tools is not None else None
    adapters: list[Tool] = []
    for remote in remote_tools:
        if allowlist is not None and remote.name not in allowlist:
            continue
        adapters.append(
            McpToolAdapter(
                bridge=bridge,
                session=session,
                server_name=ref.name,
                remote_name=remote.name,
                description=remote.description
                or f"MCP tool '{remote.name}' from server '{ref.name}'.",
                input_schema=remote.inputSchema or {"type": "object", "properties": {}},
                transport=transport,
                timeout=ref.timeout_seconds,
            )
        )
    return adapters
