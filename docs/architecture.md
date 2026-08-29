# hiveloom architecture

hiveloom's bet: the **harness** (tools, loop policy, context strategy,
guardrails, verification) governs agentic performance as much as the model. A
strong model or a human *generates* a harness; a small, cheap model *executes*
inside it; run traces are *memory* that feed an *evolution* loop.

## The pieces

```
                         hiveloom
  ┌────────────────────────────────────────────────────────────────┐
  │  spec/            the harness contract (pydantic) + YAML loader   │
  │    schema.py      every field/type/default — single source of     │
  │                   truth for schema, template, explain, generator  │
  │    loader.py      YAML <-> spec (round-trip safe), code-hook       │
  │                   import + signature checks                        │
  │    annotate.py    JSON schema, annotated template, `explain`       │
  │  catalog.py       catalog entries: tools/guardrails/validators/    │
  │                   policies/compaction/hooks/eval components         │
  │  ext.py           the open catalog: ExtensionAPI, pack/user/harness │
  │                   extension discovery, provider registry, model     │
  │                   pricing, blueprints — see docs/extending.md       │
  │                                                                     │
  │  construct.py     init/set/add/remove — the ONE path that builds   │
  │                   a harness (CLI + generator both call it),         │
  │                   validate-and-rollback per step                    │
  │  trust.py         trust store gating foreign harness folders        │
  │                                                                     │
  │  runtime:                                                           │
  │    models/        ModelProvider ABC → Claude | OpenAI-compat | Fake │
  │      capabilities declared/live probes, identity policy, cache      │
  │      router.py    which model is current, and which provider serves │
  │                   it — mid-run hot-swap at a turn boundary          │
  │    tools/         registry (active/deferred) + sandboxed builtins; │
  │                   ToolRegistry owns the MCP sync/async bridge       │
  │    context/       assembly, budgeting, pluggable compaction, skills │
  │    guardrails/    Allow/Block/Halt hooks (frozen from evolution)    │
  │    events.py      lifecycle event bus (spec `hooks:` + ambient)     │
  │    verify/        validators = the reward signal                    │
  │    loop/          engine + pluggable policies (react | plan | …)    │
  │      control.py   RunControl: stop · steer · switch playbook/model  │
  │    skills.py      progressive-disclosure SKILL.md folders           │
  │    runner.py      assemble + drive a run; `hiveloom run [--stream]` │
  │    serve.py       stdlib HTTP wrapper: POST /runs, GET /healthz     │
  │                                                                     │
  │  fork.py          re-enter a finished run at a model call; lineage  │
  │                                                                     │
  │  logging/         trace.py (append-only JSONL) + hive.py (SQLite)   │
  │      journal.py   the fold events → context state, + chain verify   │
  │                                                                     │
  │  generate/        strong model → construction plan → construct.py   │
  │                   (+ blueprints: house-style prompt fragments)      │
  │  evolve/          analyzer (Hive) → propose → gate → apply          │
  │    proposals.py   queue: create/list/get/apply/reject (Hive-backed) │
  │  package.py       portable <name>-<hash>.zip (+ Dockerfile, packs)  │
  │  serve/           HTTP surfaces (non-production)                    │
  │    simple.py      `hiveloom serve`: stdlib /runs + /healthz         │
  │                   — the rest is `hiveloom control-plane`, see       │
  │                   docs/control-plane.md:                            │
  │    keys.py        ed25519 keypairs, compact-JWT sign/verify         │
  │    auth.py        authorized-keys store, bearer verification        │
  │    runslots.py    bounded run concurrency for POST /run             │
  │    app.py         Starlette app: auth + spec lock + endpoints       │
  │  guide.py         AGENTS.md + skills/, packaged; `hiveloom guide`   │
  │  cli.py           Typer CLI over all of the above                   │
  │                                                                     │
  │  devtools/                                                          │
  │    ui/            the workbench — published to npm as               │
  │                   `hiveloom-workbench`; see docs/workbench.md       │
  └────────────────────────────────────────────────────────────────┘
```

