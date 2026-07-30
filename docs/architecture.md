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
  │                   policies/compaction/hooks (builtin + registered)  │
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
  │    tools/         registry (active/deferred) + sandboxed builtins; │
  │                   ToolRegistry owns the MCP sync/async bridge       │
  │    context/       assembly, budgeting, pluggable compaction, skills │
  │    guardrails/    Allow/Block/Halt hooks (frozen from evolution)    │
  │    events.py      lifecycle event bus (spec `hooks:` + ambient)     │
  │    verify/        validators = the reward signal                    │
  │    loop/          engine + pluggable policies (react | plan | …)    │
  │    skills.py      progressive-disclosure SKILL.md folders           │
  │    runner.py      assemble + drive a run; `hiveloom run [--stream]` │
  │                                                                     │
  │  logging/         trace.py (append-only JSONL) + hive.py (SQLite)   │
  │                                                                     │
  │  generate/        strong model → construction plan → construct.py   │
  │                   (+ blueprints: house-style prompt fragments)      │
  │  evolve/          analyzer (Hive) → propose → gate → apply          │
  │    proposals.py   queue: create/list/get/apply/reject (Hive-backed) │
  │  package.py       portable <name>-<hash>.zip (+ Dockerfile, packs)  │
  │  serve/           HTTP control plane (non-production); see          │
  │                   docs/control-plane.md                             │
  │    keys.py        ed25519 keypairs, compact-JWT sign/verify         │
  │    auth.py        authorized-keys store, bearer verification        │
  │    runslots.py    bounded run concurrency for POST /run             │
  │    app.py         Starlette app: auth + spec lock + endpoints       │
  │  cli.py           Typer CLI over all of the above                   │
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
  ├─ TraceWriter emits every step to .hiveloom/traces/<run_id>.jsonl
  └─ auto-ingest the trace into the Hive
```

## Logging as memory (the Hive)

Every run is an append-only JSONL trace with a common envelope (`run_id`,
`harness_name`, `harness_version_hash`, `seq`, `timestamp`) and typed events
(`run_started`, `model_call`, `model_response`, `tool_call`, `tool_update`,
`tool_result`, `guardrail_triggered`, `hook_triggered`, `hook_error`,
`context_compaction`, `verification_result`, `run_finished`). The **Hive** is a SQLite index over those traces
(`~/.hiveloom/hive.db`, `$HIVELOOM_DB` to override), ingested idempotently by
`run_id`. It answers, **per version hash**, the questions evolution needs:
success rate / cost / turns, the most common failure verdicts and guardrail
triggers, and the N most recent failed traces with their verifier feedback.

## Evolution and the safety boundary

`hiveloom evolve` reads the Hive's clustered failures, asks a strong model for a
minimal mutation, then **gates it in code**:

- `guardrails`, `model`, `logging.redact`, and `extensions`
  (`schema.ALWAYS_FROZEN`) — plus any path the harness lists as `frozen` — can
  **never** be changed;
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
- **Safety invariants live in code, not convention** — see `docs/spec.md` and
  `evolve/evolver.py`.
