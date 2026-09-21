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
  The evolver can never change `id`, `guardrails`, `model`, `logging.redact`,
  `extensions`, `hooks`, `mcp_servers`, `evolution.auto_propose`,
  `evolution.trace_excerpts`, `evolution.objectives`, or the `memory` budgets;
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

`apply` needs an answer about the gated YAML changes: `--yes`, or an
interactive `y`. Declining applies nothing and leaves the proposal `pending`,
so it is still there to apply later. `--json` cannot prompt, so
`proposals apply --json` without `--yes` is a usage error (exit 3) rather than
a call that resolves the row without applying it.

There is no auto-apply: a human always calls `proposals apply` or
`proposals reject`. This is the additive extension the trace sink / networked
Hive / A/B runner discussion below anticipates — proposals live in the same
Hive as runs and evolutions, so a later automatic trigger or HTTP control plane
can populate the same queue without changing this review step.

### Proposing a durable lesson

A proposal can add to the harness's [durable memory](spec.md#memory) — a
standing constraint the runs keep rediscovering — instead of enlarging the
system prompt around it. The proposing model is told the current entry count
and the budgets, and appends by writing the reserved path segment `+`:

```json
{"path": "memory.entries.+",
 "value": {"id": "iso-dates", "kind": "rule", "title": "Dates in ISO 8601",
           "content": "Emit dates as YYYY-MM-DD.",
           "source": "evolve", "evidence": "4 runs failed date_format"},
 "rationale": "date_format rejected 4 of the last 9 runs"}
```

`+` means *append*, and it is resolved when the proposal is applied, not when
it is queued. An existing index replaces that entry, and a numeric index equal
to the current length still appends — but a position written at queue time goes
stale the moment another entry lands, and the same index then silently replaces
the lesson that took it. Write `+`.

Because an append says what it does rather than where it lands, it is also the
one proposal that survives the harness moving underneath it: **a proposal whose
every change is a `memory.entries.+` append, and that carries no code changes,
applies against a newer spec version too.** Everything else is still refused
with *harness has changed — regenerate*, because a mutation drafted against a
spec the harness no longer has is a fix for a harness that no longer exists.
Applying against a newer version is not applying unchecked: the gate, full
re-validation and rollback all run at apply, so an append that would break a
budget is refused there and leaves the row pending.

Everything else under `memory` — the budgets and `enabled` — is refused
by the gate, so a proposal can add a lesson but never widen the store that
holds it, and an entry that would break `max_entries`, `max_entry_chars`, or
`prompt_budget_chars` is rejected with the rest of its batch and leaves
`harness.yaml` untouched.

Reviewing one is the ordinary flow, plus one question:

```console
hiveloom proposals show ./harness <id> --json   # the entry, its rationale, the gate result
hiveloom memory list ./harness                  # what is already there, and how full it is
hiveloom proposals apply ./harness <id>
hiveloom memory show ./harness iso-dates        # the applied entry, with its evidence
```

Ask whether the lesson is *durable* (true of every future run, not of the runs
in this failure cluster), whether it belongs in memory rather than in a
validator (memory advises the model, it does not check its work), and whether
it is worth its tokens on every model call of every run. A lesson that stops
paying comes out with `hiveloom memory forget`, which is a spec change like any
other and moves the version hash accordingly.

### Lessons the executor itself proposes

