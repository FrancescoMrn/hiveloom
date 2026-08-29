# The run journal, forks, and model swaps

Every run writes an append-only JSONL **journal** into the harness's
`.hiveloom/traces/`. From 1.0.0 that journal is progressive, self-describing,
and tamper-evident — complete enough to read a run back turn by turn, and
complete enough to **re-enter it** at any model call and continue from there
against an edited harness.

This page covers what the journal records, how to check it, and the three
things you can do with it afterwards: fork, resume, and swap models.

```bash
hiveloom trace <run_id>                    # summary + ordered events
hiveloom trace <run_id> --verify           # walk the append-only hash chain
hiveloom trace <run_id> --materialize 42   # the exact request sent at seq 42
hiveloom fork <run_id> --list              # the model calls you may re-enter
hiveloom fork <run_id> --at 42 --name probe
hiveloom run <harness>/.hiveloom/forks/probe --resume
hiveloom lineage <run_id>                  # the fork tree and its divergence points
```

## The record is progressive, not snapshot-shaped

Before 1.0.0 every `model_call` re-serialised the whole conversation, which is
O(n²) in turns and left "the state at seq N" undefined — every reader
re-derived it differently. Now the conversation is recorded **once**, message by
message:

| Event | Records |
|---|---|
| `context_append` | one message appended to the conversation |
| `context_compaction` | history rewritten; the payload carries the result |
| `context_system` | the assembled system prompt, only when it changes |
| `context_tools` | the active tool payload, only when it changes |
| `model_call` | `{turn, phase, context_head, system_hash, tools_hash, messages_hash}` |

A `model_call` therefore *references* the folded context instead of copying it.
The prompt at any turn is a fold over the events up to its `context_head`, and
compaction is another fold operation rather than a special case.

On a 12-call run this cut the journal from 676 KiB to 184 KiB, and `model_call`
from 85% of the file to 2.5%.

`hiveloom.logging.journal` is that fold, and it is the *one* implementation
shared by `trace --materialize`, `fork`, and the workbench:

```python
from hiveloom.logging.journal import read_events, state_at_model_call, verify_chain

events = read_events(".hiveloom/traces/run_abc123.jsonl")
state = state_at_model_call(events, 42)   # ContextState(system, messages, tools)
request = state.as_request()              # exactly what the provider was sent
```

Pre-1.0 traces still fold correctly: a `model_call` carrying inline
`system`/`messages`/`tools` is treated as a wholesale replace.

## "Read-only" is a claim you can check

Each event carries `prev`: the sha256 of the line before it. `--verify` walks
the chain and reports the first break with the position it broke at, exiting
`4`.

```bash
hiveloom trace run_abc123 --verify
# intact: 61 events, chain unbroken
```

Three outcomes, deliberately distinct:

- **intact** — every line commits to the one before it.
- **BROKEN at line N** — a line was edited, reordered, or removed. Truncating
  the *tail* is not a break: a prefix of an append-only log is always valid.
- **unchained** — the events carry no `prev` at all, because they were written
  before 1.0.0. "We cannot tell" is a different answer from "it was tampered
  with", and conflating the two would make the check useless for exactly the
  traces most likely to be old.

## The harness is in the record

`run_started` carries a **harness snapshot**: the dumped spec plus a
`path -> sha256` manifest of every local behavioural file — tools, validators,
hooks, playbook prompts — the same set the version hash fingerprints. You can
name and reconstruct the spec that ran without the folder being present.

Set `logging.snapshot_files: true` to inline the file *bodies* too, bounded at
256 KiB, reporting whatever it skipped. That makes a journal self-contained at
the cost of size; the default records hashes only.

`run_finished` closes the record with the run's `output`, `verdicts`,
`artifacts`, `model_path`, `models_used`, and the same `execution` envelope
returned by the SDK and CLI. That envelope keeps the requested, resolved, and
provider-reported model identities separate; sums provider-call usage; labels
cost as billed, estimated, or mixed; and records whether verification passed
on the first output, recovered, failed, or never ran.

