"""A tiny stdio MCP server used by tests/test_mcp.py.

Spawned as a real subprocess by the tests (``command=sys.executable, args=[this
file]``) so the MCP bridge/adapter can be exercised against a real, offline,
local server rather than a mock.
"""

from __future__ import annotations

from mcp.server import MCPServer

mcp = MCPServer("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def boom() -> str:
    """Always raises, to exercise the tool-error path."""
    raise RuntimeError("boom")


if __name__ == "__main__":
    mcp.run("stdio")
