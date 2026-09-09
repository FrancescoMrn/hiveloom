# log-forensics

The demo for the containment release. One shell command produces 77 KB of
output, and the harness has to answer three questions about it — including one
whose answer is in the last line — without ever holding the log in context and
without the command being able to reach anything but the log.

Three 1.1.0 mechanisms do the work:

- **`confinement`** decides what `cat` and `grep` may do once they run, which
  is a different question from the allowlist deciding *which* commands run.
  Here: no network, nothing writable, the user's home hidden, a 20s deadline.
- **`context.tool_results`** spills the oversized result whole to run-private
  storage and puts a head/tail preview plus a handle in context. The
  `search_tool_result` and `read_tool_result` tools appear at that first spill
  and read it back.
- **`egress`** screens the request that carries all of this to the provider.

## Run it

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom confinement .        # what THIS machine will actually enforce
hiveloom run . --input-text "Investigate data/service.log and report the three facts."
```

Expected output, verified against `schemas/output.json`:

```json
{"dominant_failure_code": "POOL_EXHAUSTED", "error_count": 84, "build_digest": "8F2C-77A1-DE30"}
```

## What to look for in the trace

```bash
hiveloom trace <run_id>
```

- `run_started` records the confinement backend that ran (`bwrap` on Linux,
  `sandbox-exec` on macOS, or the portable baseline where neither exists).
- `tool_spilled` records the handle, the byte count, and how much was omitted
  from context — around 75 KB of the 78 KB.
- The turns after it call `search_tool_result` to count `ERROR` and each
  `code=` value, then `read_tool_result` at a byte offset near the end for the
  `digest=` on the SUMMARY line.

That last step is the point. Before spilling, an oversized result was cut to
its leading characters, so the tail — where totals and the digest live — was
simply gone for the rest of the run. The full result is still in the journal
either way; what changed is that the model can now get it back.

## The parts that are deliberate

**The allowlist is narrow and the phases are enforced.** `cat` is pinned to the
exact argv `cat data/service.log`; `grep` and `wc` may take model-chosen
arguments, which is only permitted because a sandbox is masking runtime-private
state. `loop.steps` then reads once, investigates from the handle, and answers
with no tools at all.

**`recall_runs` is declared but scoped to `version`.** A second run of the same
harness version can look up the first as a worked example. It can never see
another harness's runs — the harness key comes from the run context, not from
tool input.

**Nothing here can read the journal.** `.hiveloom/`, the trace directory and
the spill store beside it are masked from every spawned process, so the `grep`
this harness allows cannot read back another run's evidence. Confirm it:

```bash
hiveloom confinement . --json    # runtime_state_hidden, home_hidden, network_isolated
```

## Changing it

Do not hand-edit `harness.yaml`. Make changes through the CLI, which validates
every mutation and rolls back on error — including the shell allowlist:

```bash
hiveloom add tool --builtin shell --param 'commands=["wc -l data/service.log"]' --dir .
```