`behavior_hash` is the current name of the harness version hash inside this
public envelope. `schema_version` reflects the canonical harness document
field. Legacy documents using `version` load with the same meaning and migrate
without changing the behavior hash, so their existing journal and Hive buckets
remain comparable.

## Levels

```yaml
logging:
  level: journal   # or: summary
  snapshot_files: false
  trace_dir: .hiveloom/traces
```

| Level | Records | Forkable |
|---|---|---|
| `journal` | the full progressive record, context included | yes |
| `summary` | events and tool calls, no context bodies | **no** |

The old names still load in both `harness.yaml` and `TraceWriter`
(`full` → `journal`, `tool_calls_only` → `summary`), so existing harness
folders keep working. The names changed to say what they cost you: the reason
to pick one over the other is whether you will be able to fork.

## Forking a run

A fork re-enters a finished run at one of its model calls and replays the
identical prefix against a **changed** harness. That is the difference between
debugging the failure you had and hoping a fresh run reproduces it.

```bash
hiveloom fork run_abc123 --list
# seq  turn  phase
#  12     1  react
#  27     2  react
#  42     3  react

hiveloom fork run_abc123 --at 42 --name probe
# -> ./my-harness/.hiveloom/forks/probe
```

**Fork points are model calls.** The folded state immediately before a
`model_call` is by construction a valid provider request; an arbitrary seq can
land mid-turn with a dangling `tool_use` and no result, which no provider
accepts. A `--at` that names something else snaps back to the previous model
call and says so.

The fork directory holds the harness that *actually ran* — spec rebuilt from
the journal snapshot, files verified against its sha256 manifest — plus:

```text
.hiveloom/forks/probe/
├── harness.yaml          # the parent's spec, as it was at run time
├── tools/ validators/    # the parent's code, verified against the manifest
├── fork.yaml             # the lineage record
└── .hiveloom/context.json  # the folded conversation at the fork point
```

`fork.yaml` pins the lineage to the exact journal line it came from:

```yaml
parent_run_id: run_abc123
parent_harness_version_hash: 9f2c1a4b7e05
at_seq: 42
at_turn: 3
parent_line_hash: 3b1f…          # sha256 of that journal line
created_at: 2026-08-28T09:14:22+00:00
context_file: .hiveloom/context.json
harness_version_hash: c05e8d31aa17
```

Edit the fork's `harness.yaml`, then replay:

```bash
hiveloom run ./my-harness/.hiveloom/forks/probe --resume
```

`--resume` takes no `--input`: the input is the prefix. In the library,
`runner.run_harness(resume_messages=..., lineage=...)` is the same seam.

### Where forks live, and why

Forks live **inside the harness they came from**, under `.hiveloom/forks/`. A
fork is an experiment *on* a harness rather than a harness of its own, so
archiving or packaging the harness takes its experiments with it, a directory of
harnesses stays a directory of harnesses, and the parent's file tools — rooted
at the harness folder, which they do not descend into `.hiveloom` from — cannot
reach a running experiment.

Forking a fork produces a **sibling** under the same original harness rather
than nesting a level deeper. Depth would record generation, and `fork.yaml`
already does. `fork.fork_target()` is the one resolver the CLI, the workbench,
and MCP share, so they cannot disagree about where a fork goes.

`--dir` opts a one-off out of containment for your own shell. The workbench has
no such escape hatch: a fork writes files, and a browser must never choose
where.

### When a fork is refused

| Situation | Behaviour |
|---|---|
| A behavioural file changed since the parent run | refused; `--allow-drift` overrides and warns |
| The parent's hash chain is broken | refused outright |
| The parent predates 1.0 (no harness snapshot) | refused outright |
| The prefix contains a `[REDACTED]` span | allowed, with a warning |

The redaction warning matters: redaction happens *before* persistence, so the
fork sends the marker where the parent sent a value. The run is reproducible in
shape, not in content.

