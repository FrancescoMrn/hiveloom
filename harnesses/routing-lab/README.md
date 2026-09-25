# routing-lab

> **Proves:** one run can change *what the agent is* mid-task — its model and
> its tools — on a pinned plan; a failure can be re-entered at the exact turn it
> went wrong; and an aimed evolution fixes it with the fix *measured*, all
> deterministic and offline.

No API key: `extensions/qa_provider.py` registers a small scripted provider, so
every command below produces the same journal every time.

## Capabilities

- **Playbooks** — two stages, each with its own model *and* tool set, switched
  inside one run and one conversation. ([spec.md](../../docs/spec.md))
- **`plan_then_act`** — a planning turn first; the plan is pinned into the
  system prompt for the rest of the run, so compaction cannot drop it.
- **Output schema verification** — `decide` must end in one JSON object.
- **Forking** — re-enter a finished run at one of its model calls and replay
  the identical prefix against a changed harness or model.
  ([journal.md](../../docs/journal.md))
- **Signal-driven evolution** — `signal` locates the failure, `evolve` proposes
  an aimed fix, `assess` checks it against its prediction.
  ([signal-driven-evolution.md](../../docs/signal-driven-evolution.md))

| playbook | model         | tools                          |
|----------|---------------|--------------------------------|
| `triage` | `qa-triage`   | `file_read`, `switch_playbook` |
| `decide` | `qa-decision` | `switch_playbook`              |

`decide` genuinely cannot read a file — the evidence it reasons over is what
`triage` already put in the conversation. Routing that only swapped the model
would be a model swap; this is a change of what the agent *is* for that stretch
of the run.

## Run it

```bash
hiveloom validate .
hiveloom run . --input incident.txt --json
hiveloom trace <run_id>              # the plan, then the playbook switch
```

## Fork it

```bash
hiveloom fork <run_id> --list                    # the model calls you may re-enter
hiveloom fork <run_id> --at <seq> --name probe   # lands in .hiveloom/forks/probe
hiveloom run .hiveloom/forks/probe --resume
hiveloom lineage <run_id>                        # parent and forks, on their shared prefix
hiveloom fork <run_id> --name on-alt --model qa-alt --provider routing_lab
```

A fork is an experiment *on* this harness, so it lives in this folder's
`.hiveloom/`, where `file_read` (rooted here, not descending into `.hiveloom`)
cannot reach it; the workbench shows forks nested under their harness.

## Evolve it

An input containing `FORCE_FAIL` makes `qa-decision` answer in prose unless the
prompt insists on JSON — a stand-in for a model that drifts off its contract.

```bash
for i in 1 2 3; do hiveloom run . --input-text "FORCE_FAIL: handle incident.txt" --json; done
hiveloom signal .                                   # verify_failed, all in the content loss class
hiveloom evolve . --propose --model routing_lab/qa-evolver --json
hiveloom proposals apply . <proposal_id> --yes --json
for i in 1 2 3 4 5; do hiveloom run . --input-text "FORCE_FAIL: handle incident.txt" --json; done
hiveloom assess .
```

## What to look for

- **The plan**: the first `model_call` has `phase: plan`, and `context_system`
  from then on carries a `# Plan` section.
- **The switch**: a `playbook_switch` to `decide`, then `model_swap` with
  `source: playbook` — declared routing, which keeps the run in its version's
  fitness bucket (an operator's swap would hold it out).
- **The fork**: `fork --list` offers each model call; the resumed fork's
  journal shares its parent's prefix byte for byte (`trace --verify`).
- **The evolution**: the proposal's `target` is `status:verify_failed`, taken
  from the signal map; after `apply`, forced runs pass, and `assess` reports it
  **confirmed** (3 of 4 runs failing before, 0 of 5 after, Fisher p ≈ 0.05).

## Try this

- `hiveloom set loop.policy react` and run again: the same routing, no pinned
  plan — compare the two journals.
- `hiveloom fork <run_id> --name on-alt --model qa-alt --provider routing_lab`
  replays the prefix on another model, the clean A/B for a model swap.
