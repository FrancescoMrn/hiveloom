---
name: hiveloom-eval
description: >-
  Define and validate local Hiveloom eval datasets and scorers, attach numeric
  metrics, and inspect eval identity. Use for evaluator, dataset, scorer,
  RunMetric, eval.yaml, or held-out expected-data work.
---

# Evaluate a Hiveloom harness

Keep eval policy outside `harness.yaml`. Start by reading the live contracts:

```bash
hiveloom eval schema --json
hiveloom catalog datasets --json
hiveloom catalog scorers --json
hiveloom metrics schema --json
```

An eval document uses `schema_version: 1`, points to one harness, selects one
registered dataset loader and one or more registered scorers, and declares a
repetition count. Validate it before spending model budget:

```bash
hiveloom eval validate eval.yaml --json
```

Dataset loaders and scorers are extension code. A foreign eval folder must be
trusted before its local extension runs. Never copy private cases, expected
data, CV evidence, or raw traces into the repository.

Expected data is held out by default. Leave
`dataset.include_expected_in_input: false` unless the eval explicitly tests a
task where the expected data belongs in the executor input.

A scorer runs after harness verification. It receives the public `RunResult`,
held-out expected data, optional verification context, and public artifacts.
Return validated `RunMetric` values plus bounded diagnostics. Keep model status
and scorer status separate: a scorer exception must not relabel a successful or
failed harness run.

Read `docs/evaluating.md` for extension examples, identity digests, and privacy
rules. Use `hiveloom metrics list ... --json` to inspect ingested signals, and
always compare sample and missing-value counts together.
