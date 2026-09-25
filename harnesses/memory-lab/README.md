# memory-lab

> **Proves:** a small executor can work over far more data than its context
> holds — narrowing it in place, keeping findings outside the conversation,
> exporting results the model never reads — and learn across runs, with
> nothing it learns reaching the harness except through human review.

Offline, no API key.

`extensions/memory_provider.py` registers a scripted provider, so every command
below is deterministic and offline. The script carries no answers: every value
it reports is parsed out of a tool result it actually received, which is what
makes the journal worth reading.

| layer | what it is | mechanism here |
|---|---|---|
| L1 | what the model sees | the conversation, compacted by `context.compaction` |
| L2 | run-scoped, survives compaction | spilled results, `transform_result`, `notes`, handles as tool arguments |
| L3 | durable across runs | `memory.entries`, reviewed through the proposals queue |

The task is the one from `log-forensics`: a 77 KB service log, three facts to
report, one of them on the last line. The difference is how the executor holds
what it learns.

## Capabilities

- **Spill and handles** — an oversized result is stored whole and returned as
  a preview plus a handle. ([spec.md](../../docs/spec.md))
- **`transform_result`** — `count`, `grep`, `tail` and friends over a handle,
  in place; oversized output becomes a *derived* handle.
- **`notes`** — run-scoped findings outside the conversation, indexed in the
  system prompt, carried into a fork through a verified manifest.
- **Handle-typed parameters** — `file_write` takes a handle as its content;
  the runtime expands it, the model never sees the bytes.
- **Artifact verification** — `file_exists` proves the export was written and
  `command_succeeds` (a confined process) proves it holds the dominant failure
  code the answer reports.
- **Durable memory (L3)** — `memory.entries`, grown only through reviewed
  proposals: `propose_memory` from the executor, `evolve --propose` from the
  evolver. ([signal-driven-evolution.md](../../docs/signal-driven-evolution.md))

## Run it

```bash
uv sync                              # install the pinned runtime
hiveloom validate .
hiveloom run . --input-text "Investigate data/service.log and report the three facts." --json
hiveloom trace <run_id>
```

## What to look for

In the trace, in order:

1. `tool_spilled` — `file_read` came back as a preview and a handle.
2. Three `transform_result` calls on that handle: `count` for the ERROR total,
   `grep` for the `code=` lines, `tail` for the digest. Nothing is paged
   through context.
3. `note_written` — the findings go into a note, outside the conversation.
   The next `context_system` event shows a `# Notes` index in the system prompt.
4. A second `tool_spilled` with `derived_from` set: a wider `grep` with context
   was still too large, so it became a *derived* object with its own handle.
5. `file_write` whose `content` argument is that handle. The runtime expanded
   it at dispatch; the journal and the provider only ever saw the handle. The
   33 KB result lands in `out/error-context.txt`.
6. `notes` read, then a `propose_memory` call: the executor offers a lesson it
   learned. `memory_proposed` records it with `outcome: queued`; nothing in
   the harness changes. It waits in `hiveloom proposals list .` with
   `trigger: executor` for a human.
7. The final JSON, built from the note rather than from the turns above.
8. Three `verification_result` events: the output schema, then `file_exists`
   and `command_succeeds` on `out/error-context.txt` — the export is checked,
   not assumed.

The `# Memory` section at the top of every system prompt is L3: the one entry
seeded below was added by an operator and is frozen in place until the operator
or an applied proposal changes it.

```bash
hiveloom memory list .
```

## Fork it

A fork re-enters the run at one of its model calls. Notes and spilled objects
travel with it — but only through the manifest `hiveloom fork` writes from the
parent's verified journal, never because the transcript mentions them.

```bash
hiveloom fork <run_id> --list                    # the model calls you may re-enter
hiveloom fork <run_id> --at <seq> --name probe   # a model call after note_written
grep -A4 notes_manifest .hiveloom/forks/probe/fork.yaml
hiveloom run .hiveloom/forks/probe --resume --json
hiveloom trace <resumed_run_id>                  # notes_inherited, then the note read
```

## Evolve it

The evolver never writes memory directly either. An operator finding turns into
a gated proposal that appends one entry; applying it is a separate, reviewed
step and a new harness version.

```bash
hiveloom evolve . --propose --model memory_lab/qa-evolver \
  --note "The build digest is only ever on the last SUMMARY line."
hiveloom proposals list .
hiveloom proposals show . <proposal_id>          # path: memory.entries.+
hiveloom proposals apply . <proposal_id> --yes   # --yes: --json cannot prompt
hiveloom memory list .                           # two entries now
hiveloom run . --input-text "..." --json         # the new fact is in # Memory
hiveloom stats .                                 # a new version bucket
```

`memory.entries.+` is an append resolved when the proposal is applied, not a
position chosen when it was queued — so a second lesson queued against this
same version still lands *after* the first instead of replacing it, and the
`--yes` is what tells a non-interactive apply to go ahead.

`hiveloom memory forget . digest-is-in-the-tail` puts it back.

The memory budgets (`memory.max_entries`, `memory.max_entry_chars`,
`memory.prompt_budget_chars`, `memory.enabled`) are frozen from evolution: a
proposal that touches them is rejected at the gate, and an entry that would
overflow them is rejected at apply with `harness.yaml` untouched.

## What the executor may propose

`propose_memory` is opt-in (`max_per_run: 2` here). A lesson the executor
queues is gated the same way as an evolver's, deduplicated on its content, and
never applied by the run that proposed it. A second run that proposes the same
lesson is told it is already pending. Review it like any other proposal:

```bash
hiveloom proposals list .                        # trigger: executor
hiveloom proposals show . <proposal_id>
hiveloom proposals apply . <proposal_id> --yes   # or: proposals reject
```

## Try this

- `hiveloom set memory.selection relevant` and add a few lessons about other
  tasks with `hiveloom memory add`: each run is shown only the ones that match
  its task (`memory_selected` in the trace), and `search_memory` finds the rest.
- `hiveloom set context.tool_results.transforms false` removes
  `transform_result`: the control arm for measuring what the transforms are
  worth.
