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
- Evolution is a **gated, versioned, auditable mutation**, not silent drift. The
  evolver can never change `guardrails`, `model`, or `logging.redact`; regenerated
  code hooks require explicit y/n approval; every applied change bumps an
  `# evolved: N` counter and records old→new version hashes in the Hive.

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
4. **Redeploy** the updated folder.
5. **Judge**: `hiveloom stats ./harness` reports success rate, cost, and turns
   **per version hash**. Because runs on the new harness land under a new hash,
   you can see whether the mutation actually helped. Rollback is reverting the
   folder to the previous `harness.yaml` (git makes this a one-liner); the version
   hash keeps the before/after comparable.

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
