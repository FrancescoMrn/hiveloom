# ticket-triage

The MCP layer, end to end: the model's only data source is a
[FastMCP](https://gofastmcp.com) server (`mcp_server.py`) exposing dummy
support tickets from `data/tickets.jsonl`, and the run must emit one
validator-checked JSON triage report.

The server is declared under `mcp_servers` and launched by the harness itself
over stdio as `uv run --no-project --with fastmcp python mcp_server.py`, so
fastmcp does not need to be installed anywhere. `--no-project` matters: this
folder's own `pyproject.toml` pins `hiveloom`, and without the flag `uv run`
would try to resolve that project before starting the server. Its three tools
join the loop as `mcp__tickets__list_tickets`, `mcp__tickets__get_ticket`,
and `mcp__tickets__search_tickets`.

## Run it

```bash
hiveloom mcp list-tools --dir .   # discovery only: 3 tools, no model call
hiveloom run . --input "Triage all currently open tickets." --dry-run --json
cp .env.example .env              # add ANTHROPIC_API_KEY
hiveloom run . --input "Triage all currently open tickets." --json
hiveloom trace <run_id> --verify
```

A reference run on `claude-haiku-4-5` finished in 3 turns at under a cent:
the model listed the open tickets over MCP, read each body with
`get_ticket`, and returned all 6 open tickets — the security exposure and
the production outages marked `urgent` — with no invented ids.

## Changing it

Do not hand-edit `harness.yaml`. Make changes through the CLI — `hiveloom
set`, `hiveloom add`, `hiveloom remove` — which validates every mutation and
rolls back on error.
