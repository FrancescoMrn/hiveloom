---
name: hiveloom-ship
description: >-
  Package a hiveloom harness into a portable artifact, deploy it (zip or
  Docker), trust foreign harness folders, and close the deploy-anywhere-
  keep-evolving loop by collecting traces back. Use when shipping a harness to
  another machine/CI/production, receiving one from elsewhere, or setting up
  the run→collect→evolve→redeploy cycle, and expose harnesses to other agents
  as MCP tools. Triggers: "package the harness", "deploy the harness",
  "hiveloom package/trust", "collect traces back", "mcp serve", "let my agent
  call the harness".
---

# Shipping a hiveloom harness

A harness folder is portable like a `docker-compose.yml`: it needs the runtime
(`pip install hiveloom`, plus any packs named in `hiveloom.lock`) wherever it
lands.

## Package

```bash
hiveloom package ./h                       # validates, then <name>-<version_hash>.zip
hiveloom package ./h --docker              # + runnable Dockerfile (ENTRYPOINT: hiveloom run . --json)
hiveloom package ./h --docker --runtime-wheel dist/hiveloom-<v>-py3-none-any.whl
                                           # embed a pre-release/private hiveloom wheel
```

Secrets (`.env`) and local run memory (`.hiveloom/`) are always excluded;
`.env.example` documents what the destination must provide. `hiveloom.lock`
records the extension packs the spec uses — install them with the runtime at
the destination, or a run fails naming the missing pack.

## Trust (receiving a harness)

Harness folders carry executable code. Folders built on this machine are
trusted automatically; a **foreign** folder (unzipped artifact, git clone) is
gated before any of its code loads:

```bash
hiveloom trust ./foreign-harness       # interactive machines
hiveloom run ./foreign --approve       # one-shot approval
HIVELOOM_TRUST=always hiveloom run .   # CI (or `never` to refuse)
```

Never blanket-trust on a user's behalf — surface what the folder's hooks do
first (read `tools/`, `validators/`, `hooks/`, `extensions:`).

## Serve to agents (MCP)

The agent-facing front door: each harness becomes an MCP tool an agent can
call, returning `{status, output, reason, cost_usd, turns, run_id, verdicts}`
— `status: success` means the output passed the harness's validators.

```bash
hiveloom mcp serve ./h ./other            # stdio; one run_<name> tool each
hiveloom registry add ./h                 # register once...
hiveloom mcp serve --registered           # ...serve everything registered
hiveloom mcp serve ./h --concurrency 2 --max-depth 3
                                          # bound parallel runs / delegation depth
HIVELOOM_API_KEY=k hiveloom mcp serve --registered --http --port 8765
                                          # streamable HTTP at /mcp, Bearer-gated
```

A run that could not start at all (missing key, untrusted folder) comes back
as `status: "error"` with the reason — data the calling agent can read, not a
protocol error. Startup failures go to stderr with an exit code; stdout stays
the protocol channel.

### Harness → harness

The caller can be a harness too — point its `mcp_servers` at a peer's server:

```bash
hiveloom add mcp-server --name peer --stdio-command hiveloom \
  --stdio-arg mcp --stdio-arg serve --stdio-arg /srv/harnesses/summarizer \
  --env-from-host ANTHROPIC_API_KEY=ANTHROPIC_API_KEY \
  --timeout-seconds 300 --dir ./h

# or over HTTP
hiveloom add mcp-server --name peer --url https://harnesses.internal/mcp \
  --header-env 'X-API-Key=HIVELOOM_API_KEY' --timeout-seconds 300 --dir ./h
```

- **Forward the key explicitly.** A stdio child gets a minimal environment
  (hiveloom adds only `HIVELOOM_HOME`/`HIVELOOM_DB`, so the peer shares this
  machine's Hive and trust store). API keys are never auto-forwarded: use
  `env_from_host_env`, or give the peer folder its own `.env`. Agent hosts
  (Claude Code/Desktop) also start `mcp serve` with a minimal environment.
- **Size the timeout.** `timeout_seconds` must cover the peer's whole run
  (>= its `max_wall_clock_seconds`); a caller timeout only *asks* the peer to
  stop at its next turn boundary.
- **The chain is bounded.** The caller's run id, harness identity, depth and
  chain travel in the request `_meta`: the peer's run is linked as a child
  (`hiveloom lineage`), and a chain deeper than `--max-depth` or one that
  revisits a harness is refused as `status: "error"`.
- **Artifacts survive the hop** — a peer run's artifacts come back on the
  caller's `RunResult.artifacts`.

Every server also exposes `list_harnesses`: the catalog with each harness's
measured fitness (runs, success rate, avg cost/turns from the Hive), so a
calling agent picks on evidence. Rules to preserve: input is always literal
text (a caller can never read server files by naming one); trust is enforced
per directory at startup and the command never prompts (stdout is the
protocol) — `hiveloom trust <dir>` first for foreign folders; a non-loopback
`--http` bind without `HIVELOOM_API_KEY` is refused. Runs delegated by other
agents land in the Hive like any other, so they feed evolution too.

## The deploy-anywhere-keep-evolving loop

The deployment never evolves itself; it only **emits signal**:

1. **Run in prod** (cheap executor model): `hiveloom run . --input FILE --json`.
   Every run appends to the in-folder `.hiveloom/traces/`.
2. **Collect** traces back to a dev/CI box — rsync, a mounted volume, object
   store, git; transport is intentionally left to your tooling. For Docker,
   mount `.hiveloom/traces` to a shared volume so all replicas feed one pool.
3. **Evolve deliberately** there: `hiveloom stats ./h` and `hiveloom evolve
   ./h` ingest the folder's traces on the fly, idempotently by `run_id` — safe
   to re-copy any number of times. (Gates: see the `hiveloom-evolve` skill.)
4. **Redeploy** the bumped folder (`# evolved: N+1`, new version hash).
5. **Judge** with `hiveloom stats ./h` — success/cost/turns **per version
   hash** proves whether the mutation helped. Rollback = revert the folder.

Keep `harness.yaml` + hooks in git (traces are gitignored by `init`);
evolution then produces a clean, reviewable diff.

## Known limits (don't work around silently)

The Hive is single-machine SQLite — many replicas can't write one Hive
concurrently; trace collection is manual; judging is human-in-the-loop.
Details and topologies: `docs/deploying-and-evolving.md`.
