# ticket-triage

> **Proves:** a harness can take its only data from an external MCP server it
> launches itself, fan independent reads out concurrently, and still return
> one validated report with no invented ids.

The model's only data source is a [FastMCP](https://gofastmcp.com) server
(`mcp_server.py`) exposing dummy support tickets from `data/tickets.jsonl`.

## Capabilities

- **MCP servers as tools** — declared under `mcp_servers` and launched by the
  harness over stdio as `uv run --no-project --with fastmcp python
  mcp_server.py`, so fastmcp needs no install; its three tools join the loop as
  `mcp__tickets__list_tickets`, `mcp__tickets__get_ticket`,
  `mcp__tickets__search_tickets`. ([extending.md](../../docs/extending.md))
- **Parallel tool execution** — `loop.tool_execution: parallel`: guardrails and
  hooks preflight every call in source order, the calls run concurrently, and
  results are finalized in source order.
- **Output verification** — a `regex_match` validator gates the report.

`--no-project` matters: this folder's own `pyproject.toml` pins `hiveloom`, and
without the flag `uv run` would try to resolve that project before starting
the server.

## Run it

Live: needs `ANTHROPIC_API_KEY` in `.env` (or any provider via
`--provider/--model`); a run costs under a cent.

```bash
hiveloom mcp list-tools --dir .   # discovery only: 3 tools, no model call
hiveloom run . --input-text "Triage all currently open tickets." --dry-run --json
cp .env.example .env              # add ANTHROPIC_API_KEY
hiveloom run . --input-text "Triage all currently open tickets." --json
hiveloom trace <run_id> --verify
```

## What to look for

- **Three turns.** Turn 1 lists the open tickets; turn 2 issues one
  `get_ticket` per open ticket *in a single model response*, executed
  concurrently; turn 3 is the report.
- **All six open tickets, once each**, the security exposure and the
  production outages marked `urgent`, and no ids that are not in the data.
  The reference run on `claude-haiku-4-5` (sequential, 3 turns, under a
  cent) and a parallel run on Ministral 8B via OpenRouter (3 turns, six
  concurrent reads) both returned exactly this.
- `trace --verify` confirms the journal's hash chain.

## Try this

- `hiveloom set loop.tool_execution sequential` and compare wall-clock time in
  the two journals.
- `hiveloom set mcp_servers.0.deferred true` and watch the model find the
  server's tools through `search_tools` instead of seeing them up front.