## Data flow of a run

```
hiveloom run ./h --input notes.txt
  │
  ├─ load spec, resolve code hooks (fail fast)
  ├─ build: tool registry · guardrails · verifiers · context manager · trace
  │
  ├─ AgentLoop (react):
  │     before_model_call guardrails  ─┐
  │     context.assemble (compact?)    │  loops until: completion +
  │     provider.complete  ────────────┤  verify pass · max_turns ·
  │     after_model_response guardrails│  guardrail Halt
  │     dispatch tool_use → registry   │
  │       (before/after_tool_call)     │
  │     append tool_results ───────────┘
  │     on completion → verify → retry_with_feedback | success/fail
  │
  ├─ TraceWriter appends every step, hash-chained, to
  │    .hiveloom/traces/<run_id>.jsonl
  └─ auto-ingest the journal into the Hive
```

A run's identity is the `run_id`, and it is the *only* execution identity across
the CLI, the API, the Hive, the journal, and the workbench. Branching a run
always produces a derived run rather than a grouping above it.

`RunControl` (`loop/control.py`) is the operator seam into a run already in
flight: stop, addressable steering messages, a playbook switch, or a model swap,
all applied at the next turn boundary, where no model call or tool is
outstanding. `hiveloom serve`, the control plane, and the workbench are clients
of it, not privileged paths around it.

## Logging as memory (the journal and the Hive)

Every run is an append-only JSONL **journal** with a common envelope (`run_id`,
`harness_name`, `harness_version_hash`, `seq`, `timestamp`, `prev`) and typed
events (`run_started`, `context_append`, `context_system`, `context_tools`,
`model_call`, `model_response`, `tool_call`, `tool_update`, `tool_result`,
`guardrail_triggered`, `hook_triggered`, `hook_error`, `context_compaction`,
`playbook_switch`, `model_swap`, `verification_result`, `run_finished`).

Three properties make it more than a log, and each buys something concrete:

- **Progressive.** The conversation is appended once, message by message. A
  `model_call` references the folded context (`context_head`, `system_hash`,
  `tools_hash`, `messages_hash`) instead of re-snapshotting it, so the file is
  linear in turns rather than quadratic. `logging/journal.py` is the fold back
  to state, and it is the single implementation `trace --materialize`, `fork`,
  and the workbench all share.
- **Tamper-evident.** Each line carries `prev`, the sha256 of the line before
  it. `hiveloom trace --verify` walks the chain and names the first break.
- **Self-describing.** `run_started` carries the dumped spec plus a
  `path -> sha256` manifest of every behavioural file, so the harness that ran
  is reconstructible from the record — which is what makes forking possible at
  all.

`fork.py` turns that record back into somewhere to start from: it rebuilds the
harness that actually ran, verifies it against the manifest, folds the
conversation to a chosen model call, and writes an experiment under
`<harness>/.hiveloom/forks/<name>`. See [`journal.md`](journal.md).

The **Hive** is a SQLite index over those journals (`~/.hiveloom/hive.db`,
`$HIVELOOM_DB` to override), ingested idempotently by `run_id`. It answers,
**per version hash**, the questions evolution needs: success rate / cost /
turns, the most common failure verdicts and guardrail triggers, and the N most
recent failed traces with their verifier feedback. It also carries lineage
(`parent_run_id`, `forked_at_seq`), the run's `task` for search, and its
`model_path` — runs whose model moved mid-flight are held out of the
per-version fitness bucket and reported separately, because they did not
execute the harness as declared.

Identity is deliberately split before evidence reaches evolution:

- `schema_version` says which harness document contract was parsed;
- the behavior hash covers the validated spec and every referenced behavior
  file, including playbook prompts;
- the execution fingerprint adds the runtime, requested and effective models,
  run-only overrides, input digest, model path, and lineage.

