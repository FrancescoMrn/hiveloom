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
hiveloom run ./h --input-file notes.txt --json
hiveloom run ./h --input-text "literal task" --json
```

Use the explicit flags in scripts and evals. Legacy `--input` still guesses
whether its value is a file or literal text for one deprecation cycle.

Run-only model selection and evidence paths do not rewrite the harness:

```bash
hiveloom run ./h --input-file case.txt \
  --provider openrouter --model qwen3.5-9b \
  --run-id case-17 --trace-dir ./eval-traces --json
```

The JSON result's `runtime_config` keeps explicit `requested` overrides apart
from the validated `resolved` model and provider. A runtime model override is
included in that run's harness snapshot and version hash, so Hive statistics
do not mix it with the model stored in `harness.yaml`.

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
hiveloom run ./h --input-file x.txt --dry-run  # no model call; MCP discovery still does I/O
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

The trace is a hash-chained journal, so two more things are available:

```bash
hiveloom trace run_abc123 --verify         # append-only chain intact? (exit 4 if broken)
hiveloom trace run_abc123 --materialize 42 # the exact request sent at that seq
```

When reading the events is not enough, **fork the run** and reproduce the
failure from the turn it happened on, against a changed harness:

```bash
hiveloom fork run_abc123 --list                  # the model calls you may re-enter
hiveloom fork run_abc123 --at 42 --name probe    # -> ./h/.hiveloom/forks/probe
# edit ./h/.hiveloom/forks/probe/harness.yaml, then:
hiveloom run ./h/.hiveloom/forks/probe --resume  # replays the identical prefix
hiveloom lineage run_abc123 --json
```

`--model` forks straight onto a different model, which is the clean A/B: one
exact prefix, one variable, each arm in its own fitness bucket. Requires
`logging.level: journal` (the default); `summary` records no context and
therefore cannot be forked. See [docs/journal.md](../../docs/journal.md).

## Embedding in another program

```bash
hiveloom run ./h --input-text x --stream   # every trace event as a JSON line; result last
```

Or the Python SDK:

```python
from hiveloom import run_harness
result = run_harness("./h", "notes.txt", on_event=lambda e: print(e.type))
```

Read `result.execution` instead of scraping its trace for batch receipts. It
contains the behavior hash, runtime version, requested/resolved/effective
model identity, timestamps, total usage, cost source, verification attempts,
execution fingerprint, and durable trace path. A clean first pass and a
recovered success both have `status == "success"`; distinguish them with
`result.execution.verification`.

## Next steps

Repeated failures → `hiveloom-evolve` skill (never hand-edit the harness).
Deploying elsewhere → `hiveloom-ship` skill.
