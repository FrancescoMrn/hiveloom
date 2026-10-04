# ticket-triage

> **Proves:** a harness can take its only data from an external MCP server it
> launches itself, fan independent reads out concurrently, and still return
> one validated report with no invented ids.

The model's only data source is a [FastMCP](https://gofastmcp.com) server
(`mcp_server.py`) exposing a support queue from `data/tickets.jsonl`: 26
tickets, 21 of them open — production outages, a leaked API key, suspicious
logins, a duplicate charge, how-to questions and feature requests dressed up as
bugs.

## Capabilities

- **MCP servers as tools** — declared under `mcp_servers` and launched by the
  harness over stdio as `uv run --no-project --with fastmcp python
  mcp_server.py`, so fastmcp needs no install; its three tools join the loop as
  `mcp__tickets__list_tickets`, `mcp__tickets__get_ticket`,
  `mcp__tickets__search_tickets`. ([extending.md](../../docs/extending.md))
- **Parallel tool execution** — `loop.tool_execution: parallel`: guardrails and
  hooks preflight every call in source order, the calls run concurrently, and
  results are finalized in source order.
- **Output verification against the system of record** — a code validator
  (`validators/check_triage.py`) reads the same ticket data the server serves
  and fails the report for an open ticket left out, a closed or unknown id, a
  duplicate, or a label outside the allowed sets, naming the tickets so the
  retry fixes exactly that. Which category and priority a ticket deserves
  stays the model's judgement.

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
- **All 21 open tickets, once each**, the validator passing on the first
  attempt, the three security exposures (a public report link, a leaked API
  key, logins from an unknown country) and both production outages marked
  `urgent`. Two runs on `deepseek/deepseek-v4.1-flash` via OpenRouter returned
  exactly this, consistently, at about a fifth of a cent each.
- **Fork the report turn** (`hiveloom fork <run_id> --list`, then `--at` the
  last seq) and `hiveloom run <fork> --resume`: the reads are replayed verbatim
  and only the report is written again — the same input, one variable.
- `trace --verify` confirms the journal's hash chain.

## Try this

- `hiveloom set loop.tool_execution sequential` and compare wall-clock time in
  the two journals.
- `hiveloom set mcp_servers.0.deferred true` and watch the model find the
  server's tools through `search_tools` instead of seeing them up front.
