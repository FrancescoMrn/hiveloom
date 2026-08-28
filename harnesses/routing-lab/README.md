# routing-lab

One incident, two stages, two models — and no API key required.

`extensions/qa_provider.py` registers a small scripted provider, so every
command below is deterministic and offline. That is what makes this the
harness to learn **playbooks**, **forking** and **evolution** on: you can run
the whole walkthrough repeatedly and get the same journal every time.

## What routing means here

| playbook | model         | tools                     |
|----------|---------------|---------------------------|
| `triage` | `qa-triage`   | `file_read`, `switch_playbook` |
| `decide` | `qa-decision` | `switch_playbook`         |

Both the executor *and* the tool set change at the boundary, inside one run
and one conversation. `decide` genuinely cannot read a file — the evidence it
reasons over is what `triage` already put in the conversation. Routing that
only swapped the model would be a model swap; this is a change of what the
agent *is* for that stretch of the run.

## Run it

```bash
uv sync                              # install the pinned runtime
hiveloom validate .
hiveloom run . --input incident.txt --json
hiveloom trace <run_id>              # watch the playbook switch land
```

## Fork it

A fork re-enters a finished run at one of its model calls and replays the
identical prefix against a changed harness — the same failure from the turn
where it went wrong, not a fresh run that may not reproduce it.

```bash
hiveloom fork <run_id> --list                    # the model calls you may re-enter
hiveloom fork <run_id> --at <seq> --name probe
```

The fork lands in **`.hiveloom/forks/probe`**, inside this harness. That is
deliberate: a fork is an experiment *on* this harness rather than a harness of
its own, so it belongs in this folder's workbench state. Archiving this folder
takes its experiments with it, a directory of harnesses stays a directory of
harnesses, and `file_read` above — rooted here, and not descending into
`.hiveloom` — cannot reach the experiment. The workbench shows forks nested
under the harness that contains them.

Edit the fork's `harness.yaml`, then:

```bash
hiveloom run .hiveloom/forks/probe --resume
hiveloom lineage <run_id>            # parent and forks, on their shared prefix
```

`--model` makes the commonest edit at fork time in one step — replay this
exact prefix on a different model:

```bash
hiveloom fork <run_id> --name on-alt --model qa-alt --provider routing_lab
```

## Rebuilding

This folder is generated. Edit `scripts/build_harnesses.py` and re-run it
rather than editing `harness.yaml` by hand. The provider lives in
`scripts/harness_assets/qa_provider.py`.
