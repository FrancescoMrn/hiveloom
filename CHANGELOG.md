# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