A third source fills the same queue. Declare the opt-in
[`propose_memory`](spec.md#letting-the-executor-propose-a-lesson) tool and the
running model can offer a durable lesson mid-run — it discovered the
constraint, after all — without any new authority:

```bash
hiveloom add tool --builtin propose_memory --param max_per_run=2 --json
hiveloom proposals list ./harness --json      # rows with "trigger": "executor"
```

Such a row is a `MutationProposal` appending to `memory.entries.+` like the one
above, built with no strong-model call, gated at the moment it is queued, and
reviewed with the same four commands. Several lessons from one run compose:
applying the first moves the version hash, and the rest are appends, which
apply against the new one. Nothing about the review step changes:
`apply` still re-checks that the harness has not moved, re-gates, re-validates
and rolls back. What changes is where the queue's input comes from —
`--propose` (you), `auto_propose` (a failing run), and now the executor's own
`propose_memory` (a run that learned something).

Two things worth knowing when you review one:

- The `evidence` receipt names the run that proposed it, so
  `hiveloom trace <run_id>` shows the work that produced the lesson.
- Queue pressure is bounded by design: `max_per_run` caps one run, identical
  content dedups against the pending row, and runs launched by `hiveloom eval`
  never queue at all. If the queue still fills with restatements, the lesson to
  draw is usually about the harness's prompt, not about memory.

### Attempt memory and operator findings

Evolution includes the latest 12 resolved proposals for the same harness
identity, across versions. An `applied` or `rejected` record describes a review
decision, not measured improvement or regression. Rejected records retain the
proposed paths and rejection reason. This automatic history comes from the
proposal queue; direct `evolve --yes` applications are not queue entries.

Drivers that evaluate and keep or revert mutations can supply a newest-first
ledger via `analyze(..., attempt_history=[AttemptRecord(...)])`. Each record can
carry `outcome`, `rationale`, `changed_paths`, `yaml_diff`, `measured`,
`version_hash`, and `note`. An explicit empty list disables automatic queue
history. Record inconclusive measurements separately from measured regressions.
The ledger informs proposals; it does not automatically apply or reject them.

Operator findings can identify opportunities even when all recorded runs pass:

```console
hiveloom evolve ./harness --propose --note "Formatting passes; investigate retrieval coverage" --json
```

Repeat `--note` for multiple findings, or pass `analyst_notes` to `analyze`.
Findings cannot override frozen fields or hard metric constraints. Changing the
findings or supplied history gives a queued proposal a new deduplication key.

All prompt sections, including the current spec, history, and findings, pass
through the current redaction policy before text is shortened. Failure evidence
strings are capped at 1,500 characters; the report section is capped at 64,000
characters, history at approximately 24,000, and findings at 6,000. History
includes at most 12 attempts and 2,000 characters per diff. Cuts are marked, and
oversized JSON sections become a valid JSON object containing an explicitly
truncated excerpt. These evidence limits do not truncate the current spec.

Malformed proposals and correctable objective omissions receive feedback, with
at most three total proposing-model calls. Transport errors are left to the
provider's retry policy; inconsistent metric directions fail before calling
the model. Frozen-path gates, whole-spec validation, and code approval still
apply to every proposal.

### Bounded incident evidence

By default, evolution works from bounded Hive summaries and does not send
journal excerpts to the proposing model. A harness can opt in when a retry or
failure needs its immediate model and tool context to be understood:

```yaml
evolution:
  trace_excerpts:
    enabled: true
    max_incidents: 5
    before_events: 2
    after_events: 2
    max_event_bytes: 2048
    max_bytes: 32768
    max_tokens: 8192
```

The selector starts from indexed friction and failed runs, verifies the
journal identity and hash chain, and takes a small event window around each
incident. The current `logging.redact` policy is applied again before payloads
are truncated, hashed, or counted. `max_tokens` is a deterministic upper-bound
estimate of one token per four UTF-8 bytes, not a provider-specific tokenizer.
The smaller of the byte and token budgets is the hard serialized limit.

Missing, invalid, or retention-pruned journals degrade to their indexed
summary instead of aborting evolution. The proposing model receives the
packets inside the same untrusted-data boundary as the rest of the failure
report. The queued proposal stores only selection rules, run and friction IDs,
budgets, and a digest. It does not copy event payloads into the proposal queue.
`evolution.trace_excerpts` is frozen from evolution because it controls what
evidence may leave the local journal boundary.

### Metric objectives

External scorers can record numeric metrics after verification. To let the
evolver use those observations, configure evaluator-owned objectives through
the validated construction path:

```bash
hiveloom set evolution.objectives '[
  {"metric":"recall_at_5","direction":"maximize","unit":"ratio"},
  {"metric":"hallucination_rate","direction":"minimize","ceiling":0},
  {"metric":"billed_cost_usd","direction":"minimize","unit":"USD"}
]' --dir ./h --json
```

Evolution receives bounded aggregates with sample and missing-value counts,
behavior/model cohorts, execution fingerprints, and evidence run IDs. Eval
observations with matching case and repetition keys also produce paired
comparisons. Units, sources, scopes, recorded directions, and cohorts remain
separate. This avoids treating a model swap or a different evaluator series as
one continuous baseline.

Metric metadata is excluded from the proposing request. When trace excerpts
are enabled, they remain independently redacted and budgeted. Missing metrics
stay missing. A hard floor or ceiling violation must be addressed and cannot
be offset by lower cost or another soft improvement. The proposal records its
expected objective change plus the aggregate evidence receipt; paired history
supports comparison but does not prove causation.

`evolution.objectives` is always frozen. The proposing model cannot rewrite its
own scorecard. Metric-aware evolution still only drafts or presents a gated
change; applying a queued proposal remains an explicit command.

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
- **MCP server (agent-facing)** — `hiveloom mcp serve DIR [DIR ...]` exposes each
  harness as an MCP tool (`run_<name>`) so an MCP-capable agent can delegate a
  task and get back a structured, validator-checked result
  (`{status, output, reason, cost_usd, turns, run_id, verdicts}`). A
  `list_harnesses` tool carries the catalog with each harness's measured fitness
  (runs, success rate, avg cost/turns from the Hive) so callers pick on
  evidence. Register harnesses once (`hiveloom registry add <dir>`) and serve
  the whole registry with `--registered`; broken entries are skipped with a
  stderr warning. Transport is stdio by default — the right choice for a local
  agent's MCP config — or streamable HTTP at `/mcp` with `--http [--host]
  [--port]`, gated by `HIVELOOM_API_KEY` (`Bearer` or `X-API-Key`); a
  non-loopback bind without the key is refused. Same caller contract as the
  HTTP servers: input is always literal text, and trust is enforced per
  directory at startup (the command is non-interactive — stdout is the protocol
  channel — so approve foreign folders with `hiveloom trust` first). Delegated
  runs land in the Hive like any other, so tasks handed over by *other agents*
  drive the evolve loop too.
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
