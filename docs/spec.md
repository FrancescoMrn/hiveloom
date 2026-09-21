# Harness spec reference

The harness spec is the task-confinement contract: a declarative YAML document
(`harness.yaml`) that fixes the task, available capabilities, autonomy limits,
acceptance checks, and evolution boundary, with code escape hatches
(`path/to/file.py:function`). It is defined by the pydantic models in
`src/hiveloom/spec/schema.py` — the authoritative, machine-checked source. The
commands below emit the contract directly from that schema, so this document
can never be the thing that drifts:

```bash
hiveloom schema --json        # the JSON schema
hiveloom schema --annotated   # a valid, commented YAML template
hiveloom explain <path>       # field docs, e.g. `hiveloom explain context.compaction`
```

## Sections

| Section | Purpose | Notable fields |
|---|---|---|
| `schema_version` | Harness document format | defaults to `0.2.0`; legacy `version` still loads and `hiveloom migrate HARNESS --json` rewrites it atomically |
| `name` / `description` | Identity (Hive + packaging) | required |
| `model` | The executor model | `provider` (builtin: `claude`), `id` (default `claude-haiku-4-5`), `max_tokens` (validated against registered `max_output_tokens` when known), `params` (bounded provider fields; see [models.md](models.md#provider-request-parameters)), `temperature` (optional; unset = omitted from API calls — current Anthropic models reject it as deprecated) |
| `system_prompt` | System prompt for the executor | required; the evolver may rewrite it |
| `tools` | Tools available to the loop | list of `{builtin: name}` or `{code: path.py:fn, description: ...}` |
| `mcp_servers` | MCP servers whose tools join the loop | `transport: stdio\|http`; discovered eagerly (incl. `run --dry-run`); **always frozen** |
| `extensions` | Harness-local extension modules | paths/modules loaded before validation; **always frozen** |
| `skills` | Progressive-disclosure instructions | names of `skills/<name>/SKILL.md` folders |
| `playbooks` | Named modes the run switches between | `name`, `description`, `prompt` (md fragment), `tools` (active subset), `validators`, `model`/`model_provider` (**always frozen**), `on_enter`/`on_exit` (**always frozen**), `entry` |
| `hooks` | Lifecycle middleware | code or catalog handlers attached by `event` |
| `context` | Context assembly & budgeting | `max_input_tokens`, `strategy` (`rolling`\|`full`\|`summary`), `compaction.{trigger_at_pct,method}`, `pinned`, `tool_results.{max_inline_bytes,preview_head_bytes,preview_tail_bytes}` |
| `memory` | Durable lessons rendered into every run's system prompt | `enabled`, `max_entries`, `max_entry_chars`, `prompt_budget_chars` (**all frozen from evolution**), `entries` (evolvable) |
| `guardrails` | Safety gates | list of builtins/code; **frozen from evolution** |
| `loop` | Loop policy & stop conditions | `policy` (`react`\|`plan_then_act`\|`sequential_steps`), `steps` (string objectives or structured phases), `max_turns`, `on_tool_error`, `require_verification` |
| `verify` | Verification (the reward signal) | `validators` (builtins/code), `on_fail.{action,max_retries}` |
| `confinement` | OS confinement for spawned processes | `mode` (`auto`\|`off`\|`require`), `network`, `writable`, `hide_home`, `env_passthrough`, `timeout_seconds`, `max_output_bytes`, `max_memory_mb`, `max_processes`; **frozen from evolution** |
| `egress` | What may leave in a model request | `mode` (`redact`\|`block`\|`off`), `detect_credentials`, `patterns`; **frozen from evolution** |
| `logging` | Journal policy | `trace_dir` (in-folder by default), `level` (`journal`/`summary`), `snapshot_files`, `redact.{keys,paths,patterns}` (**frozen**; legacy regex lists still load), optional `retention.{days,max_runs,max_bytes}` |
| `evolution` | What the evolver may change | `enabled`, `mutable`, `frozen`, `auto_propose` (draft trigger), `trace_excerpts` (bounded incident evidence), `objectives` (metric goals); all three nested policies are frozen |

## Builtins

List them with `hiveloom catalog <tools|guardrails|validators|policies|compaction|hooks>`.

A builtin's catalog parameters are declared with the tool, transactionally,
rather than by editing YAML — `--param name=value`, value read as YAML:

```bash
hiveloom add tool --builtin shell \
  --param 'commands: ["wc -l app.log", {argv: [grep], allow_extra_args: true}]' --json
hiveloom add tool --builtin recall_runs --param limit=5 --param scope=version --json
```

For an autonomous HTTP reader, pre-approve destinations the same way (or with
the `--host` shorthand): `hiveloom add tool --builtin http_get --host example.com
--host '*.example.org' --json`. Omit `--host` to require an interactive decision
for each new hostname during a plain CLI run.

- **Tools:** `file_read`, `file_write` (sandboxed to the working dir), `shell`
  (allowlist-only, disabled without one), `http_get` (declared hosts or a
  run-time operator decision), `load_skill` (reads a declared skill in full —
  progressive disclosure without a filesystem reader),
  `recall_runs` (this harness's own prior runs, from the Hive),
  `notes` (run-scoped scratch storage that survives compaction),
  `propose_memory` (offer a durable lesson to the review queue — never to the
  spec).
- **Guardrails:** `max_cost_usd`, `max_wall_clock_seconds`, `max_turns_hard_cap`,
  `tool_allowlist`, `no_network_write`, `regex_output_filter`. All but
  `regex_output_filter` are *singletons*: only one entry is meaningful, so
  `hiveloom add guardrail` replaces an existing one (including the injected
  default `max_cost_usd`) rather than appending a redundant second entry.
  `regex_output_filter` composes as a list — one entry per pattern.
- **Validators:** `output_schema` (JSON-schema check), `regex_match`,
  `file_exists`, `command_succeeds` (exit 0 = pass; `timeout` seconds, default
  600, and confined like any other spawn), `grounded_references` (selected
  output IDs must occur in approved evidence from this run).
- **Policies:** `react`, `plan_then_act`, `sequential_steps` (walks the fixed,
  ordered `loop.steps` list; object steps can enforce tools and call limits),
  `best_of_n` (experimental consensus over `loop.attempts` samples).
- **Compaction:** `summarize`, `truncate_oldest`.
- **Hooks:** `strip_json_fence` (an opt-in final-output normalizer).

`best_of_n` is experimental on the ARC branch. It rewinds conversation history
between samples and chooses a plurality over whitespace-normalized outputs.
Tool state is shared between attempts; journal replay and general-purpose
verification integration still need the work described in
[evolution-migration.md](evolution-migration.md#multiple-attempt-consensus-best_of_n).

```yaml
loop:
  policy: best_of_n
  attempts: 5
  max_turns: 60  # shared across all attempts
```

`attempts: 1` provides a single-sample control. The trace records
`attempt_recorded` and `attempts_selected`; ties select the first answer.
This policy does not automatically increase the configured model token budget.

Structured sequential steps make deterministic phases inspectable and
enforceable:

```yaml
loop:
  policy: sequential_steps
  steps:
    - id: read
      instruction: Read the deal.
      tools: [read_deal]
      require_tool_calls: [read_deal]
      max_model_calls: 2
      max_tool_calls: 1
    - id: search
      instruction: Find and verify candidates.
      tools: [search_and_verify_candidates]
      require_tool_calls: [search_and_verify_candidates]
    - id: answer
      instruction: Produce the final answer.
      tools: []
```

Omitting `tools` preserves the current active set; `tools: []` creates a
tool-free phase. Required calls must succeed before the step advances. A
non-final step advances as soon as all required calls succeed; otherwise a
no-tool response is the completion signal. Hidden tool calls are blocked
before dispatch. Limit exhaustion ends with `status: step_failed` and exit 4.

Legacy string steps retain their instruction-only behavior. A structured tool
constraint cannot currently be combined with playbooks, and a deferred tool
cannot be required directly. Use `run --dry-run --json` to inspect the
effective tools, requirements, and limits for every step. Build the value with
`hiveloom set`; never edit `harness.yaml` by hand.

Use one deterministic composite tool when multiple upstream calls are a single
domain operation and an invariant must hold before results reach the model.
Search followed by an eligibility check is one example. Keep calls separate
when they are independently useful, need different permissions, should run in
parallel, or need separate audit or human review. Structured steps control
phase order and tool access without provider-specific filtering.

Code hooks are the primary extension point. A validator hook has the signature
`validate(run_output, run_context) -> {"passed": bool, "feedback": str}`. It may
add an optional third `verification_context` parameter to inspect bounded,
redacted tool calls, step receipts, and artifacts from the current run. A tool
hook is any `@hiveloom.tools.tool`-decorated function (its JSON input schema is
derived from type hints). `hiveloom add …/--code` scaffolds a correctly-signed
stub.

Use `grounded_references` when a JSON schema proves shape but not provenance:

```bash
hiveloom add validator --builtin grounded_references \
  --output-path '$.selected[*].talent_id' \
  --evidence-path 'search_candidates=$.candidates[*].talent_id' \
  --dir ./h --json
```

`--evidence-path TOOL=JSON_PATH` may be repeated. The supported deterministic
path subset is `$`, dotted keys, `[*]`, and non-negative list indexes. Evidence
comes only from allowed calls executed in the current run; failed or differently
named tools cannot satisfy the validator. Scalar values are normalized to
strings, null and missing values are ignored, and failure feedback lists only
missing normalized references rather than the surrounding private records.

A tool that declares a `run_context` parameter is handed the run context
(`input`, `harness_dir`, `run_id`, `harness_id`, `harness_name`,
`harness_version_hash`, `hive_path`, `artifacts`, and the caller's own
`context` dict from `run_harness(context=...)`) instead of having the model
supply it; the
parameter is hidden from the tool's JSON schema and cannot be forged by a
model-supplied key of the same name. A tool that returns a `ToolResult` may
attach `Artifact(kind=..., data=...)` side-products, which reach the caller on
`RunResult.artifacts` without passing through the model's text channel.

Metric objectives let evolution use numeric evaluator history without treating
it as a binary outcome:

```yaml
evolution:
  objectives:
    - metric: recall_at_5
      direction: maximize
      unit: ratio
    - metric: hallucination_rate
      direction: minimize
      ceiling: 0
    - metric: billed_cost_usd
      direction: minimize
      unit: USD
```

Construct the list with `hiveloom set evolution.objectives '...' --dir ./h
--json`; never edit the YAML directly. `metric` names are unique within the
list. Optional `source`, `scope`, and `unit` filters narrow the series. A finite
`floor` or `ceiling` is a hard per-observation constraint, not a weighted
preference. Objective policy is frozen from evolution.
## Large tool results

A tool result above `context.tool_results.max_inline_bytes` (16 KB by default)
is **spilled**: the whole result is written to run-private storage beside the
journal, and the model gets a bounded head/tail preview, the omitted byte
count, and an opaque handle:

```
REPORT HEAD
… first 2 KB …

[hiveloom spill] handle=tr_9f2c1a04b7e35d16 — this result was 184320 bytes;
181248 are omitted here and stored whole.
Shown above: bytes 0-2048. Shown below: bytes 183296-184320.
Read the omitted part with read_tool_result(handle="tr_…", offset=2048,
limit=4096), or locate it first with search_tool_result(handle="tr_…",
query="..."). Do not guess at the omitted content — read it.
[/hiveloom spill]

… last 1 KB …
REPORT TAIL
```

`read_tool_result`, `search_tool_result` and `transform_result` are added
automatically, and stay **inactive until the first spill** — a harness that
never spills never pays for them in its tool payload. None is spellable in a
spec, and none can be reached by `search_tools`.

### Reshaping a stored result in place

Reading a 40 MB log back 4 KB at a time to find twelve lines spends the whole
context budget on the 39.9 MB that did not matter. `transform_result` runs a
fixed set of ops against the *stored bytes* and returns only what they produced:

| `op` | Arguments | Returns |
|---|---|---|
| `lines` | `start` (1-based), `count` | a numbered line range |
| `head` / `tail` | `bytes` | the first/last bytes |
| `grep` | `pattern`, `max_matches` (≤ 200), `context_lines` (≤ 5) | matching lines |
| `json_path` | `path` | the selection, as JSON |
| `count` | `pattern` (optional) | lines, bytes, matching lines |
| `sort` | `unique` | the lines in order |
| `unique` | — | distinct lines, first seen first |
| `concat` | `handles` (≤ 8 objects in total) | the objects joined |

If the output fits `max_inline_bytes` it comes back inline. If it does not, it
is stored as a **derived object** with its own handle (the sidecar records
`derived_from` and the op, and a `tool_spilled` event records it as minted), so
a narrowing that is still too large can be narrowed again rather than
abandoned.

Deliberately not a scripting surface. Each op is bounded in what it may scan
(32 MB, streamed in windows) and what it may emit (4 MB); a `grep`/`count`
pattern is at most 256 characters, is applied to **one line at a time** rather
than to the whole buffer, and is refused when it repeats a group that already
contains an unbounded quantifier — the catastrophic-backtracking shape. A
"line" is itself bounded at 1 MiB, so an object with no newlines in it is still
matched against bounded input. No op can name a path, and `concat` reaches only
objects this run was already authorized to read.

### Handing a stored result to another tool

A tool may declare which of its string parameters accept a handle in place of a
literal. `file_write` declares `content`, so a large result can be written out
without the model re-emitting it token by token:

```json
{"path": "report.txt", "content": "tr_9f2c1a04b7e35d16"}
```

At dispatch the runtime replaces a value that matches the handle pattern *in
full* with the stored object's text, resolved under the run's authority. A
value that is not a handle is passed through unchanged, and a handle this run
cannot resolve is a tool error — never a silent literal, which is the one
outcome indistinguishable from success. One expansion is capped at 4 MiB. The
journal's `tool_call` event and the provider both keep the handle; the
expansion exists only for the duration of the call.

Code tools opt in through the decorator — see
[Handle-typed parameters](extending.md#handle-typed-parameters).

The point is that nothing is thrown away. Before, an oversized result was cut
to its leading characters and the model was told the rest was in the trace —
which its file tools deliberately cannot read, so the omitted part was gone for
the rest of the run. Now the tail (where totals, summary lines and error
messages live) survives in the preview, and everything between is one tool call
away.

Rules that keep it honest:

- Spilling runs **after** `after_tool_call` hooks and guardrails, so what is
  stored is the accepted canonical result — not a value something later
  rewrote or rejected.
- The journal still records the result in full; spilling changes only what the
  model carries.
- `logging.redact` is applied **before** the write. A spilled object carries
  exactly what the journal would have carried.
- **A handle is not a capability.** Authority is a recorded fact, never
  inferred from a name: each run writes into its own `spill/<run_id>/`
  directory (0700, objects created exclusively at 0600) and resolves handles
  through a per-run map. Inventing one, guessing one, or quoting one back in
  the conversation grants nothing — including in a resumed fork, whose
  transcript is model-visible text.
- **A fork inherits explicitly.** `hiveloom fork` copies the objects its
  context quotes *and* the parent's hash-verified journal records as minted,
  then writes the handle list into `fork.yaml`. The resumed run authorizes that
  list, not whatever its messages happen to mention.
- **`shell` never sees the store.** The retrieval tools are the only sanctioned
  route from private storage into a model request; spawned processes have the
  whole of [runtime-private state](#runtime-private-state) masked away, and a
  `shell` harness will not run where that cannot be enforced.
- Retrieval results are never themselves spilled, and a read is capped at
  `max_inline_bytes`, so reading back can neither loop nor blow the budget.
- Storage is best effort: if the object cannot be written, the full result goes
  to the model unchanged.

Set `max_inline_bytes: 0` to switch spilling off; oversized results are then
truncated in place and the omitted part is unreadable for the rest of the run.
A tool that truncates *its own* output before returning (`http_get` caps the
response body) is outside this mechanism — the runtime can only preserve what
the tool hands it.

## Notes

`notes` is opt-in scratch storage for the executor — the write side of the same
boundary spilled results live behind. Everything a model concludes otherwise
lives in the conversation, and the conversation is the one thing compaction is
allowed to throw away.

```yaml
tools:
  - builtin: notes
    max_notes: 32         # notes held at once (hard cap 256)
    max_note_bytes: 0     # largest note; 0 = context.tool_results.max_inline_bytes
```

The model calls it with `action: write | read | delete` and a `name`
(`^[a-z0-9][a-z0-9_-]{0,63}$`), plus `content` on a write or `offset`/`limit`
on a read. Writing an existing name replaces that note.

What the conversation carries is not the note but the **index** of notes,
rendered into the system prompt in a stable order:

```
# Notes
Your own notes for this run. They are not part of the conversation, so they
survive when older turns are compacted away. …
- findings (412 bytes): the invoice total disagrees with the line items
- plan (96 bytes): 1. reconcile totals  2. flag the mismatch
```

So a note survives compaction by construction — it was never a message — and
costs one line per note rather than a body. An empty store renders no section.

The same rules as spilled results, for the same reasons:

- notes live under the run's own directory beside the spill objects (0600 files
  in a 0700 directory), inside the trace directory `file_read`/`file_write`
  refuse and confinement masks from spawned processes;
- **a name is not a capability**: resolution goes through a per-run map, and a
  fork re-authorizes from the hash-bound `notes_manifest` in `fork.yaml`, which
  `hiveloom fork` replays from the parent's *verified* journal — a note written
  and then deleted is not a note the fork inherits;
- `logging.redact` is applied **before** the write;
- every write and delete is journaled (`note_written`, `note_deleted`), so
  `trace --verify` and `fork` see the store the model saw;
- counts and sizes are bounded by the spec, and a write past either limit is a
  tool error naming the limit rather than a silent drop. A read is ranged like
  `read_tool_result`, so reading a note back cannot exceed the inline budget.

Notes are **run-scoped**: they are discarded with the run, and nothing in them
reaches the harness, the Hive or another run. Durable lessons belong in
[`memory`](#memory), which is spec state and goes through proposals.

## Recalling prior runs

`recall_runs` is opt-in memory: declare it and the executor can look up **this
harness's own** earlier runs while it works.

```yaml
tools:
  - builtin: recall_runs
    limit: 3              # most runs per call (hard cap 10)
    include_output: true  # include what a past success produced
    scope: harness        # or: version — only runs of the version now executing
```

The model calls it with `status` (`success` for worked examples, `failed` for
runs the validators rejected and why, `any` for both), an optional `query`
matched against past tasks and outputs, and a `limit`. A failed run comes back
with the verifier feedback that rejected it and any guardrail that fired —
which is the part that makes it instructive rather than discouraging.

The evolver already reads this history *between* runs; this makes the same
evidence available *during* one. For a small executor, a worked example of the
same task from the same harness is usually worth more than another paragraph of
instruction.

Three properties bound it:

- **Own history only.** The harness key comes from the run context, not from
  tool input, so there is no parameter through which a model could ask for
  another harness's runs — or another harness's customer data.
- **Already redacted.** The Hive is ingested from journals, and `logging.redact`
  is applied before a journal is written, so recall can only return what the
  record already kept. Set `include_output: false` where even that is too much.
- **Bounded.** Runs per call and characters per field are both capped, and a
  large recall spills like any other tool result.

Recall reads the same Hive the run will be ingested into. A harness with no
history yet gets a plain "nothing recorded" answer rather than an error.

## Memory

`recall_runs` above is *lookup*: the executor asks about earlier runs while it
works. `memory` is the layer above it — what the harness has already concluded,
carried into every run without anyone asking:

```yaml
memory:
  enabled: true              # frozen from evolution
  max_entries: 24            # frozen; hard cap 200
  max_entry_chars: 600       # frozen; hard cap 4000
  prompt_budget_chars: 6000  # frozen; hard cap 40000
  entries:                   # evolvable
    - id: iso-dates
      kind: rule             # fact | rule | example
      title: Dates in ISO 8601
      content: Emit dates as YYYY-MM-DD; the validators reject locale formats.
      source: prop_2f1c9a
      evidence: 3 runs rejected by the date_format validator
      created_at: 2026-09-21T18:00:00+00:00
```

Entries render as a `# Memory` section of the system prompt, after the skills
index and before the tool guidelines, one bullet per entry in declaration
order:

```
# Memory
Lessons from earlier runs of this harness. Treat them as standing constraints
on how you work, not as the current task.
- [rule] Dates in ISO 8601: Emit dates as YYYY-MM-DD; the validators reject locale formats.
```

What keeps it from becoming drift:

- **The executor never writes it.** No tool edits `memory`. An entry reaches
  `harness.yaml` only through `hiveloom memory add` (validated and rolled back
  like every construction command) or through an applied evolution proposal —
  gate, full re-validation, `# evolved: N`, rollback. The executor's most
  privileged action, with `propose_memory` declared, is to put a suggestion in
  that review queue (below).
- **A proposal appends; it does not overwrite.** An evolution proposal writes
  the reserved final segment `+` — `memory.entries.+` — which is resolved
  against the list on disk when the proposal is *applied*, not when it is
  queued. A numeric index still works (an existing one replaces that entry, one
  equal to the current length appends), but a position chosen at queue time is
  stale as soon as another lesson lands. Because an append needs no particular
  version to be correct, an append-only proposal (no code changes) may apply
  after the harness has moved on; every other proposal is refused with *harness
  has changed — regenerate*. The gate, full re-validation and rollback still run
  at apply, so an append that would break a budget is refused there. See
  [deploying-and-evolving.md](deploying-and-evolving.md#proposing-a-durable-lesson).
- **The budgets are frozen from evolution** and hard-capped in the schema. A
  harness can add a lesson; it can never raise the ceiling on how many lessons
  there are, how long one may be, or how much prompt they occupy — nor set
  `enabled: false` to hide what it already wrote. `memory.entries` is the only
  path under `memory` the gate will ever accept. *Frozen from evolution* is not
  frozen from you: `hiveloom set memory.max_entries 10` is the sanctioned
  builder-side path, as for every other frozen field.
- **Bounded, not truncated.** Too many entries, an over-long one, or a rendered
  section above `prompt_budget_chars` is a validation error at write time (exit
  3, nothing written), never a silent eviction at run time. Which lesson to drop
  is a review decision.
- **A memory change is a behavior change.** Entries live in `harness.yaml`
  rather than a side file, so they move the spec version hash and land in their
  own fitness bucket — the effect of a lesson is measurable like any other
  mutation. A harness with an all-default `memory` section keeps its exact
  previous YAML and hash.
- **What the model saw is journalled.** The section is part of the assembled
  system prompt, so it appears in the `context_system` event and `trace
  --verify` and `fork` reproduce it.
- `evidence` is for whoever reviews the entry; it is not rendered into the
  prompt. Whitespace in `content` is collapsed on render, so one entry is always
  one line.

Operator commands (all with `--json`, exit 3 on a spec error):

```bash
hiveloom memory list ./harness                       # entries + budget usage
hiveloom memory show ./harness iso-dates             # one lesson, with evidence
hiveloom memory add ./harness --kind rule \
  --title "Dates in ISO 8601" \
  --content "Emit dates as YYYY-MM-DD." \
  --evidence "3 runs rejected by date_format"        # id is slugged from --title
hiveloom memory forget ./harness iso-dates
```

Set `enabled: false` to keep the entries in the spec while showing the executor
none of them — that is how to measure whether memory is earning its tokens.

### Letting the executor propose a lesson

The run that discovers a constraint is the one best placed to state it. The
opt-in `propose_memory` tool lets it do so without weakening anything above:

```bash
hiveloom add tool --builtin propose_memory --param max_per_run=2 --json
```

The model calls it with `kind`, `title`, `content` and (optionally) `evidence`.
What happens then is deliberately unexciting: the lesson is redacted with this
run's `logging.redact` patterns, checked against the running spec's memory
budgets, journaled as a `memory_proposed` event, and queued as an ordinary
`MutationProposal` appending to `memory.entries.+` — `trigger: executor`, gated
by the same code path as an evolved one, no strong-model call. It then waits
for `hiveloom proposals apply --yes`, which re-gates, re-validates and rolls
back like any other mutation. The tool never touches `harness.yaml`.

- Identity, version and Hive path come from the run context, never from tool
  input, so a model cannot propose into another harness's queue.
- Proposals dedup on the *content* of the lesson against the harness and spec
  version, so a run that restates what it already proposed gets the pending row
  back instead of queueing a near-duplicate.
- `max_per_run` (default 3, hard cap 10) bounds what one run may add to the
  queue. All of them can be applied: each is an append, so applying one does
  not strand the others against the version they were queued at.
- Memory turned off, an unreachable Hive, a spent cap, a lesson already stored
  or already pending: the tool answers in plain words and the run carries on.
  Proposing is an aside, never something to recover from. A malformed or
  over-long lesson *is* a tool error, because the model can restate it.
- **Eval runs do not queue.** A matrix of 200 cases would file 200 restatements
  of one finding, so a run the eval runner launched records its
  `memory_proposed` event and says so in the result instead. The marker is an
  `eval_run_id` key in the caller `context` that `hiveloom.eval_runner` passes
  to every cell; an embedding caller with its own `execute_cell` passes the
  same key to get the same behavior.

## Process confinement

Two builtins start subprocesses: the `shell` tool (argv the *model* chose, from
your allowlist) and the `command_succeeds` validator (a command *you* chose).
Allowlisting which command may run is a different question from what it may do
once it runs, and `confinement` answers the second:

```yaml
confinement:
  mode: auto              # auto | off | require
  network: false          # may spawned processes reach the network?
  writable: true          # may they write inside the harness directory?
  hide_home: true         # keep the user's home directory out of reach
  env_passthrough: []     # extra environment variables to forward
  timeout_seconds: 30     # wall-clock ceiling for a `shell` call
  max_output_bytes: 1048576 # retained per stream (stdout and stderr)
  max_memory_mb: 2048     # 0 = unlimited
  max_processes: 0        # RLIMIT_NPROC; per-user, so 0 unless hiveloom owns the uid
```

Every spawn gets a **portable baseline**, on every platform: a scrubbed
environment (an allowlisted command cannot read the API key that pays for the
run — only `PATH`, `HOME`, locale, and anything in `env_passthrough` survive),
closed stdin, its own session so a timeout kills the whole process tree rather
than orphaning a backgrounded grandchild, POSIX resource limits, and output
streamed through bounded head/tail collectors so a runaway writer cannot
exhaust the runtime's memory or disk.

Where the platform has a sandbox — `bwrap` (bubblewrap) on Linux,
`sandbox-exec` on macOS — it also gets **kernel-enforced isolation**: the
filesystem readable but writable only inside the harness directory, a private
`/tmp` (another process's scratch files are not part of the machine a command
gets to see), no network at all unless `network: true`, and — unless
`hide_home: false` — no access to the user's home directory, where SSH keys,
cloud credentials, shell history and the Hive itself live.

`HOME` never points at the operator's home either: a confined command gets a
private scratch directory, so a tool that looks for credentials by convention
finds an empty one. That part holds on every platform, backend or not. A
harness that lives *inside* home is still reachable — the directory a command
runs in is always bound in, and hiding home does not undo that.

### Runtime-private state

One effective `RunBoundary` decides what counts as the runtime's own state after
SDK/CLI `trace_dir` and `hive_path` overrides are known. Every builtin, verifier,
trace writer, spill store, and diagnostic uses that same object, so an override
cannot create a second, less-protected interpretation of the spec:

- `.hiveloom/` and the configured trace directory (wherever it is, inside the
  harness or out), which hold the journal — every tool result, in full;
- the spill store beside it;
- the Hive database and its `-wal`/`-shm` sidecars;
- the trust and authorized-key stores, and `$HIVELOOM_HOME`;
- `.env` and other credential files (re-read per spawn, so one written mid-run
  is private from the moment it exists).

When an OS sandbox backend is active, spawned processes cannot reach any of it.
Directories are masked with an empty mount and files with an inaccessible one,
applied *after* the harness bind so a path inside the harness is hidden while
the directory around it stays readable. Masks are applied to resolved targets,
so a symlink pointing into private state leads to the mask, not around it. On
every platform, the `shell` tool also refuses *arguments* that resolve into
those paths. That catches the direct form (`grep secret .hiveloom/traces`) but
not a recursive walk that never names the directory; only the optional kernel
mask stops that class of read. Therefore a shell rule that accepts arbitrary
extra arguments for a file-reading command is executable only when that mask is
active. Without a backend the model may use harmless `echo`/`printf` arguments,
or exact argv declared by the harness author, but cannot choose traversal paths;
an exact recursive walk across runtime-private state is refused too.

### Modes, and what happens without a backend

`mode` decides what happens when no backend exists:

- **`auto`** (default) — uses the strongest available backend, otherwise runs
  with the portable controls. A missing backend never blocks the run.
- **`require`** — refuses to spawn anything without a sandbox, `shell` or not.
- **`off`** — skips backend discovery: portable controls only, with shell
  private-path and variable-reader restrictions still applied. (`off` is a YAML
  boolean unquoted; the spec takes it either way.)

The baseline is a scrubbed environment, resource limits and a timeout. It is
**not** filesystem isolation, and nothing in the runtime describes it as such.

```bash
hiveloom confinement <harness>   # what THIS machine will enforce for that harness
hiveloom confinement             # the machine's capability alone
hiveloom confinement <harness> --json
```
```json
{
  "ok": true,
  "backend": "bwrap",
  "filesystem_isolated": true,
  "runtime_state_hidden": true,
  "network_isolated": true,
  "home_hidden": true,
  "provider_egress_policy": "redact",
  "provider_egress_active": true,
  "prompt_injection_boundary": {
    "safe_for_untrusted_input": true,
    "http_undeclared_hosts_require_approval": true
  }
}
```

A directory that is named must load — the diagnostic will not answer for a
harness that does not exist by falling back to defaults it never declared.

The resolved backend is recorded in `run_started`, so a journal states whether
a run's processes were really isolated rather than implying it from the spec.

`confinement` is frozen from evolution: a harness that could widen its own
containment does not have one. And the boundary is around *spawned processes* —
code hooks, extensions, and MCP servers still run with the permissions of the
hiveloom process by design, gated by the trust prompt. See
[task confinement](task-confinement.md).

## What may leave the machine

Capability scoping limits what a model may read. `egress` is the last check on
what leaves: the exact system prompt, messages, and tool schemas handed to the
provider are inspected after request hooks and before the call.

```yaml
egress:
  mode: redact          # redact | block | off
  detect_credentials: true
  patterns: []          # extra regexes, on top of logging.redact
```

`redact` (the default) replaces matches in the outgoing copy; history keeps
what actually happened, so the journal stays a true record and the model simply
never carries the secret onward. `block` refuses the request and halts the run
instead. Both journal a `provider_egress_redacted` / `provider_egress_blocked`
event carrying pattern names and counts — never the matched text, because a
safeguard that recorded what it caught would be the leak it exists to prevent.

Two sources of rules: the harness's complete `logging.redact` policy (recursive
keys, structured paths, and regex patterns) and well-known credential shapes
(private key blocks, AWS keys, Anthropic/OpenAI/GitHub/Slack/Google tokens,
JWTs, bearer headers). Dictionary keys are screened as well as values. The
recorded request checksum is calculated after hooks and screening, so it
describes what actually went on the wire.

External tool calls are outbound boundaries too. Arguments to `http_get` and
MCP tools pass through the same screen; a match blocks the action rather than
silently rewriting it. `http_get` also scopes destinations: `hosts` pre-approves
exact hosts or `*.example.com` subdomains, while an undeclared host requires a
one-run operator decision. A non-interactive caller that supplies no approval
callback denies it, and every redirect is checked against the resulting set.

This is defence in depth, not a prompt-injection detector. Pattern matching
cannot recognise arbitrary sensitive text — a previous run's customer data is
not shaped like a credential. The primary controls remain narrow typed tools,
declared destinations, and the shell's portable restrictions; OS isolation adds
defence in depth where available. `egress` is frozen from evolution.

## Playbooks

A **skill** is reference material the model reads; a **playbook** is a
configuration the runtime applies. Entering one swaps in a prompt fragment,
narrows the active tools, and adds mode-specific validators — so one harness
covers what would otherwise need several, while keeping one conversation and
one evolving spec.

```yaml
playbooks:
  - name: overview
    description: Read the segment landscape. No actions.
    prompt: playbooks/overview.md
    tools: [run_sql, render_chart]
    entry: true

  - name: targeting
    description: Turn a cohort into a confirmable proposal.
    prompt: playbooks/targeting.md
    tools: [run_sql, render_chart, propose_decisions]
    validators:
      - code: validators/proposal.py:check_consent
    on_enter: hooks/refresh_features.py:run
    on_exit: hooks/require_proposal.py:check
```

Declaring any playbook auto-adds a `switch_playbook` tool, which stays active
in every mode — a mode the model cannot leave is a trap, not a mode. The run
starts in the `entry` playbook, or the first one declared.

**Gates.** `on_enter` and `on_exit` receive `{playbook, from/to, reason,
run_context}` and may return `None` to observe, `{"context": str}` to inject a
note into the conversation, or `{"block": True, "reason": str}` to refuse. A
blocking `on_exit` is a *boundary* check — the mode grades itself as the agent
leaves ("you entered targeting and proposed nothing") instead of waiting for
the end of the run. A gate that refuses three times running is force-released,
so a badly written gate cannot trap the run; the release is traced. A hook that
raises is recorded as `hook_error` and skipped, never crashing the run.
Refusals reach the model as a tool error and are not retried.

**Evidence.** Each switch is a `playbook_switch` trace event and a
`playbook_enter`/`playbook_exit` lifecycle event. The Hive indexes them, so
`hiveloom stats` breaks success, cost, turns, and refusals down per playbook,
and the failure report localizes a problem to one mode. Attribution is *by
visit*: a run that worked in two modes counts once for each.

**Its own model.** A playbook may declare `model:` (and `model_provider:`),
so a mode runs on a different executor: profile cheaply, decide expensively,
inside one harness and one conversation. Leaving the mode restores the
harness default — a mode is a configuration, not a one-way door. The switch
happens at a turn boundary, where prior turns are stripped of content only the
previous model can validate.

**Freeze.** `on_enter`/`on_exit` execute code, and `model`/`model_provider`
are the same cost-and-capability decision that already keeps top-level `model`
frozen. None can be changed by evolution, including through a rewrite of the
surrounding `playbooks` list. Prompts are the evolvable part — which is the
point: evolution rewrites one mode's guidance on that mode's own evidence.

## MCP servers

A harness can declare MCP servers; their tools become ordinary dispatchable
tools inside the loop, named `mcp__<server-name>__<tool>`.

An MCP tool can reach the *caller* as well as the model. Returning structured
content under a `_hiveloom` envelope —
`{"_hiveloom": {"artifacts": [{"kind": "chart", "data": {...}}]}}` — lands
those entries on `RunResult.artifacts` exactly as a local code tool's would,
and the envelope never enters the model's text. This is what lets a domain
tool that also drives a UI be hosted on a server instead of copied into every
harness that needs it. Discovery is
**eager** — it happens when the tool registry is built, which includes
`run --dry-run`. Dry-run never calls the model API, but a harness with
`mcp_servers` genuinely performs local/network I/O to discover their tools
(see AGENTS.md rule 5). `mcp_servers` is **always frozen** from evolution —
the same risk class as `extensions` (arbitrary code/process).

A stdio entry launches a local subprocess — **arbitrary local exec** —
gated by the same harness-trust boundary as any other code hook (see
`hiveloom trust`):

```yaml
mcp_servers:
  - name: search
    transport: stdio
    command: npx
    args: ["-y", "@foo/mcp-search"]
    env_from_host_env:
      API_KEY: FOO_SEARCH_API_KEY   # resolved from the host env at connect time
```

An http entry reaches a Streamable HTTP endpoint:

```yaml
mcp_servers:
  - name: jira
    transport: http
    url: https://mcp.acme.com/mcp
    header_env:
      Authorization: ACME_MCP_TOKEN
    tools: [search_issues, create_issue]   # allowlist; omit to expose all
```

Add one with `hiveloom add mcp-server` (see `hiveloom add mcp-server --help`);
inspect what a harness's declared servers actually expose with
`hiveloom mcp list-tools --dir ./h`.

## Three identities, three jobs

- `schema_version` identifies the `harness.yaml` document format.
- The behavior hash identifies the validated spec plus referenced prompts,
  hooks, extensions, skills, and output schemas.
- The execution fingerprint identifies one reproducible run configuration:
  behavior hash, Hiveloom runtime, requested and effective model identity,
  runtime overrides, input digest, model path, and lineage.

The legacy top-level `version` key remains readable for a transition. A
document containing conflicting `version` and `schema_version` values is
rejected. Run `hiveloom migrate HARNESS --json`; never edit either field by
hand. Migration validates hooks before and after its atomic write, restores the
original bytes on failure, and does not change the behavior hash.

## External run metrics

Numeric evaluator results are not part of `harness.yaml`. They are immutable
Hive records attached to an indexed `run_id`, with a user-defined name, finite
value, maximize/minimize direction, unit, source, and case/run/eval scope.
Inspect the machine contract with `hiveloom metrics schema --json`; record or
transactionally import observations with `hiveloom metrics record|import`, and
query them with `hiveloom metrics list`.

Metric aggregation keeps name, source, scope, unit, and direction separate and
reports both sample and missing-value counts. Binary `run_outcomes` remain the
contract for deferred success or failure and are not reinterpreted as numeric
metrics.

The separate versioned eval document selects a registered dataset loader and
scorers without changing the harness schema. Inspect it with `hiveloom eval
schema --json` and validate its components with `hiveloom eval validate`; see
[evaluating.md](evaluating.md).

## Safety invariants (enforced in code)

1. The evolver can never modify `id`, `guardrails`, `model`, `logging.redact`,
   `extensions`, `hooks`, `mcp_servers`, `evolution.auto_propose`,
   `evolution.trace_excerpts`, `evolution.objectives`, or the `memory` budgets
   (`enabled`, `max_entries`, `max_entry_chars`, `prompt_budget_chars`) — nor any
   playbook's `on_enter`/`on_exit`, including by rewriting the `playbooks` list
   around them. Playbook *prompts* stay mutable: evolution rewrites guidance,
   never side-effecting code.
2. Code-hook regeneration always requires explicit human approval.
3. `shell` is allowlist-only and disabled unless the spec enables it.
4. Redaction patterns are applied before any trace is persisted.
5. The cost guardrail defaults **on** (`max_cost_usd: 1.00`) even if omitted from
   a spec.
6. Every subprocess the runtime spawns runs with a scrubbed environment, a
   timeout, bounded output and resource limits. OS isolation is opportunistic
   in `auto`, mandatory only in `require`, and skipped in `off`; the reported
   confinement facts never describe the portable baseline as isolation.
7. A spill handle grants no authority on its own: inheritance requires a
   verified parent journal and a size/hash-bound manifest; unchained journals
   may fork as context but grant no spill bytes.
8. Provider requests and external tool arguments are screened before they leave.
9. New `http_get` hosts require an operator decision unless pre-approved.
10. `recall_runs` is scoped to the running harness from the run context, so no
   tool input can widen it to another harness's evidence.
11. `propose_memory` writes no spec. It queues a gated proposal scoped the same
   way, redacted before it is stored, bounded per run — and a human still
   applies it.

## The harness directory

```
<harness-name>/
├── harness.yaml          # the spec
├── tools/  validators/  schemas/  playbooks/
├── .hiveloom/
│   ├── traces/           # in-folder trace dir (memory travels with the harness)
│   └── forks/<name>/     # experiments on this harness (`hiveloom fork`)
├── .env.example          # every env var the spec/hooks reference
├── pyproject.toml        # PEP 621 deps: hiveloom==<pinned> + hook deps
└── README.md
```

The folder is portable and versionable, like a `docker-compose.yml`; it needs the
runtime (`pip install hiveloom`) wherever it lands. `hiveloom package` bundles it
into `<name>-<version_hash>.zip` (+ optional Dockerfile), excluding `.env` and
`.hiveloom/`.

A fork is a full harness directory of its own — spec, code hooks, and a
`fork.yaml` naming the run and seq it re-entered — kept under
`.hiveloom/forks/<name>` rather than beside the harness. It is an experiment
*on* this harness, not a harness of its own: archiving or packaging the folder
therefore leaves the experiments behind with the traces, a directory of
harnesses stays a directory of harnesses, and the file tools — rooted here and
not descending into `.hiveloom` — cannot read or mutate a running experiment.
Forking a fork produces a sibling under the same original harness rather than a
deeper nest; `fork.yaml` is the only record of generation.
