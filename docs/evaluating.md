# Local evaluations

Hiveloom eval documents keep dataset and scoring policy outside
`harness.yaml`. A scorer runs only after normal harness verification and sees
the public `RunResult`; its numeric outputs use the same `RunMetric` validation
and Hive ingestion as `hiveloom metrics import`.

## Version one document

```yaml
schema_version: 1
harness: ../matching-harness
extensions:
  - eval_extension.py
dataset:
  loader: matching_cases
  params:
    split: test
  include_expected_in_input: false
scorers:
  - recall_at_k
  - name: ndcg
    params:
      k: 5
repetitions: 3
```

Inspect the schema without loading extensions, then validate the complete
local contract:

```bash
hiveloom eval schema --json
hiveloom catalog datasets --json
hiveloom catalog scorers --json
hiveloom eval validate eval.yaml --json
```

Validation resolves the harness, runs the dataset loader, constructs each
scorer, and returns only aggregate receipts: case count and content digests.
It does not print cases or expected values. Dataset loaders and scorers are
code, so eval-local extensions use the same trust gate as harness extensions.

`include_expected_in_input` defaults to false. In that mode the executor sees
only `EvalCase.input`; `expected` remains available to post-verification
scorers. Turning the flag on is an explicit request to add a delimited JSON
copy of expected data to the model input.

## Dataset and scorer extensions

An installed pack or trusted local extension can register both components:

```python
from hiveloom import EvalCase, RunMetric, ScorerOutput


class Cases:
    def load(self):
        return [
            EvalCase(id="case-1", input="Rank the records.", expected={"ids": ["r2"]})
        ]


class RecallAtOne:
    def score(self, context):
        matched = context.expected["ids"][0] in context.run_result.output
        return ScorerOutput(
            metrics=[
                RunMetric(
                    run_id=context.run_result.run_id,
                    name="recall_at_1",
                    value=float(matched),
                    direction="maximize",
                    unit="ratio",
                    source="retrieval_eval_v1",
                    scope="case",
                )
            ]
        )


def hiveloom_extension(hive):
    hive.register_dataset(
        "matching_cases",
        lambda params, ctx: Cases(),
        description="Load local synthetic or private matching cases.",
    )
    hive.register_scorer(
        "recall_at_k",
        lambda params, ctx: RecallAtOne(),
        description="Measure retrieved expected identifiers.",
    )
```

A loader returns `EvalCase` objects or matching dictionaries with unique IDs.
A scorer receives `ScorerContext`: case ID and input, held-out expected data,
case metadata, the public run result, optional verification context, and public
artifacts. It returns `ScorerOutput`, one `RunMetric`, or a list of metrics.

Scorer exceptions do not rewrite the model result. `ScoringResult.run_status`
keeps the harness outcome while each scorer gets its own success/error receipt.
Valid metrics from other scorers can still be ingested.

## Identity and privacy

Every validated eval has four receipts:

- `spec_digest` covers the canonical eval document;
- `dataset_digest` covers the loader implementation and validated cases;
- `scorer_digest` covers scorer references and implementations;
- `eval_id` combines the three.

Only digests and counts are returned by validation. The raw case set and
expected values stay in the evaluator process. Scorers should put aggregates,
bounded diagnostics, and non-sensitive identifiers in their outputs, never CV
text, application rows, or full model/tool payloads.

The scorer SDK is the foundation for the native resumable runner. Normal
`hiveloom run` behavior is unchanged.
