# Deploying a harness and keeping it evolving

A harness is a self-contained folder (`harness.yaml` + hooks + `.hiveloom/traces/`).
Like a `docker-compose.yml`, it is portable and versionable but needs the engine
(`pip install hiveloom`) wherever it lands. This document describes the loop that
lets you **deploy a harness anywhere and still improve it over time.**

## Two properties make the loop work

1. **Traces travel by default.** Every `hiveloom run` writes an append-only JSONL
   trace into the harness's *in-folder* `.hiveloom/traces/`. Wherever the harness
   runs, it accumulates its own memory next to itself.
2. **Ingestion is idempotent by `run_id`.** The Hive can absorb the same trace
   directory any number of times without double-counting — which is what makes
   "copy it back and evolve" safe.

## Run and evolve are separated — on purpose

The running deployment does **not** evolve itself:

- **Running** uses a small, cheap executor model in the hot path.
- **Evolving** uses a strong model plus human approval for any code change — off
  the hot path, and never in production latency or cost.
- Evolution is a **gated, versioned, auditable mutation**, not silent drift.
  The evolver can never change `guardrails`, `model`, `logging.redact`,
  `extensions`, `hooks`, `mcp_servers`, or `evolution.auto_propose`;
  regenerated code hooks require explicit y/n approval; every applied change
  bumps an `# evolved: N` counter and records old→new version hashes in the
  Hive.

"Still evolving" therefore means the harness *emits the signal* (traces) wherever
it runs, and you close the loop deliberately — not that it mutates live.

## The loop

```
   PROD (anywhere)                          DEV / CI (evolution box)
   ─────────────────                        ────────────────────────
   hiveloom run . --input …    ── traces ─▶  hiveloom evolve ./harness
   (cheap model, gated)           flow        (re-ingests in-folder traces,
   writes .hiveloom/traces/                    strong model proposes a gated
        │                                      mutation, you approve)
        │                                           │
        └───────────  redeploy new version  ◀───────┘
                      (# evolved: N+1, new version hash)

   hiveloom stats ./harness  →  success rate / cost / turns PER VERSION HASH
                                = the fitness signal that proves it helped
```

Step by step:

1. **Run** the harness wherever it lives: `hiveloom run . --input FILE --json`.
   Each run auto-ingests into the local Hive and appends to `.hiveloom/traces/`.
2. **Collect** the traces on an evolution box. Both `hiveloom stats <dir>` and
   `hiveloom evolve <dir>` ingest a harness directory's in-folder traces on the
   fly (idempotent by `run_id`), so a harness that ran in production for a week
   can be copied back and analyzed against *real* failures.
3. **Evolve**: `hiveloom evolve ./harness` reads the Hive's clustered failures,
   asks a strong model for a minimal mutation, gates it (see above), and applies
   it — bumping `# evolved: N` and recording the new version hash.

   The analysis is scoped to the **current** version hash: only failures of the
   harness as it is right now. So the loop is genuinely a loop — after applying a
   mutation (or editing the folder by hand) you must run the harness again before
   there is anything to evolve from, and `evolve` says so:
   `nothing to evolve — no failures recorded for the current harness version
   (94 on earlier versions) — re-run the harness to collect fresh ones`.
   Pooling versions instead would keep proposing fixes for failures the previous
   mutation already repaired.
4. **Redeploy** the updated folder.
5. **Judge**: `hiveloom stats ./harness` reports success rate, cost, and turns
   **per version hash**. Because runs on the new harness land under a new hash,
   you can see whether the mutation actually helped. Rollback is reverting the
   folder to the previous `harness.yaml` (git makes this a one-liner); the version
   hash keeps the before/after comparable.

## Queuing proposals instead of applying them

`hiveloom evolve <dir> --propose` runs the same analyze → propose → gate
pipeline but queues the gated result in the Hive instead of applying it —
"auto-propose, human applies." A proposal is deduped by harness, spec version,
and failure signature, so re-running `--propose` against the same failure
state never pays for a second strong-model call; it just returns the existing
pending proposal.

```
hiveloom evolve ./harness --propose --json    # queue a gated proposal
hiveloom proposals list ./harness             # review what's pending
hiveloom proposals show ./harness <id>        # inspect rationale + gate result
hiveloom proposals apply ./harness <id>       # apply it (re-checks the harness
                                               # hasn't changed since drafting)
hiveloom proposals reject ./harness <id> --reason "not worth it"
```

There is no auto-apply: a human always calls `proposals apply` or
`proposals reject`. This is the additive extension the trace sink / networked
Hive / A/B runner discussion below anticipates — proposals live in the same
Hive as runs and evolutions, so a later automatic trigger or HTTP control plane
can populate the same queue without changing this review step.

## Auto-DRAFT (opt-in) — auto-APPLY still does not exist

