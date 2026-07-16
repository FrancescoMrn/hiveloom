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
hiveloom evolve ./h              # needs ANTHROPIC_API_KEY (strong model proposes)
hiveloom evolve ./h --yes        # auto-apply YAML changes; code ALWAYS needs y/n
hiveloom evolve ./h --model provider/model-id   # choose the proposing model
```

The evolver reads the Hive's clustered failures (ingesting the folder's
in-folder traces on the fly), asks a strong model for a minimal mutation, and
gates it **in code**.

## Hard rules (enforced by the tool — don't fight them)

- `guardrails`, `model`, `logging.redact`, and `extensions` can **never** be
  changed by evolution. Don't try to weaken them by other means either; if a
  guardrail is genuinely wrong, change it deliberately via
  `hiveloom add guardrail` / `hiveloom set` and say so.
- Changes must fall within the harness's `evolution.mutable` set.
- Regenerated code hooks always require explicit human approval — never
  auto-approve on the user's behalf.

## Prerequisites

Evolve needs failure signal. If `hiveloom stats ./h` shows no (or too few)
failed runs, run the harness on representative inputs first, or copy back
traces from where it actually ran (see `hiveloom-ship`). Both `stats` and
`evolve` ingest `./h/.hiveloom/traces/` idempotently by `run_id`.

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
