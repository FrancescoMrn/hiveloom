# ranked-retrieval

This offline example ranks synthetic knowledge records, returns only IDs seen
in current-run tool evidence, and scores the result with Recall@3, nDCG@3, and
hallucination rate.

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

Do not hand-edit `harness.yaml`. This example was built with `init`, `add`, and
`set`; use the same validated commands for changes.
