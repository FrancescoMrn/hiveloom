---
name: hiveloom-evolve
description: >-
  Improve a failing hiveloom harness through the gated evolve flow instead of
  hand-editing it. Use when a harness keeps failing verification, hits
  guardrails repeatedly, or the user asks to improve/fix/tune an existing
  harness. Triggers: "this harness keeps failing", "improve the harness",
  "hiveloom evolve", "tune the prompt/tools of the harness".
---

# Evolving a hiveloom harness

When asked to improve a failing harness, **do not hand-edit it** — run the
evolve flow so the change is minimal, gated, versioned, and provable:

```bash
hiveloom evolve ./h              # needs configured-provider credentials when required
hiveloom evolve ./h --yes        # auto-apply YAML changes; code ALWAYS needs y/n
hiveloom evolve ./h --model provider/model-id   # choose the proposing model
```

The evolver reads the Hive's clustered failures (ingesting the folder's
in-folder traces on the fly), asks a strong model for a minimal mutation, and
gates it **in code**.

If a bounded Hive summary is not enough to explain a retry, opt in to
`evolution.trace_excerpts.enabled` through `hiveloom set`. The selector uses
indexed friction as an anchor, re-applies `logging.redact`, and enforces hard
event, byte, and token-estimate budgets before evidence reaches the proposing
model. Missing or retention-pruned journals fall back to indexed summaries.
Queued proposals keep the selection receipt and digest, not the event
payloads. Inspect that receipt with `proposals show --json`.

## Hard rules (enforced by the tool — don't fight them)

- `guardrails`, `model`, `logging.redact`, `extensions`, `hooks`,
  `mcp_servers`, `evolution.auto_propose`, and `evolution.trace_excerpts` can
  **never** be changed by
  evolution. Don't try to weaken them by other means either; if a guardrail is
  genuinely wrong, change it deliberately via `hiveloom add guardrail` /
  `hiveloom set` and say so.
- Changes must fall within the harness's `evolution.mutable` set.
- Regenerated code hooks always require explicit human approval — never
  auto-approve on the user's behalf.

## Prerequisites

Evolve needs a grounded signal: final failures, failed deferred outcomes, or
indexed friction such as repeated retries. If `hiveloom stats ./h` and
`hiveloom stats ./h --include-friction` show none, run the harness on
representative inputs first, or copy back traces from where it actually ran
(see `hiveloom-ship`). Both `stats` and `evolve` ingest
`./h/.hiveloom/traces/` idempotently by `run_id`.

For automatic drafts from recovered incidents, configure an ordered
`evolution.auto_propose.triggers` list with `kind: repeated_friction`, a Hive
category, `minimum_runs`, and a bounded `window`. The current run must carry
the matched fingerprint. The proposal remains a draft and its JSON evidence
receipt names the exact run window.

## Evolving a fork

Analysis is scoped to the version on disk, so a fresh `hiveloom fork` — which
exists *because* its parent failed, but has no runs of its own — reports
`nothing to evolve`. Point it at the run it came from instead:

```bash
# The fork lands inside the harness it came from, at .hiveloom/forks/<name>.
hiveloom fork <run_id> --at <seq> --name probe        # re-enter the failing run
PROBE=./h/.hiveloom/forks/probe
hiveloom evolve $PROBE --from-parent --propose --json
hiveloom run $PROBE --resume                          # replay the prefix, changed harness
```

`--from-parent` reads the parent version out of the fork's `fork.yaml` and
drafts against those failures, applying the result to the fork's own spec. The
proposal is recorded with `trigger: fork`. Every gate above still applies — it
changes which failures are analysed, nothing about what may be mutated. It
errors if the directory is not a fork; once the fork has runs of its own,
evolve it normally.

## Prove the mutation helped

Every applied change bumps an `# evolved: N` counter in `harness.yaml` and
lands in the Hive under a **new version hash**. After redeploying/re-running:

```bash
hiveloom stats ./h --json    # success rate / cost / turns per version hash
```

Compare the new hash's bucket against the old. Rollback is reverting the
folder (one `git revert` if the harness is in git); the version hashes keep
before/after comparable.

## When evolve is the wrong tool

- The harness was **misconstructed** (wrong tool set, missing validator):
  fix it with the construct commands (`hiveloom-build` skill).
- The failures are **environmental** (missing env var, network, bad input
  data): fix the environment; evolving the spec won't help.
- There's **no verifiable definition of done**: add a validator first —
  validators are the reward signal evolution optimizes against.

Full loop and topology details: `docs/deploying-and-evolving.md`.
