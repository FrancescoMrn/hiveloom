# ranked-retrieval

> **Proves:** structure beats model size. Enforced phases, a verify-first
> search tool and grounded ids make a small model reliable, and a local eval
> measures it with ranked metrics.

Answers engineering questions from a 25-record knowledge base (22 published
and verified, plus a draft and an unverified runbook that look relevant and must
never be returned), returns only IDs seen in current-run tool evidence, and
scores the result with Recall@3, nDCG@3 and hallucination rate. The eval's ten
cases are mostly phrased the way people describe problems — "our service keeps
running out of database connections", "a container keeps getting killed" —
while the records are written in their authors' terms ("connection pooling",
"OOMKilled"): the vocabulary gap every internal search has. The data, tool,
validators and scorers are local and deterministic; the runs need a model.

Measured on `deepseek/deepseek-v4.1-flash` via OpenRouter: 20 of 20 eval cells
succeed with **zero hallucinated ids**, nDCG@3 0.92 and Recall@3 0.73 — the
best record is found every time, the supporting ones are often missed because
the person's words are not the record's. The contracts below, not the model,
carry the zero; the recall gap is what the improvement loop is for.

## Capabilities

It shows four contracts working together:

- `sequential_steps` exposes one tool during retrieval, requires that call to
  succeed, then removes all tools from the answer phase.
- `search_and_verify_records` combines lexical search with publication and
  quality checks. Ineligible hits never cross the tool boundary.
- `output_schema` checks the JSON shape while `grounded_references` separately
  rejects IDs absent from the approved tool result.
- `eval.yaml` keeps synthetic expected relevance outside model input and
  records ranked metrics that match `evolution.objectives`.

## Why the search tool is composite

Search followed by eligibility checking is one domain operation here. The
invariant is simple: the model must never see a draft or unverified record.
Keeping both operations inside one deterministic tool makes that invariant
enforceable before evidence enters the conversation.

A composite tool is the wrong choice when calls are independently useful,
need different permissions, should run in parallel, or must remain separate
for audit or human review. In those cases, keep the tools separate and use
structured steps to control their order and availability.

## Inspect it without credentials or network

```bash
hiveloom validate . --json
hiveloom run . --input-text \
  "Rank up to three records about PostgreSQL query performance." \
  --dry-run --json
hiveloom eval validate eval.yaml --approve --json
```

The first live harness or eval run uses the configured model provider, but the
dataset, retrieval tool, validators, and scorers are local and deterministic:

```bash
hiveloom run . --input-text \
  "Rank up to three records about PostgreSQL query performance." --json
hiveloom eval run eval.yaml --json
```

## What to look for

- Two phases in every journal: one `search_and_verify_records` call, then an
  answer turn with no tools at all (`sequential_steps` removes them).
- `grounded_references` passing: every selected id is in the approved tool
  result, so a hallucinated id cannot survive verification.
- After `eval run`, `hiveloom metrics list . --name recall_at_3 --json`, and
  `hiveloom signal .` for where any failure concentrates.

## Try this

- Measured evolution against the objectives:
  `hiveloom evolve . --experiment eval.yaml --yes --rounds 2`. A change is kept
  only when the eval confirms it: on the reference model two unguided rounds
  were reverted, and a round guided with `--note` ("pass the search the
  question rewritten into the knowledge base's terms") moved Recall@3 from
  0.795 to 0.817 — the right direction, not significant at 20 pairs, so also
  reverted. Raise `repetitions` in `eval.yaml` for the power to confirm a
  small effect.
- A research program on the vocabulary gap:
  `hiveloom research init . --name vocabulary --charter research.yaml`, then
  `hiveloom research run . --name vocabulary`. A director model designs the
  experiments and the engine measures them; with every run succeeding and the
  gap in ranking quality rather than in failures, expect it to test an idea,
  stop it for futility, and say why rather than promote an unproven change.
- Run it on a smaller model (`--provider openrouter --model
  mistralai/ministral-3b-2512`) and compare.

Do not hand-edit `harness.yaml`. This example was built with `init`, `add`, and
`set`; use the same validated commands for changes.
