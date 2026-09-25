# ranked-retrieval

> **Proves:** structure beats model size. Enforced phases, a verify-first
> search tool and grounded ids make a small model reliable, and a local eval
> measures it with ranked metrics.

Ranks synthetic knowledge records, returns only IDs seen in current-run tool
evidence, and scores the result with Recall@3, nDCG@3 and hallucination rate.
The data, tool, validators and scorers are local and deterministic; the runs
need a model (live, an API key).

Measured in the 1.2.0 release checks through OpenRouter: Ministral 8B passed
9 of 9 eval cells and Ministral 3B 4 of 4 ad-hoc queries, with no hallucinated
ids. The contracts below, not the model, carry most of that.

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
  `hiveloom evolve . --experiment eval.yaml --yes --rounds 2` (in the release
  checks both rounds aimed at `recall_at_3` and were reverted as inconclusive
  at 9 pairs — raise `repetitions` in `eval.yaml` for more power).
- Run it on a smaller model (`--provider openrouter --model
  mistralai/ministral-3b-2512`) and compare.

Do not hand-edit `harness.yaml`. This example was built with `init`, `add`, and
`set`; use the same validated commands for changes.
