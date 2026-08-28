# quickstart

The smallest harness that is still a harness: a prompt, a model, two
guardrails, and no tools whatsoever.

It exists to make one point. Even here — where the harness adds no
capability — it adds a *record*. The run is journalled, the spec it ran under
is hashed, and the cost ceiling is declared in the file rather than hoped for.
Everything the other demos add is layered onto this.

## Run it

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input "What is a harness, in the hiveloom sense?"
```

## Then look at what happened

```bash
hiveloom stats .              # success rate, cost and turns, per version hash
hiveloom trace <run_id>       # the ordered journal for one run
```

## Where to go next

- `../example-summarizer` adds tools and verification.
- `../article-extractor` adds a custom tool written in Python.
- `../routing-lab` adds playbooks, per-playbook models, and forking.

## Rebuilding

This folder is generated. Edit `scripts/build_harnesses.py` and re-run it
rather than editing `harness.yaml` by hand.
