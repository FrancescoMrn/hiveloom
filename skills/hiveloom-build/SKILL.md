---
name: hiveloom-build
description: >-
  Build a new hiveloom harness for a repetitive, verifiable task — explore the
  contract, construct incrementally with validated CLI steps, then dry-run.
  Use when the user wants a durable, versioned harness for a task (extraction,
  reconciliation, triage, summarization) instead of doing it inline. Triggers:
  "make a harness", "build a harness for X", "hiveloom", "cheap model for X".
---

# Building a hiveloom harness

A harness is a self-contained folder (`harness.yaml` + code hooks) that
scaffolds tools, loop policy, context strategy, guardrails, and verification
around a small executor model (default `claude-haiku-4-5`).

**Never hand-edit `harness.yaml`.** Drive the CLI — every mutating command
validates the full spec and rolls back on error, so the folder is never left
invalid. Pass `--json` and check each result.

## Is a harness the right shape?

Build one when the task is **repetitive + verifiable** — it has a checkable
notion of "done": a JSON shape, a regex, a file, or a passing command. For
one-off, unverifiable, or creative work, do the task inline instead.

## Step 1 — learn the contract (read-only, no API key)

```bash
hiveloom schema --annotated        # a valid, commented YAML template
hiveloom schema --json             # the JSON schema
hiveloom catalog tools             # also: guardrails|validators|policies|compaction|hooks
hiveloom explain context.compaction  # field-level docs for any spec path
hiveloom extensions                # loaded packs/providers — the catalog may be extended
```

Everything a spec can reference is a catalog entry; if `catalog` doesn't show
it, it doesn't exist.

## Step 2 — construct incrementally

```bash
hiveloom init ./h --name my-harness --task "One-line task."
hiveloom set system_prompt --file prompt.txt --dir ./h
hiveloom set loop.max_turns 15 --dir ./h
hiveloom add tool --builtin file_read --dir ./h
hiveloom add validator --builtin output_schema --schema-file ./schemas/output.json --dir ./h
hiveloom add guardrail --builtin max_cost_usd --value 0.50 --dir ./h
hiveloom remove file_read --dir ./h      # remove by identifier, or delete a field path
```

To dictate a fixed, ordered list of objectives instead of free-form react,
set `loop.steps` **before** switching `loop.policy` to `sequential_steps`
(each `set` fully re-validates, and an empty-steps `sequential_steps` is
rejected):

```bash
hiveloom set loop.steps '["extract fields", "validate schema", "write report"]' --dir ./h
hiveloom set loop.policy sequential_steps --dir ./h
```

Builtin quick reference (list live versions with `hiveloom catalog <kind>`):

- **Tools:** `file_read`, `file_write` (sandboxed to the working dir), `shell`
  (allowlist-only, disabled without one), `http_get`.
- **Validators** (the reward signal — always add at least one):
  `output_schema --schema-file`, `regex_match --pattern`, `file_exists --path`,
  `command_succeeds --command`.
- **Guardrails:** `max_cost_usd`, `max_wall_clock_seconds`,
  `max_turns_hard_cap`, `tool_allowlist`, `no_network_write`,
  `regex_output_filter --pattern`. The cost guardrail defaults **on**
  (`max_cost_usd: 1.00`) even if omitted.

### Task-specific logic → code hooks

```bash
hiveloom add tool --code tools/fetch.py:fetch --description "..." --dir ./h
hiveloom add validator --code validators/check.py:validate --dir ./h
hiveloom add hook --on before_tool_call --code hooks/audit.py:audit --dir ./h
hiveloom add skill pdf-report --description "Build a PDF report." --dir ./h
```

`--code` scaffolds a correctly-signed stub for you to fill in. A validator has
the signature `validate(run_output, run_context) -> {"passed": bool,
"feedback": str}`; a tool is a `@hiveloom.tools.tool`-decorated function whose
JSON schema is derived from its type hints. `add skill` scaffolds a
progressive-disclosure `skills/<name>/SKILL.md` the executor reads on demand —
pair it with the `file_read` tool.

## Step 3 — finish

```bash
hiveloom validate ./h                              # spec + code-hook import/signature checks
hiveloom run ./h --input sample.txt --dry-run      # assembles the first model call; no API use
```

Read the dry-run output: does the system prompt, tool list, and input framing
look like what the executor needs?

## One-shot alternative

```bash
hiveloom generate "task description" -o ./h            # needs ANTHROPIC_API_KEY
hiveloom generate "task" -o ./h --blueprint scraper    # apply a house-style blueprint
```

`generate` has a strong model drive the same construct commands with a
validate/repair loop — same code path, so the result is inspectable and
editable with the commands above.

## Next steps

Running and interpreting results: `hiveloom-run` skill. Improving after
failures: `hiveloom-evolve` skill. Full spec reference: `docs/spec.md`.
