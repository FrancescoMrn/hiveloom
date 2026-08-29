"""A minimal FastMCP server exposing support tickets from a JSONL file.

Run by the harness over stdio: `uv run --no-project --with fastmcp python
mcp_server.py`. `--no-project` matters: the harness folder's own
pyproject.toml pins hiveloom, and without the flag `uv run` would try to
resolve that project before starting the server.
"""

import json
from pathlib import Path

from fastmcp import FastMCP

DATA = Path(__file__).parent / "data" / "tickets.jsonl"

mcp = FastMCP("tickets")


def _load() -> list[dict]:
    with DATA.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


@mcp.tool
def list_tickets(status: str | None = None) -> list[dict]:
    """List tickets (id, status, subject, customer), optionally filtered by
    status ('open' or 'closed')."""
    tickets = _load()
    if status is not None:
        tickets = [t for t in tickets if t["status"] == status]
    return [
        {k: t[k] for k in ("id", "status", "subject", "customer")}
        for t in tickets
    ]


@mcp.tool
def get_ticket(ticket_id: str) -> dict:
    """Return the full record for one ticket by id (e.g. 'TCK-1003')."""
    for t in _load():
        if t["id"] == ticket_id:
            return t
    return {"error": f"no ticket with id {ticket_id!r}"}


@mcp.tool
def search_tickets(query: str) -> list[dict]:
    """Case-insensitive substring search over subject and body; returns matching full records."""
    q = query.lower()
    return [
        t for t in _load()
        if q in t["subject"].lower() or q in t["body"].lower()
    ]


if __name__ == "__main__":
    mcp.run()
