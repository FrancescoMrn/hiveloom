---
name: hiveloom
description: >-
  Generate, run, and evolve agent harnesses so a small, cheap model reliably
  performs a repetitive, verifiable task. Use when the user has a task worth
  turning into a durable, versioned harness (invoice reconciliation, log
  triage, structured extraction, file summarization) instead of doing it inline
  — or when an existing harness keeps failing and should be improved via the
  evolve flow. Triggers: "make a harness", "hiveloom", "cheap model for X",
  "this keeps failing, improve it".
---

# hiveloom

hiveloom turns a task into a **harness**: a self-contained folder (`harness.yaml`
+ code hooks) that scaffolds tools, loop policy, context strategy, guardrails,
and verification around a small executor model (default `claude-haiku-4-5`). The
harness — not the conversation — is the durable, versionable, improvable
artifact. Run traces are memory (the *Hive*), and an evolve loop improves the
harness after failures.

This is the compact all-in-one skill. Focused per-stage skills (build / run /
evolve / extend / ship) live in [`skills/`](skills/README.md), and
[`AGENTS.md`](AGENTS.md) is the agent entry point for the whole repo.

## When to use it

- **Repetitive + verifiable task** → generate a harness instead of doing the
  task inline. Good fits have a checkable notion of "done" (a JSON shape, a
  regex, a passing command/test).
- **An existing harness keeps failing** → run `hiveloom evolve <dir>` rather
  than hand-editing it, so the change is gated and recorded.
- **One-off, unverifiable, or creative** work → do it inline; don't build a
  harness.

## How to create a harness (explore → construct)

Never hand-edit `harness.yaml`. Drive the CLI and check each `--json` result.

1. **Learn the contract** (read-only):
   ```bash
   hiveloom schema --annotated        # a valid, commented template
   hiveloom catalog tools             # tools|guardrails|validators|policies|compaction|hooks
   hiveloom explain context.compaction
   hiveloom extensions                # loaded packs/providers (catalog may be extended)
   ```
2. **Construct incrementally** — each step is validated and rolled back on error:
   ```bash
   hiveloom init ./h --name my-harness --task "One-line task."
   hiveloom set system_prompt --file prompt.txt --dir ./h
   hiveloom set loop.max_turns 15 --dir ./h
   hiveloom add tool --builtin file_read --dir ./h
   hiveloom add validator --builtin output_schema --schema-file ./schemas/output.json --dir ./h
   hiveloom add guardrail --builtin max_cost_usd --value 0.50 --dir ./h
   ```
   For task-specific logic use a code hook — `add tool --code tools/x.py:fn
   --description "..."` scaffolds a correctly-signed stub for you to fill in.
   Also available: `add hook --on <event> --code hooks/x.py:fn` (lifecycle
   middleware: block/patch tool calls, transform context) and
   `add skill <name> --description "..."` (progressive-disclosure SKILL.md the
   executor reads on demand — pair with the `file_read` tool).
3. **Finish**: `hiveloom validate ./h` then `hiveloom run ./h --input FILE --dry-run`
   (assembles the first model call without calling the model API; declared MCP
   servers are still contacted for eager tool discovery).

Or let a strong model do all of the above in one shot:
`hiveloom generate "task description" -o ./h` (it drives the same construct
commands under the hood).

## How to run one

```bash
hiveloom run ./h --input notes.txt --json
```
Interpret the **exit code**: `0` success, `1` verify failed, `2` guardrail halt,
`3` spec/validation error, `4` runtime error. Traces land in
`./h/.hiveloom/traces/<run_id>.jsonl`; inspect with `hiveloom trace <run_id>` and
aggregate with `hiveloom stats ./h` (success rate / cost / turns **per version
hash**). Running needs credentials for the configured provider when required
(for example, `ANTHROPIC_API_KEY` for the default provider, loaded from the
harness `.env`).

## Improving a harness — use evolve, not the editor

When asked to improve a failing harness, **do not** hand-edit it — and never
touch `guardrails`, `model`, `logging.redact`, `extensions`, `hooks`,
`mcp_servers`, or `evolution.auto_propose` through evolution. Run:
```bash
hiveloom evolve ./h            # analyze Hive failures → propose a gated mutation
```
The evolver enforces that always-frozen set in code, requires changes to stay
within the harness's `mutable` set, and requires explicit y/n approval for any
regenerated code hook. Applied changes bump an `# evolved: N` counter and are
recorded in the Hive so `hiveloom stats` can prove the mutation helped.

## Shipping

`hiveloom package ./h [--docker]` produces a portable `<name>-<hash>.zip`
(secrets and local traces excluded) plus, with `--docker`, a runnable Dockerfile.
`hiveloom.lock` records any extension packs the spec uses — install them with
the runtime at the destination. A harness folder that arrives from elsewhere is
trust-gated: `hiveloom trust <dir>` (or `run --approve`, or
`HIVELOOM_TRUST=always` in CI) before its code hooks may load. To embed a run
in another program, use `hiveloom run --stream` (JSONL trace events on stdout)
or the Python SDK (`from hiveloom import run_harness`). For a long-lived HTTP
deployment, `hiveloom serve ./h` exposes `POST /runs` + `GET /healthz`
(`HIVELOOM_API_KEY` gates `/runs`); `package --docker --serve` builds a
container that serves on `:8080`.
