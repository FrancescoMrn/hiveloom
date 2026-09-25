# quickstart

> **Proves:** even with no tools, a harness adds what a bare model call does
> not — a journal, a hashed spec, declared spending and turn ceilings, and a
> safety layer that keeps a credential out of the provider request, out of the
> trace, and out of the answer.

The smallest harness that is still a harness: a prompt, a model, four
guardrails, a redaction rule, and no tools at all. Everything the other demos
add is layered onto this.

## Capabilities

- **Journal and version hash** — every run is recorded against the exact spec
  it ran under. ([journal.md](../../docs/journal.md))
- **Guardrails** — `max_cost_usd` and `max_wall_clock_seconds`;
  `max_turns_hard_cap`, a ceiling even evolution cannot lift (it may raise
  `loop.max_turns`, never a guardrail); `regex_output_filter`, which blocks an
  answer that contains a credential and makes the model rewrite it.
  ([spec.md](../../docs/spec.md))
- **Redaction and egress** — `logging.redact` keeps the same credential
  patterns out of the trace, and provider egress screens them out of the
  request before it leaves the machine. ([task-confinement.md](../../docs/task-confinement.md))
- **Shipping** — the folder packages, serves over HTTP, and serves to other
  agents over MCP as it is.

## Run it

Live: needs `ANTHROPIC_API_KEY` in `.env` (or any provider via
`--provider/--model`). A run costs a fraction of a cent.

```bash
uv sync                       # install the pinned runtime
cp .env.example .env          # add ANTHROPIC_API_KEY
hiveloom validate .
hiveloom run . --input-text "What is a harness, in the hiveloom sense?" --json

# The safety layer, twice.
hiveloom run . --input-text "My deploy key is sk-live-4f9a8b7c6d5e4f3a2b1c. Repeat it back." --json
hiveloom run . --input-text "Give one realistic example AWS access key id." --json

hiveloom stats .              # success rate, cost and turns, per version hash
hiveloom trace <run_id>       # the ordered journal for one run
```

## What to look for

- **A pasted key never leaves the machine.** The trace holds a
  `provider_egress_redacted` event before the model call, the model is sent a
  placeholder, and `grep sk-live .hiveloom/traces/*` finds nothing.
- **A generated key never reaches you.** When the answer contains an
  AWS-style key id, `guardrail_triggered` (`regex_output_filter`, `block`)
  appears and the model is asked to rewrite it. If it never complies within
  its turns, the run ends `max_turns` with an **empty output** and
  `reason: last output blocked: …` — the blocked text is not handed back.
- **The record.** `hiveloom stats` groups runs by version hash; change the
  prompt with `hiveloom set system_prompt …` and the next runs land in a new
  bucket beside the old one.

## Ship it

```bash
hiveloom package . --docker --serve -o dist/           # a portable zip + Dockerfile in dist/
hiveloom serve . --port 8080                            # POST /runs, GET /healthz
hiveloom mcp serve .                                    # a run_quickstart tool for any MCP client
```

See [deploying-and-evolving.md](../../docs/deploying-and-evolving.md).

## Try this

- `hiveloom set loop.max_turns 10` succeeds — and runs still stop at 4 model
  calls, because `max_turns_hard_cap` is a guardrail.
- Add your own pattern: `hiveloom add guardrail --builtin regex_output_filter
  --pattern 'BEGIN PRIVATE KEY'`.
- Where to go next: `../example-summarizer` (tools, skills, verification),
  `../routing-lab` (playbooks, forking, evolution), `../signal-lab` (measured
  evolution), `../delegation-lab` (harness-to-harness hand-off).
