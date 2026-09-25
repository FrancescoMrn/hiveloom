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

A harness is the task-specific half of **agent = model + harness**: a
self-contained folder (`harness.yaml` + code hooks) that declares the tools,
loop policy, context, budgets, guardrails, and verification around a small
executor model (default `claude-haiku-4-5`). You are the builder agent; create a
boundary that lets that smaller model perform one repeatable job reliably.

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
hiveloom migrate ./h --json        # atomic legacy version -> schema_version rewrite
hiveloom catalog tools             # also: guardrails|validators|policies|compaction|hooks
hiveloom explain context.compaction  # field-level docs for any spec path
hiveloom extensions                # loaded packs/providers — the catalog may be extended
```

Builtin and extension-registered entries must appear in `catalog`; MCP tools
are discovered dynamically from declared servers and instead appear in
`hiveloom mcp list-tools`.

## Step 2 — construct incrementally

```bash
hiveloom init ./h --name my-harness --task "One-line task."
hiveloom set system_prompt --file prompt.txt --dir ./h
hiveloom set loop.max_turns 15 --dir ./h
hiveloom add tool --builtin file_read --dir ./h
hiveloom add tool --builtin shell --param 'commands=["wc -l app.log"]' --dir ./h
hiveloom add validator --builtin output_schema --schema-file ./schemas/output.json --dir ./h
hiveloom add guardrail --builtin max_cost_usd --value 0.50 --dir ./h
hiveloom remove file_read --dir ./h      # remove by identifier, or delete a field path
```

`set`/`remove` paths are dotted and may be indexed into an existing list item
for read-modify-write, e.g. `hiveloom set guardrails.0.value 0.05 --dir ./h`
(the default cost guardrail `init` seeds) or `hiveloom remove guardrails.1
--dir ./h`; an out-of-range index is a clear error naming the list's length —
there is no append-via-`set`, use `add`. A `set` value normally parses as
YAML (so `"30"` becomes `30`), except a plain string field such as
`system_prompt` keeps the CLI text verbatim so `": "` and the like can't be
misread as a YAML mapping; use `--file` for anything long, multi-line, or
that this heuristic can't resolve.

Add an MCP server's tools the same way — no live connection is made, so a
typo in the command/URL only surfaces at `run`/`--dry-run`/`mcp list-tools`:

```bash
hiveloom add mcp-server --name jira --url https://mcp.acme.example/mcp \
  --header-env Authorization=ACME_MCP_TOKEN --timeout-seconds 120 --dir ./h
