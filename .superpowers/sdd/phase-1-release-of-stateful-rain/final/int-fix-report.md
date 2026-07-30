# Phase 1 integration-fix report

Branch inspected: `phase1-integration` at `368af37`. No checkout, merge, commit,
push, dependency install, network call, or API credential was used.

## 1. Unappliable-proposal seam

### Shared gate behavior

- `src/hiveloom/evolve/evolver.py:154` now validates the complete provisionally
  accepted YAML mutation batch against the existing `HarnessSpec` schema.
- The gate serializes the current validated spec to a fresh plain dict, applies
  all accepted dotted-path writes to that copy, and calls the existing
  `spec_from_dict` validator.
- If the resulting spec is invalid, every provisionally accepted change moves
  to `rejected` with a reason beginning
  `accepted mutation batch would produce an invalid spec`.
- Rejections produced earlier by the frozen, dangerous-tool, and mutable-set
  checks are left untouched. The direct regression includes a frozen
  `guardrails` mutation and confirms its reason remains exactly `frozen path`.
- Validation is batch-level rather than per-change. A proposal that opts
  `loop.steps` into its mutable set and changes both `loop.policy` and
  `loop.steps` to a jointly valid configuration remains accepted.

### Queue and auto-propose behavior

- `src/hiveloom/evolve/proposals.py:108` refuses to persist a proposal when
  gating leaves neither accepted YAML nor a code change. It raises the existing
  `ProposalQueueError` with `proposal has no applicable changes after gating`.
- This keeps schema-invalid auto-drafts out of the Hive, so they create neither
  a pending dedup row nor a `created_at` value that starts the auto-propose
  cooldown. The strong-model call that produced the bad proposal cannot be
  recovered, but a later qualifying run can try again.
- A code-only proposal is still queueable because code changes remain subject
  to their separate human-approval gate.

### Tests and pre-fix evidence

Added:

- `tests/test_evolve.py:152` — direct `gate()` rejects the invalid
  `loop.policy=sequential_steps` batch and preserves an existing frozen-path
  reason.
- `tests/test_evolve.py:170` — a valid two-change
  `loop.policy` + `loop.steps` batch passes, and a spy proves schema validation
  actually ran.
- `tests/test_proposals.py:70` — `create_proposal` raises and persists no row
  for the schema-invalid gated batch.
- `tests/test_auto_propose.py:157` — two auto-propose attempts both reach the
  model and persist no proposal, proving the first invalid draft consumed
  neither dedup nor cooldown state.

Before changing production source, these four tests were run together against
the merged pre-fix implementation. All four failed:

- direct gate: `loop.policy` remained accepted;
- valid-batch test: the schema-validation spy observed zero calls;
- queued proposal: `ProposalQueueError` was not raised;
- auto-propose: one pending proposal occupied the Hive.

After the source change, the same command produced `.... [100%]`.

## 2. Documentation reconciliation

### Frozen-list drift

Synchronized every enumerated evolution boundary to:

`guardrails`, `model`, `logging.redact`, `extensions`, `hooks`, `mcp_servers`,
`evolution.auto_propose`

Updated:

- `AGENTS.md`
- `README.md`
- `docs/architecture.md`
- `docs/spec.md`
- `docs/control-plane.md`
- `docs/deploying-and-evolving.md`
- root `SKILL.md`
- `skills/hiveloom-evolve/SKILL.md`
- `skills/hiveloom-extend/SKILL.md`

The evolver module docstring now refers to `schema.ALWAYS_FROZEN` rather than
maintaining another partial list.

### Catalog truth and MCP

- `AGENTS.md` now defines the catalog as authoritative for builtin and
  extension-registered entries, names MCP tools as the dynamic exception, and
  points to `hiveloom mcp list-tools`.
- Reconciled the same formerly absolute claim in `README.md`,
  `docs/extending.md`, `skills/hiveloom-build/SKILL.md`, and
  `skills/hiveloom-extend/SKILL.md`.

### Provider credentials

- `docs/extending.md` did not contain a literal “Anthropic only” sentence in the
  merged file, but it lacked a general credential statement. Added an explicit
  provider-based rule: use credentials required by the configured provider;
  `ANTHROPIC_API_KEY` is the default Anthropic example,
  `api_key_env` covers hosted OpenAI-compatible providers, and local
  vLLM/Ollama/mlx deployments may require none.
- Reconciled equivalent Anthropic-only wording in `AGENTS.md`, `README.md`,
  root `SKILL.md`, the build/run/evolve skills, the CLI generate docstring, and
  the control-plane credential example.

### Dry-run

- `docs/spec.md` already warned that MCP discovery performs real I/O during
  dry-run; clarified the complete contract: no model-API call, but eager MCP
  discovery can perform local/network I/O.
- Reconciled absolute “no API use” wording in `README.md`, root `SKILL.md`, the
  build/run skills, `runner.py`, and CLI help/docstrings.

### Security and changelog

- `SECURITY.md` now names the latest `0.2.x` release line.
- Populated `CHANGELOG.md` `[Unreleased]` under Added/Changed/Fixed from
  `git log --oneline ffbe54c..HEAD`: proposals, sequential steps, auto-propose,
  MCP, the HTTP control plane/auth, typed-package metadata, dependency and
  boundary changes, and the actual OpenAI/loop/cooldown/MCP/serve/proposal
  fixes. No version or release date was added.

## 3. Small cleanups

- `scripts/smoke_openai_compat.py` now has mode `100755`; `git diff --summary`
  reports `mode change 100644 => 100755`.
  - Staging was attempted with `git add --chmod=+x`, but the outer `nono`
    sandbox denied creation of
    `/Users/rinaldofesta/Projects/Personal/hiveloom/.git/worktrees/phase1-int/index.lock`.
    The mode change is therefore present but unstaged. This is the only
    requested operation that could not be completed in the current sandbox.
- `tests/test_openai_compat_matrix.py:55` adds a list-valued
  `tool_calls[].function.arguments` fixture to the existing offline matrix. It
  exercises the existing `TypeError` fallback and completed normally in the
  matrix and full-suite runs.
  - This is coverage for an already-correct exception branch, not a regression
    tied to a source change. No new test function or production fix was added,
    so there was no source change to revert for a pre-fix-failure run; forcing
    it to fail would contradict the requested observation that the code
    already catches this shape.
- `src/hiveloom/runner.py:176` explains that auto-propose stays under
  `if ingest:` because it must count the just-ingested failure.
- `src/hiveloom/cli.py:904` restores `CodeChange` as the shared approve
  closure’s parameter type.

## 4. Final verification

All tests remained offline.

`uv run pytest`

```text
472 passed, 1 warning in 9.33s
```

The warning is the existing Starlette/httpx deprecation warning from
`tests/test_serve_app.py:17`.

`uv run ruff check src tests`

```text
All checks passed!
```

The default `uv lock --check` cache initialization was blocked by the outer
sandbox at `/Users/rinaldofesta/.cache/uv`. Re-running the same gate with a
fresh `UV_CACHE_DIR` under `/tmp` succeeded:

```text
Resolved 51 packages in 2ms
```

Exit code was `0`; `uv.lock` was not modified.