Renaming legacy `version` to `schema_version` is a document migration, not a
behavior change. The behavior hash normalizes that one spelling transition so
old Hive buckets stay usable.

The Hive also derives a normalized friction index from redacted journal
events. A recovered output validation failure or tool retry remains separate
from final success and can be queried by category, component, model, time, and
recovery state. The index stores bounded summaries and fingerprints, not raw
tool or model payloads.

External scorers can attach immutable numeric observations to an indexed run.
These `RunMetric` records remain separate from binary deferred outcomes and
join to execution provenance through `run_id`. Aggregation groups by metric
name, source, scope, unit, and direction, so a case score cannot silently mix
with a run or eval score. Every group reports its sample and missing-value
counts; raw traces are not needed to regenerate the aggregate.

An eval document resolves outside the runtime loop. Its dataset loader creates
held-out `EvalCase` values; the normal harness path produces a public
`RunResult`; only then do scorers receive the case, result, verification
context, and artifacts. Scorer failures have their own receipts and cannot
rewrite the run status. Content digests cover the eval document, loaded
dataset, and scorer implementations before a batch starts.

The native eval runner expands that resolved contract into deterministic case
and repetition cells. It checkpoints an atomic manifest below
`HIVELOOM_HOME/evals`, keeps traces in the same durable managed tree, and
revalidates eval, harness, adapter, effective-model, and case identities before
resume. Infrastructure attempts have distinct run IDs; completed harness
outcomes and scorer receipts are never retried or conflated.

## Evolution and the safety boundary

`hiveloom evolve` reads the Hive's clustered failures, asks a strong model for a
minimal mutation, then **gates it in code**:

- `guardrails`, `model`, `logging.redact`, `extensions`, `hooks`,
  `mcp_servers`, and `evolution.auto_propose` (`schema.ALWAYS_FROZEN`) — plus
  any path the harness lists as `frozen` — can **never** be changed;
- accepted changes must fall within the harness's `mutable` set;
- regenerated code hooks always require explicit human approval.

Applied mutations bump `# evolved: N`, re-validate, and are recorded in the Hive
under a new version hash — which is exactly what lets `hiveloom stats` prove a
mutation helped. The runtime and packager fingerprint both the YAML spec and its
referenced local code, schemas, extensions, and skills, so a validator or hook
edit also creates a distinct version bucket.

The end-to-end **deploy-anywhere-keep-evolving loop** (run in prod on a cheap
model → collect in-folder traces → evolve deliberately on a dev/CI box → redeploy
→ judge by version hash) is documented in
[`deploying-and-evolving.md`](deploying-and-evolving.md).

## Design invariants

- **One construction path.** CLI and generator both go through `construct.py`;
  the loader is the single enforcement layer.
- **Schema is the single source of truth.** The JSON schema, annotated template,
  `explain`, and the generator meta-prompt are all derived from `spec/schema.py`.
- **The folder is the harness, not the runtime.** A harness is portable and
  versionable; `pip install hiveloom` (plus any packs named in `hiveloom.lock`)
  provides the engine wherever it lands.
- **Catalog-as-truth, and the catalog is open.** Nothing runnable exists that
  `hiveloom catalog` can't show; extensions register entries through
  `hiveloom.ext` and the generator sees them immediately
  (see [`extending.md`](extending.md)).
- **Extensions widen choice, never the evolution gate.** Registered providers,
  policies, and hooks expand what a human or generator may pick; the frozen
  paths and human code approval are untouched, and foreign folders are
  trust-gated before their code loads.
- **The record is checkable, not merely written.** Append-only is a claim the
  hash chain lets a reader test, and "we cannot tell" (a pre-1.0 trace with no
  chain) is reported as a distinct answer from "it was tampered with".
- **Evidence is bucketed by what actually executed.** A version hash covers the
  spec and its local code; a run whose model moved mid-flight is excluded from
  that bucket rather than averaged into it.
- **Safety invariants live in code, not convention** — see `docs/spec.md` and
  `evolve/evolver.py`.
