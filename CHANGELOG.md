# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
