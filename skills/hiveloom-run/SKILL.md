---
name: hiveloom-run
description: >-
  Run a hiveloom harness on an input, interpret exit codes and results, and
  inspect run memory (traces, per-version stats). Use when executing an
  existing harness, debugging why a run failed, embedding a harness run in
  another program, or checking a harness's success rate/cost. Triggers:
  "run the harness", "why did the run fail", "harness stats", "hiveloom run".
---

# Running a hiveloom harness

```bash
hiveloom run ./h --input notes.txt --json     # --input takes a FILE path or literal TEXT
```

Needs credentials for the configured provider when required (for example,
`ANTHROPIC_API_KEY` for the default provider, loaded from the harness `.env`
if present). The small executor model (default `claude-haiku-4-5`) runs
inside; guardrails and validators gate it.

## Interpret the exit code — always

| Code | Meaning | What to do |
|---|---|---|
| 0 | success (verify passed) | use the output |
| 1 | verify failed | read the validator feedback in the trace; recurring → evolve |
| 2 | guardrail halt | check which guardrail (`guardrail_triggered` event); raise the limit deliberately or fix the loop |
| 3 | spec/validation error | `hiveloom validate ./h` and fix the construction |
| 4 | runtime error | read the trace tail; usually a tool/hook exception or missing env var |

## Before spending API budget

```bash
hiveloom validate ./h                          # structure + code-hook checks
hiveloom run ./h --input x.txt --dry-run       # no model call; MCP discovery still does I/O
```

A harness folder that arrived from elsewhere (unzipped artifact, clone) is
trust-gated before its code loads: `hiveloom trust ./h`, or `run --approve`,
or `HIVELOOM_TRUST=always` in CI.

## Inspecting memory (the Hive)

Every run appends a JSONL trace to `./h/.hiveloom/traces/<run_id>.jsonl` and
auto-ingests it into the Hive (`~/.hiveloom/hive.db`, override `$HIVELOOM_DB`),
idempotently by `run_id`.

```bash
hiveloom trace run_abc123 --json           # one run: summary + ordered events
hiveloom trace run_abc123 --dir ./h        # ingest the folder's traces first if unknown
hiveloom stats ./h --json                  # success rate / cost / turns PER VERSION HASH
hiveloom stats my-harness-name             # by name, from the Hive
```

To debug a failure, read the trace's `verification_result`,
`guardrail_triggered`, and `tool_result` (`is_error`) events — they name the
proximate cause. `stats` bucketing by version hash is what proves a later
mutation helped.

## Embedding in another program

```bash
hiveloom run ./h --input x --stream        # every trace event as a JSON line; result last
```

Or the Python SDK:

```python
from hiveloom import run_harness
result = run_harness("./h", "notes.txt", on_event=lambda e: print(e.type))
```

## Next steps

Repeated failures → `hiveloom-evolve` skill (never hand-edit the harness).
Deploying elsewhere → `hiveloom-ship` skill.