A harness can opt in to drafting proposals automatically, right after a
failing run, via `evolution.auto_propose` in `harness.yaml`:

```yaml
evolution:
  auto_propose:
    enabled: true        # off by default
    min_failures: 5       # non-success runs of THIS version, since the last auto-proposal
    cooldown_hours: 24.0  # minimum gap between auto-drafted proposals
    model: null            # strong-model override; else the CLI/env default
```

This is a synchronous check at the tail of every completed `hiveloom run` —
no daemon, no scheduler, no background thread. It costs nothing for the
(default, disabled) common case: a single boolean check, no Hive query. When
enabled and a run fails, it counts recent failures **of the current version**
(same scope as `evolve`, so the threshold and the analysis always agree on which
failures count), checks the cooldown, and — if both pass — analyzes the Hive and
drafts a gated proposal with
`trigger="auto"`, deduped exactly like `--propose` (a second failing run
against the same failure state never pays for a second strong-model call).
**It only ever drafts.** Applying still requires `hiveloom proposals apply`.
A failure here (no API key, no network, a malformed model response) never
fails the run itself — same discipline as trace ingestion.

`cooldown_hours` cannot be removed: values below one minute are rejected, so
there's always a real floor on how often a harness can auto-draft. Each
qualifying failing run costs a strong-model call unless the dedup pre-check
catches it, so this is partly a spend guard; `min_failures` is the
complementary throttle if you want a different shape of restraint.

If you'd rather not pay this tail latency inside every run, leave
`auto_propose` off and instead schedule `hiveloom evolve <dir> --propose`
from cron (or your platform's scheduler) against the deployed harness — same
queue, same dedup, just triggered on your own cadence instead of per-run.

## Deployment topologies

- **Same box** — deploy the folder; runs ingest into the local Hive
  (`~/.hiveloom/hive.db`, override with `$HIVELOOM_DB`); `hiveloom evolve .` in
  place.
- **Prod + central evolution** — prod writes in-folder traces; sync
  `.hiveloom/traces/` back (rsync, a mounted volume, object-store sync, a
  git-of-traces) to a dev/CI box that runs `evolve`, then redeploy the bumped
  folder.
- **Docker** — `hiveloom package --docker` produces a runnable image
  (`ENTRYPOINT ["hiveloom", "run", ".", "--json"]`). For a release already on PyPI,
  the generated Dockerfile installs the locked `hiveloom` version. Before publishing
  (or when hiveloom is served from a private index), first run `uv build`, then pass
  `--runtime-wheel dist/hiveloom-<version>-py3-none-any.whl`; the artifact embeds that
  exact wheel. Its generated `.dockerignore` excludes `.env` and `.hiveloom/` from the
  build context. This embeds hiveloom itself; a fully air-gapped image also needs a
  wheelhouse for its third-party dependencies. Mount `.hiveloom/traces` to a shared
  volume so every replica feeds the same trace pool.
- **HTTP service** — `hiveloom package --docker --serve` builds the same image with
  `hiveloom serve` as the entrypoint: a long-lived container answering
  `POST /runs` (`{"input": "...", "stream": true}` streams trace events as NDJSON,
  final `run_result` line last) with `GET /healthz` for probes. Set
  `HIVELOOM_API_KEY` to require a `Bearer`/`X-API-Key` header on `/runs` — but treat
  that as defense in depth and put real authentication in your gateway. Run inputs
  over HTTP are always literal text, never file paths, so remote callers cannot read
  files out of the container. Because the harness executes model-written code hooks,
  the container *is* the sandbox: no docker socket, read-only filesystem outside the
  harness dir, and an egress policy. Traces still land in `.hiveloom/traces/`
  in-container, so the evolve loop works unchanged — or capture the `/runs` stream at
  your gateway and ship events wherever you like.
- **Git-backed harness** — keep `harness.yaml` + hooks in git (traces are
  gitignored by `init`). Evolution produces a clean diff (the `# evolved` counter
  and version hash); commit it and redeploy. Rollback = `git revert`.

## What is intentionally left to you

The artifact and memory models are portable and complete; the *transport* between
"runs anywhere" and "evolves centrally" is deliberately out of scope for now:

- **Trace collection is manual** — there is no built-in sync/push or pluggable
  trace sink; you move `.hiveloom/traces/` with whatever tooling you already use.
- **The Hive is single-machine SQLite** — many replicas cannot all write one Hive
  concurrently; a central multi-deployment Hive would need a networked backend.
- **Judging is human-in-the-loop** — `stats` gives you the per-version-hash signal
  to decide; automated A/B re-runs and auto-promote/rollback are future work (the
  schema's version-hash bucketing is designed to support them).

These are additive: the idempotent-by-`run_id`, version-hash-bucketed foundation
was chosen precisely so a trace sink, a networked Hive, or an A/B runner can be
bolted on without rethinking the loop.