**Trust inheritance.** A fork whose files came from an already-trusted source
folder, verified against the journal's manifest, inherits that trust — it is the
same code at a new path. A fork built from a journal's inlined `contents` never
does, and says so: a journal is a file someone can hand you, and its hash chain
proves internal consistency, not provenance.

### Lineage

`runs` carries `parent_run_id` and `forked_at_seq` (migrated in place on
existing Hives), so a fork and its parent are compared on the prefix they share
rather than as two unrelated runs:

```bash
hiveloom lineage run_abc123
```

## Model hot-swap

The executing model can change while a run is in flight, through two surfaces
and no others.

**Declarative** — a playbook may declare its own `model:` (and
`model_provider:`). Profile on a cheap model, decide on an expensive one, inside
one harness and one conversation. Leaving the mode restores the harness default,
so a mode is a configuration and not a one-way door:

```yaml
playbooks:
  - name: triage
    model: claude-haiku-4-5-20251001
    prompt: playbooks/triage.md
  - name: decide
    model: claude-opus-5
    prompt: playbooks/decide.md
```

Both fields are **frozen from evolution**, joining top-level `model`: evolution
must not move a harness onto a pricier model, or a different lab, on its own
initiative.

**Imperative** — `RunControl.switch_model(...)` and `POST /runs/{run_id}/model`,
consumed at the loop's next turn boundary alongside stop and steer, where no
model call or tool is in flight.

`hiveloom.models.router.ModelRouter` owns which model is current and which
provider instance serves it, building cross-provider instances lazily so an
unused provider's absent credentials are not a startup failure. Pass
`run_harness(providers={...})` to pre-register what a swap may talk to.

At the boundary, prior turns are stripped of model-internal content —
`thinking`, `redacted_thinking`, anything carrying a `signature` — because those
blocks are only valid for the model that produced them. An assistant turn
stripped to nothing is dropped whole rather than left to break role alternation.

Swaps are journalled as `model_swap`; a swap to an unknown provider is
`model_swap_failed` and the run continues on the model it had.

### Swapped runs are held out of fitness

A run whose model moved did not execute the harness as declared. Averaging it
into "this version scores N%" would silently turn that number into a
distribution over model paths, so `Hive.version_stats` **excludes** it from the
per-version bucket and reports the count held out as `swapped_runs`.
`hiveloom stats` says so in words and breaks the held-out runs into a
per-model-path table; `version_stats(include_swapped=True)` returns the raw
population. An empty `model_path` — every pre-1.0 run — means "not recorded",
never "swapped".

### Fork × swap: the controlled A/B

The cleanest experiment the two features make possible is one exact prefix,
replayed on two models:

```bash
hiveloom fork run_abc123 --name on-haiku  --model claude-haiku-4-5-20251001
hiveloom fork run_abc123 --name on-sonnet --model claude-sonnet-5
hiveloom lineage run_abc123
```

`--model` rewrites the fork's **spec** (through `construct.set_model`, so it is
validated and rolled back like any other edit) rather than swapping mid-run.
Each arm is therefore a clean sample of its own harness version, and neither is
held out as swapped. A rejected model removes the half-built fork rather than
leaving one behind that claims a model it does not have.

## Reading the journal from the Hive

Ingested runs are queryable:

```python
from hiveloom import Hive

with Hive() as hive:
    hive.search_runs("invoice reconciliation")      # runs by what was asked
    hive.compare_versions("my-harness", "9f2c1a", "c05e8d")
    hive.lineage("run_abc123")
```

`runs` carries `task` (the opening statement, capped at 2000 chars — a title and
search target, not a shadow copy of the journal) and `model_path`.

`compare_versions` puts two versions side by side with deltas (right minus
left), plus which failure signatures stopped appearing and which started. It
reports `underpowered` when either side has fewer than five runs, because a
confident delta over a sample of two is worse than no delta.

## See also

- [The workbench](workbench.md) — the graphical front end to all of this
- [Harness spec](spec.md) — `logging`, `playbooks`, and the frozen fields
- [Architecture](architecture.md) — where the journal sits in the runtime
- [Deploying and evolving](deploying-and-evolving.md) — the evidence loop
