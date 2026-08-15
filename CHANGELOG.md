# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Run control**: a thread-safe `RunControl` channel (`hiveloom.loop.control`)
  lets a caller stop a running loop gracefully or inject steering messages,
  both consumed at the next turn boundary. A stopped run completes with the
  new status `"stopped"`, trace intact and partial output kept. `hiveloom
  serve` exposes it as `POST /runs/{run_id}/stop` and
  `POST /runs/{run_id}/messages`, and streaming responses now open with a
  `{"type": "run_accepted", "run_id": …}` line so clients can address a run
  while it is still going. `runner.run_harness` accepts `control=` and a
  pre-allocated `run_id=`. Steering messages are traced as `user_steer`.
- **Session-grouped traces**: `run_harness(session_id=…)` (and the optional
  `"session_id"` field on serve's `POST /runs`) groups related runs — the
  turns of one conversation — under `<trace_dir>/<session_id>/`, with every
  trace event carrying the id. `Hive.ingest_dir` is recursive, so grouped and
  flat traces ingest alike.
- **Truncation guard**: tool calls from a `max_tokens`-truncated model
  response are no longer executed on partial arguments — each call gets an
  error result asking the model for a compact retry, traced as
  `tool_truncated`.
- **Playbooks**: named modes a harness switches between during a run. A
  `playbooks:` entry carries a prompt fragment, the tool subset that mode may
  use, mode-scoped validators, and optional `on_enter`/`on_exit` code hooks.
  Declaring any auto-adds a `switch_playbook` tool. One harness with three
  playbooks replaces three harnesses that would otherwise duplicate a system
  prompt and split their evidence three ways.
  - `on_enter` may return `{"context": str}` to inject a note or
    `{"block": True, "reason": str}` to refuse entry (e.g. stale data);
    `on_exit` may refuse the *exit*, making it a boundary gate — "you entered
    targeting and proposed nothing". A stuck exit gate is force-released after
    three consecutive refusals, and the release is traced.
  - Every switch is a `playbook_switch` trace event and a
    `playbook_enter`/`playbook_exit` lifecycle event. The Hive indexes them, so
    `stats` reports success, cost, turns, and refusals **per playbook**, and
    the failure report localizes a problem to one mode.
  - `playbooks.*.on_enter`/`on_exit` execute code and are frozen from
    evolution, enforced by a value-inspecting gate check so rewriting the whole
    `playbooks` list cannot smuggle a hook in. Prompts stay mutable — evolution
    rewrites guidance, never side-effecting code.
  - Construction: `construct.add_playbook` scaffolds the prompt, hooks, and
    validators through the same validate-and-rollback path as everything else;
    because playbooks live in `spec/schema.py`, the JSON schema, annotated
    template, `explain`, and the generator's meta-prompt all pick them up.
- **Multi-turn conversation input**: `run_harness(conversation=[...])` and
  `dry_run(conversation=[...])` take a whole alternating thread. The trailing
  user message becomes the task statement; earlier turns are seeded as history
  and are the first thing compaction reclaims. For callers that own the
  conversation themselves, such as a chat service replaying it each turn.
- **Structured artifacts**: a tool may return a `ToolResult` carrying
  `Artifact(kind=..., data=...)` side-products. They surface on
  `RunResult.artifacts` (and `artifacts_of(kind)`), on the run payload, and on
  the trace — so a harness can drive a real UI with charts or proposals
  without pushing JSON through the model's text channel. Visible to guardrails
  via `RunState.artifacts`.
- **Per-run context injection**: `run_harness(context={...})` passes
  caller-owned values (a DSN, request-scoped state, a mutable accumulator) to
  code tools that declare a `run_context` parameter, and to validators. The
  parameter is hidden from the model's tool schema and cannot be forged by a
  model-supplied key of the same name. Never written to the trace.
- **`load_skill` builtin tool**: progressive disclosure without shipping a
  filesystem reader. Only skills the spec declares are reachable, and the
  schema enumerates them. The skills index points at whichever loader the
  harness actually carries.
- **Deferred outcome labels**: `Hive.record_outcome(run_id, "success"|"failure")`
  and `hiveloom outcome <run-id> <result>` attach a real-world judgement to a
  completed run — a human confirmed or dismissed the proposal, the extracted
  record turned out wrong. Validators grade a run while it happens; this
  records what the world said afterwards. Surfaced by `stats` and by the
  failure report, so it can drive evolution. The run row is never rewritten.
- **Artifacts over MCP.** An MCP server can hand hiveloom caller-facing
  side-products by returning structured content under a `_hiveloom` envelope
  (`{"_hiveloom": {"artifacts": [{"kind": ..., "data": ...}]}}`); the bridge
  lifts them onto `RunResult.artifacts` and keeps the envelope out of the
  model's text. Without this, moving a tool behind MCP silently downgraded it
  to text-only — a domain tool that also drives a UI (a chart spec, a decision
  proposal) could not be hosted remotely.
- Playbook tool subsets may name MCP tools (`mcp__<server>__<tool>`). They are
  exempt from the declared-tool check, since MCP tools are discovered from a
  live server when the registry is built and cannot be known at parse time.
- `ToolResult.retryable`: set `False` on a deterministic error so
  `loop.on_tool_error: retry_once` does not repeat it. A refused playbook
  switch uses it — retrying re-runs the gate's side effects and double-counts
  the refusal in run evidence.
- Two evaluation suites that measure the harness on frontier models, where
  article-extractor only measured the rescue of a weak one. `evals/article-digest`
  (output-heavy digest, Opus 5 / Sonnet 5) shows verification and retry closing
  the contract-compliance tail: 80% → 100% success at flat cost per success.
  `evals/page-audit` (exhaustiveness past a truncated tool view, aggregation,
  date arithmetic) shows the property that matters downstream: silently wrong
  runs drop from 5/6 and 3/6 raw to 0/6 harnessed.
- `evals/README.md`: an index of the three suites, the shared method, and the
  caveats that apply to all of them.
- `docs/models.md`: how the `claude` provider handles adaptive-thinking models.
- Charts for the README's evidence section, regenerated from the committed
  results by `docs/assets/make_plots.py`.

### Changed

- `model.temperature` is now optional and unset by default: when unset it is
  omitted from provider API calls entirely. Current Anthropic models reject
  the parameter as deprecated, so specs should simply not set it.
- The README's article-extractor table showed the pre-0.3.0 sweep, not the
  0.3.1 re-run it linked to. Corrected against the committed `RESULTS.md`
  (Haiku 2%→3% raw and 61%→65% harnessed, cost per success $0.3405→$0.2212
  raw, so 12× rather than 17×), with the Gemma arm — where the harness is
  slightly worse — added rather than omitted, and the statistical caveat that
  only the Haiku delta survives a paired test.

### Fixed

- `dry_run` reported `spec.system_prompt` verbatim instead of the assembled
  system prompt, omitting the skills index and active tools' guidelines. Both
  the reported prompt and `estimated_input_tokens` under-reported what a real
  run sends.
- `_set_dotted` (evolution's change applier) could not address list entries, so
  a dotted path through any list-valued section silently created a mapping and
  the whole batch was then rejected as invalid. Numeric segments now index
  lists, which is what lets evolution target one playbook's prompt.
- `construct._scaffold_hook` raised a bare `ValueError` on a malformed
  `path.py:function` ref instead of an actionable `SpecError`.
- `hiveloom.serve`'s `result_payload` was a second copy of the canonical run
  payload, so fields added to it (notably `artifacts`) were silently missing
  over HTTP. It now delegates.
- `claude` provider: omit `temperature` for the models whose API rejects
  sampling parameters (Opus 4.7 and later, Sonnet 5, Fable/Mythos), even when a
  spec sets one explicitly. Any value at all makes those models 400.
- `claude` provider: preserve `thinking` / `redacted_thinking` blocks on the
  assistant turn. Adaptive-thinking models require the turn to be replayed
  unchanged, so dropping them broke the next call of a tool-use loop.

## [0.4.0] - 2026-08-03

### Added

- Builtin model providers for the major labs, so no configuration is needed
  beyond an API key: `openai`, `gemini`, `mistral`, `deepseek`, `xai`, `groq`,
  `openrouter`, `together`, `fireworks`, `ollama`, and `vllm` join `claude`.
  All are backed by the existing stdlib-only OpenAI-compatible provider.
- `hiveloom models [PROVIDER] [--json]` lists providers with their endpoint,
  API-key variable, whether that key is set (never its value), catalog policy,
  and per-model pricing.
- `hiveloom set model <provider>/<model-id>` (and `construct.set_model`) moves
  provider and id in a single validated commit. Previously a harness could not
  change lab at all: the two fields validate against each other, so either
  single-field ordering was rolled back.
- Open-catalog providers: every builtin except `claude` accepts model ids that
  are not pre-registered, so a model released after a hiveloom version is
  usable immediately. `claude` stays closed, so typos still fail validation.
- `~/.hiveloom/models.yaml` can now extend a builtin provider (omit `base_url`
  to add models or correct pricing) or override one (supply `base_url` to point
  a lab name at a gateway or proxy).
- `docs/models.md`: the full model/provider reference.
- Prompt caching. The `claude` provider marks the system prompt, tool list,
  and conversation tail as cache breakpoints on every call, so the stable
  prefix of an agent loop is written once and read at 0.1x the input price on
  every later turn. Cache read/write tokens are broken out on `Usage`, priced
  in cost estimation (0.1x / 1.25x input), and visible in traces; OpenAI-style
  `cached_tokens` are likewise split out of the prompt count.
- `hiveloom mcp serve DIR [DIR ...]`: expose harnesses as MCP tools over
  stdio — the agent-facing front door. Each harness becomes a `run_<name>`
  tool whose description is the harness description and whose result is
  structured (`status`, `output`, `reason`, `cost_usd`, `turns`, `run_id`,
  `verdicts`), so a calling agent can delegate a task and check the verified
  outcome instead of improvising. Caller input is always literal text
  (mirroring the HTTP servers), trust is enforced per directory at startup,
  and failed runs land in the Hive like any other — so delegated work drives
  evolution too. SDK: `hiveloom.serve.mcp.build_mcp_server`.
- Local harness registry and MCP discovery. `hiveloom registry add|remove|list`
  keeps a machine-level list of harnesses (`~/.hiveloom/registry.yaml`);
  `hiveloom mcp serve --registered` serves all of them at once (broken entries
  are skipped with a stderr warning), and every MCP server now exposes a
  `list_harnesses` tool returning the catalog *with measured fitness* — total
  runs, success rate, average cost/turns from the Hive — so a calling agent
  can pick a harness on evidence, not vibes. Registration is not trust: the
  same per-directory trust gate still applies.
- `hiveloom mcp serve --http [--host] [--port]`: the MCP server over the
  streamable-HTTP transport at `/mcp`, gated by `HIVELOOM_API_KEY` (Bearer or
  `X-API-Key`, constant-time compare, mirroring `hiveloom serve`). A
  non-loopback bind without the key is refused at startup. No TLS — a gateway
  should front this, as with the other HTTP surfaces.
- Parallel tool execution (opt-in): `loop.tool_execution: parallel` preflights
  guardrails and hooks for a turn's tool calls in source order, executes the
  surviving calls concurrently, and finalizes results in source order.
  `TraceWriter` is now thread-safe, and the builtin `file_write` serializes
  writes per resolved path. The default stays `sequential`, where a halt
  during one call still prevents later calls from executing at all.
- Provider middleware events: `before_provider_request` lets a hook patch the
  outgoing request (`system`/`messages`/`tools`; this request only, and always
  after guardrails), and `after_provider_response` observes the wire-level
  accounting (`usage`, `cost_usd`, `stop_reason`) per call.

- Context-overflow recovery. When a provider rejects a request because the
  prompt exceeds the model's context window (the offline token estimate
  under-counted), the loop now force-compacts the history — targeting half the
  current estimate — and retries the turn once, instead of ending the run as an
  `error`. Recovery is traced as `context_overflow_recovery`; a second
  overflow still surfaces as an error run. Overflow rejections are classified
  by both the Anthropic and OpenAI-compatible providers
  (`ContextOverflowError`), and are never blindly retried against the same
  too-long prompt.

### Changed

- `summarize` compaction now requests a structured summary (Goal / Progress /
  Key decisions / Next steps / Critical context) instead of free-form "be
  terse" notes, so post-compaction turns keep direction, not just facts.
- Cost estimation resolves an unregistered model id through its provider's
  default price before the conservative fallback, so an unlisted model on a
  local Ollama or vLLM server is priced at zero instead of Haiku rates. Unknown
  *hosted* models keep the conservative fallback, so budget guardrails still
  never under-count.

### Fixed

- `evolve --apply` now restores the harness folder when validation of an
  approved proposal fails, instead of leaving a mutated spec, a bumped
  `# evolved:` counter, and half-applied code files behind.
- Failure analysis groups verdicts by a normalised template and counts distinct
  runs, so a behaviour that recurs across different inputs shows up as one
  cluster rather than one cluster per input. Analysis is now scoped to the
  current harness version.
- `openai_compat` reports the attempt a model call actually failed on, rather
  than claiming the full retry budget was spent on errors it never retried.
- `evals/article-extractor/scripts/setup_harnesses.sh` creates `harnesses/`, so
  the benchmark can be set up from a fresh clone.

## [0.3.1] - 2026-07-30

### Changed

- Simplified the Python generation SDK: `generate_harness(task, output)` now
  resolves the default strong model like the CLI, while dependency injection
  through `model=` remains supported.
- Added the missing `--json` startup contract to `control-plane`, aligning the
  long-running HTTP commands with the rest of the agent-facing CLI.
- Replaced the single internal structured-logging dependency with the standard
  library logger and added an 85% branch-coverage quality gate.
- Tightened source-distribution exclusions so local traces, caches, generated
  Docker artifacts, lock files, and embedded runtime wheels cannot ship.
- Simplified the README around hiveloom's durable-harness moat and added the
  reproducible article-extractor benchmark results.

## [0.3.0] - 2026-07-30

### Added

- A Hive-backed evolution proposal queue with create/list/show/apply/reject
  flows in the CLI and HTTP control plane.
- The `sequential_steps` loop policy for executing declared `loop.steps` in a
  fixed order.
- Opt-in post-run `evolution.auto_propose` drafting with failure thresholds,
  cooldowns, and proposal deduplication; proposals are never auto-applied.
- MCP server declarations, construction commands, dynamic tool discovery,
  runtime dispatch, and `hiveloom mcp list-tools`.
- A bearer-authenticated, non-production HTTP control plane with ed25519 key
  generation, authorization, signing, bounded run concurrency, and
  run/evolve/proposal endpoints.
- Typed-package metadata (`py.typed`) and SPDX license metadata for PyPI.

### Changed

- Proposal records expose typed proposal/gate/apply-result accessors; queue
  deduplication happens before the strong-model call, and proposal application
  claims are concurrency-safe and retryable.
- The HTTP mutation boundary is derived from `ALWAYS_FROZEN`, and remove
  checks derive list-section roots from the construct API.
- Starlette, Uvicorn, PyJWT with crypto support, and cryptography are direct
  runtime dependencies for the control plane.
- `hiveloom keys authorize` now requires an explicit `--scope` (no `*` default),
  so a key is never granted broad scope by omission.
- Bearer tokens have a 7-day server-side lifetime ceiling enforced at verify
  time, in addition to the 15-minute default TTL.
- The release workflow no longer publishes to PyPI automatically on a tag push;
  a `v*` tag builds and cuts a GitHub Release, while publishing is a deliberate
  manual `workflow_dispatch` with `publish_pypi=true`.

### Fixed

- OpenAI-compatible responses now preserve reasoning-only text, never
  re-serialize empty assistant turns as `content: null`, tolerate dict-shaped
  tool-call arguments, fall back across usage-token field names, and map
  `content_filter` termination. Tool-call arguments that are valid-but-non-object
  JSON (`"null"`, `"[1]"`) and explicit null usage counts no longer crash
  normalization.
- The HTTP control plane freezes the code-execution roots `tools` and
  `verify.validators` and the `name` root over `mutate` scope (closing a remote
  code-execution and a cross-harness rebinding path), and refuses writes to a
  parent of any frozen leaf.
- MCP server names may not contain `__`, keeping the `mcp__<name>__<tool>` tool
  prefix injective so tools from different servers cannot silently collide.
- Auto-propose records a terminal attempt when a draft gates to nothing, so the
  cooldown advances and a failing run no longer re-pays a strong-model call.
- `authorized_keys.json` rows that are non-dict or missing `key_id` fail closed
  as a typed 401 instead of an unhandled 500.
- `hatchling` is pinned to `>=1.27` for the SPDX license metadata it emits.
- Loop-policy dependency injection again distinguishes an omitted policy from
  an explicitly supplied one.
- Auto-propose cooldowns have a real one-minute minimum instead of accepting
  functionally disabled near-zero values.
- MCP transport declares its HTTP dependency, bounds initialization time, and
  rejects tool-name collisions introduced by sanitization.
- The HTTP control plane fails closed on corrupted authorized-key rows and
  blocks sensitive input-file reads, case-variant path bypasses, custom trace
  directory leaks, and mutation access to frozen configuration.
- Proposal queue review fixes cover trust checks, stale specs, confirmation
  ordering, code-path approval, apply claims, and harness-bound HTTP access.

## [0.2.0] - 2026-07-21

### Added

- Deployable analysis and extraction example harnesses alongside the summarizer.
- Per-lifecycle agent skills (`skills/hiveloom-{build,run,evolve,extend,ship}`) for
  progressive disclosure, in addition to the all-in-one `SKILL.md`.
- Atomic spec writes: construction steps validate and roll back on failure instead
  of leaving a harness directory in a partially-written state.
- Trust gating for foreign harness folders — `hiveloom trust`, `--approve`, and
  `HIVELOOM_TRUST` now gate any foreign code before it loads.
- WAL-mode Hive storage with explicit pruning and trace-level honoring.
- CI, security audit, release, and Dependabot workflows.

### Changed

- Guardrails: `add guardrail` now replaces an existing singleton entry instead of
  appending a duplicate.
- Schema validation bounds runtime limits and validates model ids against
  registered providers.
- Evolve gates dangerous mutations and sandboxes code-change paths — regenerated
  code hooks always require explicit approval.
- The agent loop guards every model call and makes run cost/turn accounting precise.
- Model providers retry transient failures without losing usage accounting.
- Packaging excludes every `.env` variant and trace data from built artifacts.
- Shell and `http_get` builtin tools hardened (shell allowlists now match exact
  argv, not just `argv[0]`).

### Fixed

- The CLI translates unexpected errors into a clean message instead of leaking
  raw tracebacks.

## [0.1.0] - 2026-07-16

Initial beta release: the harness spec and loader, explore/construct CLI
commands, the runtime (`run`), the Hive (`trace`/`stats`), generation and
evolution (`generate`/`evolve`), and packaging (`package`).