```

`--timeout-seconds` (default 30, must be `> 0` and `<= 600`) covers
connect/initialize and each tool call — raise it for a slower peer
harness/tool round trip.

To dictate a fixed, ordered list of objectives instead of free-form react,
set `loop.steps` **before** switching `loop.policy` to `sequential_steps`
(each `set` fully re-validates, and an empty-steps `sequential_steps` is
rejected):

```bash
hiveloom set loop.steps '["extract fields", "validate schema", "write report"]' --dir ./h
hiveloom set loop.policy sequential_steps --dir ./h
```

Use object steps when the workflow needs enforcement, not only guidance:

```bash
hiveloom set loop.steps '[{"id":"read","instruction":"Read input.","tools":["file_read"],"require_tool_calls":["file_read"],"max_model_calls":2,"max_tool_calls":1},{"id":"answer","instruction":"Return the answer.","tools":[],"max_model_calls":1}]' --dir ./h
hiveloom set loop.policy sequential_steps --dir ./h
hiveloom run ./h --input-file sample.txt --dry-run --json
```

`tools: []` is deliberately tool-free; omitted `tools` preserves the active
set. A required call must succeed. Hidden calls are blocked before dispatch,
and a per-step limit ends the run with exit 4. Read the dry-run `steps` array
before spending model budget. Legacy strings keep their existing behavior.

Builtin quick reference (list live versions with `hiveloom catalog <kind>`):

- **Tools:** `file_read`, `file_write` (sandboxed to the working dir), `shell`
  (allowlist-only; variable file-reading arguments need an OS sandbox),
  `http_get` (declare repeatable `hosts`; new hosts otherwise need an
  interactive operator decision and fail closed in agent/JSON runs).
  Add them with repeated `--host` flags; never hand-edit the tool entry.
  Opt-in memory tools: `recall_runs` (this harness's own prior runs), `notes`
  (run-scoped storage that survives compaction), `propose_memory` (queue a
  durable lesson for review; it never writes the spec — curate entries with
  `hiveloom memory list|show|add|forget`).
- **Validators** (the reward signal — always add at least one):
  `output_schema --schema-file`, `regex_match --pattern`, `file_exists --path`,
  `command_succeeds --command`, `grounded_references --output-path
  --evidence-path TOOL=JSON_PATH` (repeat the evidence flag as needed).
- **Guardrails:** `max_cost_usd`, `max_wall_clock_seconds`,
  `max_turns_hard_cap`, `tool_allowlist`, `no_network_write`,
  `regex_output_filter --pattern`. The cost guardrail defaults **on**
  (`max_cost_usd: 1.00`) even if omitted.

### Optional: let the harness ask a peer for help

A harness's model is frozen, but *which harness runs the task* need not be.
`delegation` (off by default) lets a run hand the task to a peer harness in
this machine's registry that measures better on it, verify the answer with its
own validators, and charge the peer's cost to its own budget — or, when nothing
qualifies, return `referrals` naming the harness that fits.

```bash
hiveloom set delegation.enabled true --dir ./h --json
hiveloom set delegation.when '["on_start","model_choice"]' --dir ./h --json
hiveloom set delegation.min_peer_success_rate 0.7 --dir ./h --json
hiveloom set delegation.min_peer_runs 5 --dir ./h --json
```

`on_start` and `on_verify_fail` are enforced by the loop; `model_choice` adds a
deferred `delegate__<peer>` tool per eligible peer plus an active `list_peers`.
Depth, cycles, and the child's share of the remaining budget are runtime
decisions, not the model's. Full reference: `hiveloom guide delegation`.

### Task-specific logic → code hooks

```bash
hiveloom add tool --code tools/fetch.py:fetch --description "..." --dir ./h
hiveloom add validator --code validators/check.py:validate --dir ./h
hiveloom add hook --on before_tool_call --code hooks/audit.py:audit --dir ./h
hiveloom add skill pdf-report --description "Build a PDF report." --dir ./h
```

`--code` scaffolds a correctly-signed stub for you to fill in. A validator has
the signature `validate(run_output, run_context) -> {"passed": bool,
"feedback": str}` and may add a third `verification_context` parameter for
bounded, redacted current-run tool evidence. A tool is a
`@hiveloom.tools.tool`-decorated function whose
JSON schema is derived from its type hints. `add skill` scaffolds a
progressive-disclosure `skills/<name>/SKILL.md` the executor reads on demand —
pair it with the `file_read` tool.

When the output selects IDs, add `grounded_references` as well as an output
schema. Shape validation alone cannot prove that a selected ID came from an
allowed tool call. Inspect `hiveloom catalog validators --json` before building
the command.

When several upstream calls form one domain operation with an invariant
between them, prefer one deterministic composite tool. Search followed by an
eligibility check is a good example: unverified hits should not cross the tool
boundary. Keep tools separate when calls are independently useful, need
different permissions, should run in parallel, or must remain separately
visible for audit or human review. Use structured steps for ordering and tool
availability; do not hide phase filtering in a provider adapter.

## Step 3 — finish

```bash
hiveloom validate ./h                              # spec + code-hook import/signature checks
hiveloom run ./h --input sample.txt --dry-run      # no model call; MCP discovery still does I/O
```

Read the dry-run output: does the system prompt, tool list, and input framing
look like what the executor needs?

## One-shot alternative

```bash
hiveloom generate "task description" -o ./h            # needs configured-provider credentials
hiveloom generate "task" -o ./h --blueprint scraper    # apply a house-style blueprint
```

`generate` has a strong model drive the same construct commands with a
validate/repair loop — same code path, so the result is inspectable and
editable with the commands above.

## Next steps

Running and interpreting results: `hiveloom-run` skill. Improving after
failures: `hiveloom-evolve` skill. Full spec reference: `docs/spec.md`.
